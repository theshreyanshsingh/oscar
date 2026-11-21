"""Call history: searching, filtering and paging calls, ticking off to-dos, and voice samples."""

import io
import json
import wave
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.main import create_app
from app.preview import PreviewFailed, sample_text, voice_sample
from app.speech import Recordings
from app.store import DEFAULT_AGENT, Store
from tests.conftest import make_config
from tests.fake_openrouter import answer_stream

START = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path / "oscar.db")
    yield store
    store.close()


def add(store: Store, kind="browser", *, minutes=0, status="ended", said=(), **fields) -> dict:
    call = store.create_call(kind, status="live", from_number=fields.pop("from_number", ""), to_number=fields.pop("to_number", ""))
    for role, text in said:
        store.add_message(call["id"], role, text)
    store.update_call(call["id"], status=status, **fields)
    stamp = (START + timedelta(minutes=minutes)).isoformat(timespec="seconds")
    store._db.execute("UPDATE calls SET started_at = ? WHERE id = ?", (stamp, call["id"]))
    return store.get_call(call["id"])


def ids(found) -> list[str]:
    calls, _ = found
    return [call["id"] for call in calls]


# Finding calls


def test_calls_come_newest_first_a_page_at_a_time(store):
    made = [add(store, minutes=minute) for minute in range(5)]
    first, more = store.find_calls(limit=2)
    assert [c["id"] for c in first] == [made[4]["id"], made[3]["id"]] and more
    second, more = store.find_calls(limit=2, before=first[-1]["started_at"])
    assert [c["id"] for c in second] == [made[2]["id"], made[1]["id"]] and more
    last, more = store.find_calls(limit=2, before=second[-1]["started_at"])
    assert [c["id"] for c in last] == [made[0]["id"]] and not more


def test_calls_that_started_in_the_same_second_are_all_paged_through(store):
    made = [add(store, minutes=minute) for minute in (0, 1, 1, 1, 2)]  # three in one second
    seen, before_id, more = [], "", True
    while more:
        page, more = store.find_calls(limit=2, before_id=before_id)
        seen += [call["id"] for call in page]
        before_id = page[-1]["id"]
    assert sorted(seen) == sorted(call["id"] for call in made) and len(seen) == len(set(seen))


def test_each_filter_shows_its_own_calls(store):
    todo = add(store, "phone_inbound", follow_up="Call Sam back.", minutes=1)
    done = add(store, "phone_inbound", follow_up="Sent the quote.", follow_up_done_at="2026-11-01T10:00:00+00:00", minutes=2)
    outgoing = add(store, "phone_outbound", minutes=3)
    failed = add(store, "browser", status="failed", error="The call never connected.", minutes=4)
    browser = add(store, "browser", minutes=5)
    assert ids(store.find_calls(show="todo")) == [todo["id"]]
    assert ids(store.find_calls(show="incoming")) == [done["id"], todo["id"]]
    assert ids(store.find_calls(show="outgoing")) == [outgoing["id"]]
    assert ids(store.find_calls(show="browser")) == [browser["id"], failed["id"]]
    assert ids(store.find_calls(show="failed")) == [failed["id"]]
    assert len(ids(store.find_calls(show="all"))) == 5
    assert store.count_open_todos() == 1


def test_a_search_looks_in_the_summary_the_to_do_the_name_and_what_was_said(store):
    by_summary = add(store, summary="Asked for a quote for a fence.", minutes=1)
    by_todo = add(store, follow_up="Send the FENCE price list.", minutes=2)
    by_name = add(store, caller_name="Priya Sharma", minutes=3)
    by_words = add(store, said=[("user", "Is the fence paint dry yet?")], minutes=4)
    add(store, summary="Something else entirely.", minutes=5)
    assert set(ids(store.find_calls(query="fence"))) == {by_summary["id"], by_todo["id"], by_words["id"]}
    assert ids(store.find_calls(query="priya")) == [by_name["id"]]


def test_a_number_is_found_however_it_is_typed(store):
    sam = add(store, "phone_inbound", from_number="+61412345678", callback_number="0412345678")
    for typed in ["0412 345 678", "412-345", "+61 412 345 678", "(0412) 345678"]:
        assert ids(store.find_calls(query=typed)) == [sam["id"]], typed


def test_search_text_is_taken_literally(store):
    add(store, summary="Twenty per cent off.")
    percent = add(store, summary="Got 100% of it.")
    underscore = add(store, summary="Code A_B.")
    assert ids(store.find_calls(query="100%")) == [percent["id"]]
    assert ids(store.find_calls(query="A_B")) == [underscore["id"]]
    assert ids(store.find_calls(query="%")) == [percent["id"]]


def test_search_and_filter_go_together(store):
    add(store, "phone_inbound", summary="Fence quote.", minutes=1)
    wanted = add(store, "phone_outbound", summary="Fence follow-up.", minutes=2)
    assert ids(store.find_calls(query="fence", show="outgoing")) == [wanted["id"]]


# The dashboard


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(make_config(tmp_path)), base_url="http://localhost") as test_client:
        yield test_client


def test_call_history_says_whether_there_are_older_calls_and_how_many_to_dos_are_open(client):
    store = client.app.state.store
    for minute in range(3):
        add(store, minutes=minute, follow_up="Ring back." if minute == 0 else "")
    page = client.get("/api/calls", params={"limit": 2}).json()
    assert (len(page["calls"]), page["more"], page["open_todos"]) == (2, True, 1)
    older = client.get("/api/calls", params={"limit": 2, "before": page["calls"][-1]["started_at"]}).json()
    assert (len(older["calls"]), older["more"]) == (1, False)
    assert client.get("/api/calls", params={"show": "todo"}).json()["calls"][0]["follow_up"] == "Ring back."
    assert client.get("/api/calls", params={"show": "nonsense"}).status_code == 422
    assert client.get("/api/calls", params={"limit": 500}).status_code == 422


def test_a_to_do_is_ticked_off_and_back_on(client):
    store = client.app.state.store
    call = add(store, "phone_inbound", follow_up="Call Sam back.")
    ticked = client.put(f"/api/calls/{call['id']}/todo", json={"done": True}).json()["call"]
    assert ticked["follow_up_done_at"]
    assert client.get("/api/calls").json()["open_todos"] == 0
    reopened = client.put(f"/api/calls/{call['id']}/todo", json={"done": False}).json()["call"]
    assert reopened["follow_up_done_at"] == ""
    assert client.get("/api/calls").json()["open_todos"] == 1


def test_a_call_without_a_to_do_has_nothing_to_tick(client):
    call = add(client.app.state.store)
    assert client.put(f"/api/calls/{call['id']}/todo", json={"done": True}).status_code == 409
    assert client.put("/api/calls/nope/todo", json={"done": True}).status_code == 404


async def test_a_summary_written_again_starts_its_to_do_afresh(tmp_path, monkeypatch):
    from app import summary
    from app.calls import CallManager
    from app.events import EventBus
    from app.summary import CallSummary, start_summary

    async def write(messages, **details):
        return CallSummary(summary="Sam wants a quote.", follow_up="Send Sam the quote.")

    monkeypatch.setattr(summary, "write_summary", write)
    store = Store(tmp_path / "oscar.db")
    calls = CallManager(store, EventBus())
    call = add(store, "phone_inbound", said=[("user", "Hi, it's Sam.")], follow_up="Old to-do.", follow_up_done_at="2026-11-01T10:00:00+00:00")
    start_summary(make_config(tmp_path, openrouter_api_key="sk-or-test"), calls, call, DEFAULT_AGENT)
    import asyncio

    await asyncio.gather(*calls._tasks)
    again = store.get_call(call["id"])
    assert (again["follow_up"], again["follow_up_done_at"]) == ("Send Sam the quote.", "")
    store.close()


# Hearing a voice before it is saved

AGENT = DEFAULT_AGENT.model_copy(update={"name": "Ava", "voice": "marin", "accent": "british"})


def speaking(text, asked: list):
    def respond(request: httpx.Request) -> httpx.Response:
        asked.append(json.loads(request.content))
        body = "".join(answer_stream(text))
        return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})

    return httpx.AsyncClient(transport=httpx.MockTransport(respond))


def seconds_of(wav_bytes: bytes) -> float:
    with wave.open(io.BytesIO(wav_bytes)) as clip:
        assert (clip.getnchannels(), clip.getframerate()) == (1, 24000)
        return clip.getnframes() / clip.getframerate()


async def test_a_sample_is_said_in_the_voice_and_accent_of_the_form_and_kept(tmp_path):
    asked = []
    recordings = Recordings(tmp_path)
    first = await voice_sample(AGENT, api_key="sk-or-test", base_url="https://or.test/api/v1",
                               recordings=recordings, client=speaking(sample_text(AGENT), asked))
    assert seconds_of(first) > 0
    (body,) = asked
    assert body["audio"] == {"voice": "marin", "format": "pcm16"}
    assert "British" in body["messages"][0]["content"] and "Ava" in body["messages"][0]["content"]
    assert sample_text(AGENT) in body["messages"][-1]["content"]
    # Kept: the same sample again asks nothing.
    again = await voice_sample(AGENT, api_key="sk-or-test", base_url="https://or.test/api/v1",
                               recordings=recordings, client=speaking("unused", asked))
    assert again == first and len(asked) == 1
    assert len(list(tmp_path.glob("preview-marin-*.pcm"))) == 1


async def test_a_sample_said_otherwise_is_played_but_not_kept(tmp_path):
    asked = []
    sample = await voice_sample(AGENT, api_key="sk-or-test", base_url="https://or.test/api/v1",
                                recordings=Recordings(tmp_path), client=speaking("Something else.", asked))
    assert seconds_of(sample) > 0
    assert list(tmp_path.iterdir()) == []


async def test_a_sample_that_cannot_be_had_says_why():
    refusing = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(402, json={"error": {"message": "Insufficient credits"}})))
    with pytest.raises(PreviewFailed, match="out of credits"):
        await voice_sample(AGENT, api_key="sk-or-test", base_url="https://or.test/api/v1", client=refusing)


def test_the_dashboard_plays_a_sample_or_says_why_not(client, monkeypatch):
    form = AGENT.model_dump()

    async def sample(agent, **options):
        assert agent.voice == "marin" and options["recordings"] is not None
        return b"RIFF-a-wav"

    monkeypatch.setattr(main_module, "voice_sample", sample)
    response = client.post("/api/agent/preview", json=form)
    assert (response.status_code, response.headers["content-type"], response.content) == (200, "audio/wav", b"RIFF-a-wav")

    async def failing(agent, **options):
        raise PreviewFailed("The OpenRouter account is out of credits.")

    monkeypatch.setattr(main_module, "voice_sample", failing)
    response = client.post("/api/agent/preview", json=form)
    assert response.status_code == 502 and "out of credits" in response.json()["detail"]
    assert client.post("/api/agent/preview", json={**form, "voice": "nobody"}).status_code == 422
