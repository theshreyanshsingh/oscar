"""Analytics (app/analytics.py) and the Excel workbooks of calls (app/exports.py)."""

import io
import json
from datetime import datetime, timedelta, timezone

import pytest
from openpyxl import load_workbook

from app.analytics import analytics, outcome
from app.store import DEFAULT_AGENT, Store

NOW = datetime(2026, 11, 24, 3, 0, tzinfo=timezone.utc)  # 2 pm on a Tuesday in Sydney
SYDNEY = "Australia/Sydney"


def seed(store: Store, kind: str, ago: timedelta, *, seconds: int = 60, spoke: bool = True, cost: float = 0.0,
         status: str = "ended", notes: list[str] = (), **fields) -> dict:
    """A call that started `ago` before NOW and lasted `seconds`."""
    call = store.create_call(kind, status=status, from_number=fields.pop("frm", ""), to_number=fields.pop("to", ""))
    if fields:
        store.update_call(call["id"], **fields)
    start = NOW - ago
    end = None if status in ("live", "ringing", "connecting") else (start + timedelta(seconds=seconds)).isoformat(timespec="seconds")
    store._db.execute(
        "UPDATE calls SET started_at = ?, ended_at = ?, cost = ? WHERE id = ?",
        (start.isoformat(timespec="seconds"), end, cost, call["id"]),
    )
    store.add_message(call["id"], "agent", "Hello, Oscar here.")
    if spoke:
        store.add_message(call["id"], "user", "Hi there.")
    for note in notes:
        store.add_message(call["id"], "note", note)
    return store.get_call(call["id"])


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path / "oscar.db")
    yield store
    store.close()


def report(store: Store, days: int = 7, kind: str = "all") -> dict:
    return analytics(store, days=days, kind=kind, zone_name=SYDNEY, now=NOW)


def test_each_call_ends_one_way(store):
    seed(store, "phone_inbound", timedelta(hours=1), frm="+61400000001")  # talked
    seed(store, "phone_inbound", timedelta(hours=2), frm="+61400000002", spoke=False)  # hung up
    seed(store, "phone_inbound", timedelta(hours=3), frm="+61400000003", spoke=False, status="failed")
    seed(store, "phone_outbound", timedelta(hours=4), to="+61400000004", spoke=False, status="failed")
    seed(store, "phone_outbound", timedelta(hours=5), to="+61400000005", spoke=False, answered_by="voicemail")
    seed(store, "phone_inbound", timedelta(hours=6), frm="+61400000006", transferred_to="+61411111111", transfer_result="answered")
    seed(store, "phone_inbound", timedelta(hours=7), frm="+61400000007", transferred_to="+61411111111", transfer_result="no one answered")
    seed(store, "browser", timedelta(minutes=1), status="live")
    counts = {one["key"]: one["count"] for one in report(store)["outcomes"]}
    assert counts == {
        "talked": 1, "put_through": 1, "not_put_through": 1, "voicemail": 1,
        "not_reached": 1, "hung_up": 1, "failed": 1,
    }
    assert outcome({"status": "live", "kind": "browser"}) == "on_now"


def test_the_headline_figures_compare_with_the_period_before(store):
    seed(store, "phone_inbound", timedelta(days=1), seconds=90, cost=0.04, frm="+61400000001", follow_up="Call back")
    seed(store, "phone_inbound", timedelta(days=2), seconds=30, cost=0.02, frm="+61400000002", spoke=False)
    seed(store, "phone_outbound", timedelta(days=3), seconds=150, cost=0.06, to="+61400000003")
    seed(store, "phone_inbound", timedelta(days=9), seconds=60, cost=0.01, frm="+61400000004")  # the week before
    seed(store, "phone_inbound", timedelta(days=30), frm="+61400000005")  # long before: in neither
    data = report(store)
    assert data["totals"] == {
        "calls": 3,
        "conversations": 2,
        "conversation_rate": round(2 / 3, 4),
        "average_seconds": 120,
        "talk_seconds": 240,
        "incoming": 2,
        "missed": 1,
        "missed_rate": 0.5,
        "cost": 0.12,
        "cost_per_conversation": 0.06,
        "todos": 1,
        "todos_open": 1,
        "on_now": 0,
    }
    assert data["previous"]["calls"] == 1 and data["previous"]["conversations"] == 1
    assert (data["first_day"], data["last_day"]) == ("2026-11-18", "2026-11-24")


def test_calls_are_counted_by_the_owners_day_and_hour(store):
    # 22:30 UTC on 23 November is 9:30 am on Tuesday 24 November in Sydney.
    seed(store, "phone_inbound", NOW - datetime(2026, 11, 23, 22, 30, tzinfo=timezone.utc), frm="+61400000001")
    seed(store, "phone_outbound", NOW - datetime(2026, 11, 23, 22, 45, tzinfo=timezone.utc), to="+61400000002")
    seed(store, "browser", timedelta(days=3))
    data = report(store)
    points = {point["date"]: point for point in data["over_time"]["points"]}
    assert data["over_time"]["bucket"] == "day" and len(points) == 7
    assert (points["2026-11-24"]["incoming"], points["2026-11-24"]["outgoing"]) == (1, 1)
    assert points["2026-11-21"]["browser"] == 1
    assert data["hours"][1][9] == 2  # Tuesday, 9 am
    assert sum(map(sum, data["hours"])) == 3


def test_a_year_is_shown_week_by_week(store):
    seed(store, "phone_inbound", timedelta(days=200), frm="+61400000001")
    data = report(store, days=365)
    assert data["over_time"]["bucket"] == "week"
    assert 52 <= len(data["over_time"]["points"]) <= 54
    assert all(datetime.fromisoformat(point["date"]).weekday() == 0 for point in data["over_time"]["points"])
    assert sum(point["incoming"] for point in data["over_time"]["points"]) == 1


def test_one_kind_of_call_at_a_time(store):
    seed(store, "phone_inbound", timedelta(hours=1), frm="+61400000001")
    seed(store, "phone_outbound", timedelta(hours=2), to="+61400000002")
    seed(store, "browser", timedelta(hours=3))
    assert report(store, kind="incoming")["totals"]["calls"] == 1
    assert report(store, kind="outgoing")["totals"]["calls"] == 1
    assert report(store, kind="browser")["people"]["numbers"] == 0
    assert report(store, kind="incoming")["lists"] == []


def test_conversations_are_sorted_by_how_long_they_lasted(store):
    for seconds in (10, 45, 100, 200, 400, 900, 2000):
        seed(store, "browser", timedelta(hours=1), seconds=seconds)
    seed(store, "browser", timedelta(hours=1), seconds=5, spoke=False)  # not a conversation
    assert [one["count"] for one in report(store)["lengths"]] == [1, 1, 1, 1, 1, 2]


def test_what_oscar_did_comes_from_the_transcripts(store):
    seed(
        store, "phone_inbound", timedelta(hours=1), frm="+61400000001",
        notes=["Searched the web for: opening hours", "Searched the web for: parking",
               "Looked in the spreadsheets for: gate. Read: Prices (2 rows)",
               "Asked not to be called again: +61400000001 is on the do-not-call list."],
        summary_status="ready",
    )
    seed(store, "phone_outbound", timedelta(hours=2), to="+61400000002", spoke=False, answered_by="voicemail",
         notes=["A voicemail answered. Left this message: Hi, it's Oscar."], recording_seconds=12.5)
    seed(store, "phone_inbound", timedelta(hours=3), frm="+61400000003", transferred_to="+61411111111", transfer_result="answered")
    actions = report(store)["actions"]
    assert actions == {
        "web_searches": 2, "sheet_lookups": 1, "put_through": 1, "put_through_answered": 1,
        "voicemails_left": 1, "do_not_call": 1, "summaries": 1, "recorded": 1,
        "recorded_seconds": 12, "interrupted": 0,
    }


def test_new_and_returning_numbers(store):
    seed(store, "phone_inbound", timedelta(days=40), frm="+61400000001")  # called long before
    seed(store, "phone_inbound", timedelta(days=1), frm="+61400000001", caller_name="Sam")
    seed(store, "phone_inbound", timedelta(days=2), frm="+61400000002")
    seed(store, "phone_inbound", timedelta(days=3), frm="+61400000002", spoke=False)
    seed(store, "phone_outbound", timedelta(days=4), to="+61400000003")
    people = report(store)["people"]
    assert (people["numbers"], people["new"], people["repeat"]) == (3, 2, 1)
    top = people["top"]
    assert [one["number"] for one in top] == ["+61400000002", "+61400000001", "+61400000003"]
    assert (top[0]["calls"], top[0]["conversations"]) == (2, 1)
    assert top[1]["name"] == "Sam"


def test_summary_fields_are_counted_whatever_their_capitals(store):
    store.save_agent(DEFAULT_AGENT.model_copy(update={"summary_fields": ["Reason"]}))
    for value in ("Quote", "quote.", "QUOTE", "Booking", "Complaint", "", None):
        details = {"Reason": value} if value is not None else {}
        call = seed(store, "phone_inbound", timedelta(hours=1), frm="+61400000001")
        store.update_call(call["id"], details=json.dumps(details) if details else "")
    (field,) = report(store)["fields"]
    assert field["name"] == "Reason"
    assert field["filled"] == 5
    assert field["values"][0] == {"value": "Quote", "count": 3}
    assert {one["value"] for one in field["values"][1:]} == {"Booking", "Complaint"}


def test_call_lists_made_or_calling_in_the_period_show(store):
    from app.campaigns import parse_contacts

    contacts, _, _ = parse_contacts("Name,Phone\nSam,+61412345678\nLee,+61498765432\n", "+61")
    fresh = store.create_campaign(
        {
            "name": "Quotes", "status": "paused", "instructions": "", "greeting": "", "voicemail_message": "",
            "calling_from": 540, "calling_until": 1020, "timezone": "Australia/Sydney", "at_once": 1,
        },
        contacts,
    )
    store._db.execute("UPDATE campaigns SET created_at = ? WHERE id = ?", ((NOW - timedelta(days=60)).isoformat(), fresh["id"]))
    assert report(store)["lists"] == []  # made long before, and nothing called in the period
    store._db.execute("UPDATE campaigns SET created_at = ? WHERE id = ?", ((NOW - timedelta(days=2)).isoformat(), fresh["id"]))
    (shown,) = report(store)["lists"]
    assert (shown["name"], shown["total"], shown["calls"]) == ("Quotes", 2, 0)
    assert len(report(store, days=365)["lists"]) == 1


def test_the_analytics_route(client):
    data = client.get("/api/analytics", params={"days": 30, "kind": "all", "tz": SYDNEY}).json()
    assert data["days"] == 30 and data["time_zone"] == SYDNEY
    assert data["totals"]["calls"] == 0
    assert client.get("/api/analytics", params={"days": 12}).status_code == 400
    # A time zone that is not one counts in UTC.
    assert client.get("/api/analytics", params={"days": 7, "tz": "Nowhere/Else"}).json()["time_zone"] == "UTC"


# ----- Excel workbooks -----


def workbook(response) -> dict:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument.spreadsheetml")
    book = load_workbook(io.BytesIO(response.content))
    return {sheet.title: [list(row) for row in sheet.iter_rows(values_only=True)] for sheet in book.worksheets}


def test_call_history_downloads_as_excel_with_what_was_said(client, cfg):
    store = Store(cfg.db_path)
    call = store.create_call("phone_inbound", status="ended", from_number="+61412345678")
    store.update_call(call["id"], summary="Asked about a quote.", caller_name="Sam", ended_at=call["started_at"])
    store.add_message(call["id"], "agent", "Hi, Oscar here.", at_seconds=0.4)
    store.add_message(call["id"], "user", "=HYPERLINK(\"http://evil.test\")", at_seconds=3.0)
    store.add_message(call["id"], "note", "Searched the web for: gates")
    store.close()
    sheets = workbook(client.get("/api/calls.xlsx", params={"tz": SYDNEY}))
    assert list(sheets) == ["Calls", "Transcripts"]
    heading, row = sheets["Calls"]
    assert heading[:6] == ["Started", "Type", "Number", "Caller's name", "Length (min:s)", "How it ended"]
    assert row[1:4] == ["Incoming", "+61412345678", "Sam"]
    assert row[5] == "Talked with Oscar"
    assert isinstance(row[0], datetime)  # a real date, in Sydney's time
    started = datetime.fromisoformat(call["started_at"]).astimezone(__import__("zoneinfo").ZoneInfo(SYDNEY))
    assert row[0] == started.replace(tzinfo=None)
    _, oscar, caller, note = sheets["Transcripts"]
    assert oscar[3:5] == ["Oscar", "Hi, Oscar here."]
    # What a caller said that looks like a formula stays their words.
    assert caller[3:5] == ["Caller", "=HYPERLINK(\"http://evil.test\")"]
    assert round(caller[2].total_seconds() if hasattr(caller[2], "total_seconds") else caller[2] * 86400) == 3
    assert note[3:5] == ["Note", "Searched the web for: gates"]


def test_analytics_downloads_as_excel(client, cfg):
    store = Store(cfg.db_path)
    an_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    seed(store, "phone_inbound", NOW - an_hour_ago, frm="+61400000001")
    store.close()
    sheets = workbook(client.get("/api/analytics.xlsx", params={"days": 7, "tz": SYDNEY}))
    assert list(sheets) == ["Overview", "Day by day", "Calls", "Transcripts"]
    overview = {row[0]: row[1:] for row in sheets["Overview"] if row and row[0]}
    assert overview["Calls"][0] == 1
    assert overview["Talked with Oscar"][0] == 1
    assert len(sheets["Day by day"]) == 1 + 7
    assert len(sheets["Calls"]) == 2
    assert client.get("/api/analytics.xlsx", params={"days": 5}).status_code == 400
