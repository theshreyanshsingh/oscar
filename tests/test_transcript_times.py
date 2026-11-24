"""Where each transcript line falls in its call's recording (app/calls.py, CallClock), and
the transcript as the summary webhook sends it (app/notify.py). A whole call's line times
are checked in test_call_flow.py."""

import sqlite3

import numpy as np
import pytest

from app.calls import CallClock, CallManager
from app.events import EventBus
from app.notify import SAMPLE_CALL, SAMPLE_MESSAGES, summary_payload, transcript_lines, transcript_text
from app.recording import CallRecorder
from app.store import Store
from app.summary import transcript_text as summary_transcript


def test_a_moment_is_placed_from_the_recordings_last_write():
    clock = CallClock()
    assert clock.seconds_at(1000.0) is None  # nothing known yet
    clock.began(1000.0)
    assert clock.seconds_at(1003.5) == 3.5  # not recorded: from when the audio began
    # Written two seconds at a time; the third write came in late, all at once.
    clock.reached(1002.0, 2.0)
    clock.reached(1004.0, 4.0)
    clock.reached(1006.9, 6.0)
    assert clock.seconds_at(1001.0) == 1.0  # before the first write: counted back from it
    assert clock.seconds_at(1003.0) == 3.0
    assert clock.seconds_at(1005.0) == 5.0
    assert clock.seconds_at(1006.5) == 6.0  # never past where the next write got to
    assert clock.seconds_at(1010.0) == 9.1  # after the last write: on from it
    assert clock.seconds_at(990.0) == 0.0  # never before the start


def test_the_recorder_says_how_far_it_has_got(tmp_path):
    clock = CallClock()
    recorder = CallRecorder(tmp_path, "a" * 32, 8000, clock)
    recorder._write(np.zeros(16000, dtype=np.int16).tobytes())  # 2 s
    recorder._write(np.zeros(8000, dtype=np.int16).tobytes())  # 1 s more
    assert clock._seconds == [2.0, 3.0]
    recorder.discard()


def test_a_running_calls_lines_keep_their_place_in_its_audio(tmp_path):
    store = Store(tmp_path / "oscar.db")
    bus = EventBus()
    calls = CallManager(store, bus)
    call = calls.create("browser", status="live")
    calls.add_message(call["id"], "note", "Before its clock.")
    clock = CallClock()
    clock.began(1000.0)
    calls.keep_time(call["id"], clock)
    calls.add_message(call["id"], "user", "Hi there.", said_at=1004.25)
    calls.add_message(call["id"], "agent", "Hello!", said_at=None)  # now: long after
    calls.add_message(call["id"], "person", "Jane here.", at_seconds=42.5)
    calls.stop_keeping_time(call["id"])
    calls.add_message(call["id"], "note", "After the call.")
    times = [(m["role"], m["at_seconds"]) for m in store.get_messages(call["id"])]
    assert times[0] == ("note", None)
    assert times[1] == ("user", 4.25)
    assert times[2][0] == "agent" and times[2][1] > 1_000_000  # time.time() from 1000.0
    assert times[3] == ("person", 42.5)
    assert times[4] == ("note", None)
    store.close()


def test_an_earlier_database_gets_line_times(tmp_path):
    path = tmp_path / "oscar.db"
    Store(path).close()
    db = sqlite3.connect(path)
    db.execute("ALTER TABLE messages DROP COLUMN at_seconds")  # as it used to be
    db.execute("INSERT INTO calls (id, kind, status, started_at) VALUES ('c', 'browser', 'ended', '2026-11-01')")
    db.execute("INSERT INTO messages (call_id, role, text, created_at) VALUES ('c', 'user', 'Hi', '2026-11-01')")
    db.commit()
    db.close()
    store = Store(path)
    assert store.get_messages("c")[0]["at_seconds"] is None
    assert store.add_message("c", "agent", "Hello", at_seconds=1.5)["at_seconds"] == 1.5
    assert store.get_messages("c")[1]["at_seconds"] == 1.5
    store.close()


MESSAGES = [
    {"role": "agent", "text": "Hi, Oscar here.", "at_seconds": 0.4},
    {"role": "user", "text": "Can I talk to someone?", "at_seconds": 3.0},
    {"role": "note", "text": "Put the caller through to +61412345678.", "at_seconds": 6.2},
    {"role": "person", "text": "Jane speaking.", "at_seconds": 73.9},
    {"role": "user", "text": "Older line.", "at_seconds": None},
]


def test_the_webhook_gets_the_transcript_line_by_line_and_as_text():
    call = {**SAMPLE_CALL, "kind": "phone_inbound"}
    payload = summary_payload(call, agent_name="Oscar", messages=MESSAGES)
    assert payload["call"]["transcript"] == [
        {"speaker": "Oscar", "text": "Hi, Oscar here.", "at_seconds": 0.4},
        {"speaker": "Caller", "text": "Can I talk to someone?", "at_seconds": 3.0},
        {"speaker": "note", "text": "Put the caller through to +61412345678.", "at_seconds": 6.2},
        {"speaker": "Person put through to", "text": "Jane speaking.", "at_seconds": 73.9},
        {"speaker": "Caller", "text": "Older line.", "at_seconds": None},
    ]
    assert payload["call"]["transcript_text"] == (
        "[0:00] Oscar: Hi, Oscar here.\n"
        "[0:03] Caller: Can I talk to someone?\n"
        "[0:06] (Put the caller through to +61412345678.)\n"
        "[1:13] Person put through to: Jane speaking.\n"
        "Caller: Older line."
    )
    # The message the chat apps show stays the summary.
    assert "Jane speaking" not in payload["text"]


def test_on_a_call_oscar_made_the_other_end_is_the_person_called():
    (line,) = transcript_lines([{"role": "user", "text": "Hello?"}], kind="phone_outbound", agent_name="Oscar")
    assert line["speaker"] == "Person called" and line["at_seconds"] is None
    assert transcript_text([line]) == "Person called: Hello?"


def test_the_test_summary_shows_what_a_transcript_looks_like():
    payload = summary_payload(SAMPLE_CALL, agent_name="Oscar", messages=SAMPLE_MESSAGES)
    assert len(payload["call"]["transcript"]) == len(SAMPLE_MESSAGES)
    assert payload["call"]["transcript_text"].startswith("[0:00] Oscar: Hi, thanks for calling.")
    assert payload["call"]["recording_seconds"] == SAMPLE_CALL["recording_seconds"]


def test_the_summary_model_knows_who_the_person_put_through_to_is():
    text = summary_transcript(MESSAGES[:4], kind="phone_inbound", agent_name="Oscar")
    assert "Person the caller was put through to: Jane speaking." in text


@pytest.mark.parametrize("value", ["", "not a time", None])
def test_a_missing_time_from_pipecat_is_none(value):
    from app.agent import wall_time

    assert wall_time(value) is None


def test_a_time_from_pipecat_reads_as_a_clock_time():
    from app.agent import wall_time

    assert wall_time("2026-11-24T08:00:01.250+00:00") == pytest.approx(1795507201.25)
