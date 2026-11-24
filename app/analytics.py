"""What the dashboard's Analytics section shows: how Oscar's calls went over a period.

It is all worked out from what Oscar already keeps (app/store.py): the calls,
what their transcripts show, their summaries and the call lists. Nothing is sent
anywhere and nothing is asked of a model. Days and hours are the owner's: the
dashboard says which time zone it is in.

Each call ends one way (`OUTCOMES`), decided in this order:
  - a voicemail or another machine answered a call Oscar made;
  - the caller was put through to a person, who answered, or not;
  - the other person spoke: they talked with Oscar;
  - a call Oscar made that no one picked up (no answer, busy, could not be made);
  - an incoming or browser call that failed (an error, or turned away);
  - an incoming or browser call where no one said anything: hung up before speaking.
A call still going on counts only as that.
"""

import re
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.store import OPEN_STATUSES, Store

PERIODS = (7, 30, 90, 365)
KINDS = {"all": None, "incoming": "phone_inbound", "outgoing": "phone_outbound", "browser": "browser"}
# Up to 90 days is shown day by day, a year week by week.
LONGEST_DAILY_PERIOD = 90

OUTCOMES = (
    ("talked", "Talked with Oscar"),
    ("put_through", "Put through to a person"),
    ("not_put_through", "Put through, no one answered"),
    ("voicemail", "Reached a voicemail"),
    ("not_reached", "No answer or busy"),
    ("hung_up", "Hung up before speaking"),
    ("failed", "Failed"),
)
# How long conversations lasted: (label, under this many seconds).
LENGTHS = (
    ("Under 30 s", 30),
    ("30 s – 1 min", 60),
    ("1 – 3 min", 180),
    ("3 – 5 min", 300),
    ("5 – 10 min", 600),
    ("10 min or more", None),
)
TOP_PEOPLE = 8
TOP_FIELD_VALUES = 6
FIELD_VALUE_CHARS = 60


def time_zone(name: str) -> ZoneInfo | timezone:
    """The owner's time zone by its IANA name ("Australia/Sydney"); UTC when it is not one."""
    try:
        return ZoneInfo(name) if name and re.fullmatch(r"[A-Za-z0-9_+\-/]{1,64}", name) else timezone.utc
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def other_number(call: dict) -> str:
    """The other end of a phone call: who called in, or who Oscar called."""
    return {"phone_inbound": call.get("from_number"), "phone_outbound": call.get("to_number")}.get(
        call.get("kind"), ""
    ) or ""


def outcome(call: dict) -> str:
    """How the call ended: one of OUTCOMES' keys, or "on_now" for one still going on."""
    if call["status"] in OPEN_STATUSES:
        return "on_now"
    if call.get("answered_by") in ("voicemail", "fax"):
        return "voicemail"
    if call.get("transferred_to"):
        return "put_through" if call.get("transfer_result") == "answered" else "not_put_through"
    if call.get("caller_lines"):
        return "talked"
    if call["kind"] == "phone_outbound":
        return "not_reached"
    if call["status"] == "failed":
        return "failed"
    return "hung_up"


def talked(call: dict) -> bool:
    """Whether the other person said anything (to Oscar, or to whoever they were put through to)."""
    return bool(call.get("caller_lines"))


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _local(call: dict, zone) -> datetime | None:
    try:
        return datetime.fromisoformat(call["started_at"]).astimezone(zone)
    except (TypeError, ValueError):
        return None


def totals(calls: list[dict]) -> dict:
    """The headline figures for a set of calls."""
    finished = [call for call in calls if call["status"] not in OPEN_STATUSES]
    conversations = [call for call in finished if talked(call)]
    lengths = [call["duration_seconds"] for call in conversations if isinstance(call["duration_seconds"], int)]
    incoming = [call for call in finished if call["kind"] == "phone_inbound"]
    missed = [call for call in incoming if not talked(call)]
    cost = sum(float(call["cost"] or 0) for call in calls)
    todos = [call for call in finished if call.get("follow_up")]
    return {
        "calls": len(calls),
        "conversations": len(conversations),
        "conversation_rate": round(len(conversations) / len(finished), 4) if finished else None,
        "average_seconds": round(sum(lengths) / len(lengths)) if lengths else None,
        "talk_seconds": sum(lengths),
        "incoming": len(incoming),
        "missed": len(missed),
        "missed_rate": round(len(missed) / len(incoming), 4) if incoming else None,
        "cost": round(cost, 4),
        "cost_per_conversation": round(cost / len(conversations), 4) if conversations else None,
        "todos": len(todos),
        "todos_open": sum(1 for call in todos if not call.get("follow_up_done_at")),
        "on_now": len(calls) - len(finished),
    }


def _buckets(first: date, last: date, weekly: bool) -> list[date]:
    step = 7 if weekly else 1
    start = first - timedelta(days=first.weekday()) if weekly else first
    buckets = []
    while start <= last:
        buckets.append(start)
        start += timedelta(days=step)
    return buckets


def over_time(calls: list[dict], zone, first: date, last: date) -> dict:
    """Calls a day (or a week, over a year), by kind, with how many were conversations and their cost."""
    weekly = (last - first).days + 1 > LONGEST_DAILY_PERIOD
    starts = _buckets(first, last, weekly)
    rows = {start: {"incoming": 0, "outgoing": 0, "browser": 0, "conversations": 0, "cost": 0.0} for start in starts}
    names = {"phone_inbound": "incoming", "phone_outbound": "outgoing", "browser": "browser"}
    for call in calls:
        when = _local(call, zone)
        if when is None:
            continue
        day = when.date()
        start = day - timedelta(days=day.weekday()) if weekly else day
        row = rows.get(start)
        if row is None:
            continue
        row[names.get(call["kind"], "browser")] += 1
        row["conversations"] += talked(call)
        row["cost"] += float(call["cost"] or 0)
    return {
        "bucket": "week" if weekly else "day",
        "points": [
            {"date": start.isoformat(), **{**row, "cost": round(row["cost"], 4)}} for start, row in rows.items()
        ],
    }


def busy_hours(calls: list[dict], zone) -> list[list[int]]:
    """How many calls began in each hour of each weekday: 7 rows (Monday first) of 24."""
    grid = [[0] * 24 for _ in range(7)]
    for call in calls:
        when = _local(call, zone)
        if when is not None:
            grid[when.weekday()][when.hour] += 1
    return grid


def how_calls_ended(calls: list[dict]) -> list[dict]:
    counted = Counter(outcome(call) for call in calls)
    return [{"key": key, "label": label, "count": counted.get(key, 0)} for key, label in OUTCOMES]


def conversation_lengths(calls: list[dict]) -> list[dict]:
    counts = [0] * len(LENGTHS)
    for call in calls:
        seconds = call["duration_seconds"]
        if not talked(call) or not isinstance(seconds, int) or call["status"] in OPEN_STATUSES:
            continue
        for index, (_, under) in enumerate(LENGTHS):
            if under is None or seconds < under:
                counts[index] += 1
                break
    return [{"label": label, "count": count} for (label, _), count in zip(LENGTHS, counts, strict=True)]


def what_oscar_did(calls: list[dict]) -> dict:
    def total(key: str) -> int:
        return sum(call.get(key, 0) for call in calls)

    return {
        "web_searches": total("web_searches"),
        "sheet_lookups": total("sheet_lookups"),
        "put_through": sum(1 for call in calls if call.get("transferred_to")),
        "put_through_answered": sum(1 for call in calls if call.get("transfer_result") == "answered"),
        "voicemails_left": total("voicemails_left"),
        "do_not_call": total("do_not_call"),
        "summaries": sum(1 for call in calls if call.get("summary_status") == "ready"),
        "recorded": sum(1 for call in calls if (call.get("recording_seconds") or 0) > 0),
        "recorded_seconds": round(sum(float(call.get("recording_seconds") or 0) for call in calls)),
        "interrupted": total("interrupted"),
    }


def people(calls: list[dict], first_seen: dict[str, str], since: str) -> dict:
    """The phone numbers on the period's calls: how many, how many were new, and who was on most."""
    by_number: dict[str, list[dict]] = defaultdict(list)
    for call in calls:
        number = other_number(call)
        if number:
            by_number[number].append(call)
    # Most calls first; of those with as many, the one on a call most lately.
    top = sorted(by_number.items(), key=lambda item: (len(item[1]), item[1][-1]["started_at"]), reverse=True)
    top = top[:TOP_PEOPLE]
    return {
        "numbers": len(by_number),
        "new": sum(1 for number in by_number if first_seen.get(number, since) >= since),
        "repeat": sum(1 for found in by_number.values() if len(found) > 1),
        "top": [
            {
                "number": number,
                "name": next((call["caller_name"] for call in reversed(found) if call.get("caller_name")), ""),
                "calls": len(found),
                "conversations": sum(1 for call in found if talked(call)),
                "talk_seconds": sum(
                    call["duration_seconds"] for call in found if talked(call) and isinstance(call["duration_seconds"], int)
                ),
                "last_at": found[-1]["started_at"],
            }
            for number, found in top
        ],
    }


def _plain(value: object) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip().rstrip(".")
    return text[:FIELD_VALUE_CHARS]


def summary_fields(calls: list[dict], fields: list[str]) -> list[dict]:
    """What each of the owner's summary fields (Agent settings) said, most common first. The
    same answer in other capitals or with a full stop counts as one."""
    results = []
    for field in fields:
        counted: Counter[str] = Counter()
        shown: dict[str, Counter[str]] = defaultdict(Counter)
        for call in calls:
            value = _plain((call.get("details") or {}).get(field))
            if not value:
                continue
            key = value.casefold()
            counted[key] += 1
            shown[key][value] += 1
        common = counted.most_common()
        values = [{"value": shown[key].most_common(1)[0][0], "count": count} for key, count in common[:TOP_FIELD_VALUES]]
        rest = sum(count for _, count in common[TOP_FIELD_VALUES:])
        results.append({"name": field, "filled": sum(counted.values()), "values": values, "other": rest})
    return results


def call_lists(store: Store, calls: list[dict], since: str, *, everything: bool) -> list[dict]:
    """Each call list made, running or calling in the period (every list, over a year): how
    its people have got on."""
    made = Counter(call["campaign_id"] for call in calls if call.get("campaign_id"))
    talks = Counter(call["campaign_id"] for call in calls if call.get("campaign_id") and talked(call))
    found = []
    for campaign in store.list_campaigns():
        recent = campaign["created_at"] >= since or campaign["status"] == "running"
        if not everything and not made.get(campaign["id"]) and not recent:
            continue
        counts = campaign["counts"]
        found.append(
            {
                "id": campaign["id"],
                "name": campaign["name"],
                "status": campaign["status"],
                "total": campaign["total"],
                "counts": counts,
                "calls": made.get(campaign["id"], 0),
                "conversations": talks.get(campaign["id"], 0),
            }
        )
    return found


def analytics(
    store: Store, *, days: int, kind: str = "all", zone_name: str = "", now: datetime | None = None
) -> dict:
    """Everything the Analytics section shows for the last `days` days (today included) of
    calls of one kind ("all", "incoming", "outgoing" or "browser"), with the period
    before it for comparison."""
    zone = time_zone(zone_name)
    now = (now or datetime.now(timezone.utc)).astimezone(zone)
    last = now.date()
    first = last - timedelta(days=days - 1)
    since = _stamp(datetime.combine(first, time(), zone))
    before = _stamp(datetime.combine(first - timedelta(days=days), time(), zone))

    wanted = KINDS.get(kind)
    found = [call for call in store.calls_for_analytics(before) if wanted is None or call["kind"] == wanted]
    calls = [call for call in found if call["started_at"] >= since]
    earlier = [call for call in found if call["started_at"] < since]
    numbers = sorted({other_number(call) for call in calls} - {""})

    agent = store.get_agent()
    return {
        "days": days,
        "kind": kind,
        "time_zone": getattr(zone, "key", "UTC"),
        "first_day": first.isoformat(),
        "last_day": last.isoformat(),
        "totals": totals(calls),
        "previous": totals(earlier),
        "over_time": over_time(calls, zone, first, last),
        "hours": busy_hours(calls, zone),
        "outcomes": how_calls_ended(calls),
        "lengths": conversation_lengths(calls),
        "actions": what_oscar_did(calls),
        "people": people(calls, store.first_calls_with(numbers), since),
        "fields": summary_fields(calls, agent.summary_fields),
        "lists": call_lists(store, calls, since, everything=days >= max(PERIODS)) if kind in ("all", "outgoing") else [],
    }
