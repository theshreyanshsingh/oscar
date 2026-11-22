"""A phone number that has called (or been called) before: Oscar is told what those calls were about."""

from datetime import datetime, timedelta, timezone

import pytest

from app.agent import earlier_calls_for, earlier_calls_note, system_instruction
from app.store import DEFAULT_AGENT, Store

SAM = "+61412345678"
START = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path / "oscar.db")
    yield store
    store.close()


def a_call(store, kind="phone_inbound", *, days=0, summary="Sam asked for a fence quote.", **fields):
    numbers = {"from_number": SAM} if kind == "phone_inbound" else {"to_number": SAM} if kind == "phone_outbound" else {}
    call = store.create_call(kind, status="live", **{**numbers, **fields.pop("numbers", {})})
    store.update_call(call["id"], status="ended", summary_status="ready" if summary else "", summary=summary, **fields)
    stamp = (START + timedelta(days=days)).isoformat(timespec="seconds")
    store._db.execute("UPDATE calls SET started_at = ? WHERE id = ?", (stamp, call["id"]))
    return store.get_call(call["id"])


def test_a_numbers_earlier_calls_are_found_newest_first(store):
    first = a_call(store, days=1, summary="Sam asked for a fence quote.")
    called = a_call(store, "phone_outbound", days=2, summary="Oscar told Sam the quote is on its way.")
    a_call(store, days=3, summary="")  # nothing was said: no summary
    a_call(store, "browser", days=4)  # no number
    a_call(store, days=5, numbers={"from_number": "+61499999999"})  # someone else
    now = a_call(store, days=6, summary="")
    assert [c["id"] for c in earlier_calls_for(store, now["id"])] == [called["id"], first["id"]]


def test_only_the_latest_three_are_given(store):
    made = [a_call(store, days=day) for day in range(5)]
    now = a_call(store, days=9, summary="")
    assert [c["id"] for c in earlier_calls_for(store, now["id"])] == [c["id"] for c in made[:1:-1]]


def test_a_browser_call_has_no_earlier_calls(store):
    a_call(store, days=1)
    browser = a_call(store, "browser", days=2, summary="")
    assert earlier_calls_for(store, browser["id"]) == []
    assert earlier_calls_for(store, "no-such-call") == []


def test_the_note_says_what_each_call_was_about_and_what_is_left_to_do():
    note = earlier_calls_note([
        {"started_at": "2026-11-18T09:00:00+00:00", "summary": "Sam asked for a fence quote.",
         "caller_name": "Sam Taylor", "follow_up": "Call Sam back with the quote.", "follow_up_done_at": ""},
        {"started_at": "2026-11-02T09:00:00+00:00", "summary": "Sam asked about opening hours.",
         "caller_name": "", "follow_up": "Send Sam the price list.", "follow_up_done_at": "2026-11-03T10:00:00+00:00"},
    ])
    assert "- 18 November: Sam asked for a fence quote. (They gave their name as Sam Taylor.) Still to do: Call Sam back with the quote." in note
    assert "- 2 November: Sam asked about opening hours. Done since: Send Sam the price list." in note
    assert "do not assume it is the same person" in note and "Never read the notes out" in note
    assert earlier_calls_note([]) == ""


def test_the_note_comes_last_in_the_instructions_and_only_when_there_are_earlier_calls():
    earlier = [{"started_at": "2026-11-18T09:00:00+00:00", "summary": "Sam asked for a fence quote."}]
    plain = system_instruction(DEFAULT_AGENT)
    told = system_instruction(DEFAULT_AGENT, earlier=earlier)
    assert told.startswith(plain) and told.endswith(earlier_calls_note(earlier).split("\n")[-1])
    assert "You have spoken with this phone number before" not in plain
