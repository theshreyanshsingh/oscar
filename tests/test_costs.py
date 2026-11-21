"""What calls cost, and what is left on the OpenRouter key."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from pipecat.frames.frames import InterruptionFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.tests.utils import SleepFrame

from app.calls import CallManager
from app.costs import CALL_MINIMUM_DOLLARS, CostMeter, KeyCredit, cost_of
from app.events import EventBus
from app.main import create_app
from app.research import write_research
from app.script import ScriptKeeper
from app.search import WebLookup
from app.store import Store
from app.summary import write_summary
from tests.conftest import PUBLIC_URL, TWILIO_AUTH_TOKEN, make_config
from tests.fake_openrouter import answer_stream
from tests.test_phone import signed_post
from tests.test_speech import GreetingFrame, caller_turn, run_voice, slowly, spoken

BASE = "https://openrouter.test/api/v1"


# Reading what a request cost


@pytest.mark.parametrize(
    ("body", "cost"),
    [
        ({"usage": {"cost": 0.0015895, "prompt_tokens": 12}}, 0.0015895),
        ({"usage": {"cost": 0}}, 0.0),
        ({"usage": {"prompt_tokens": 12}}, 0.0),  # not asked for
        ({"usage": {"cost": "0.1"}}, 0.0),
        ({"usage": {"cost": True}}, 0.0),
        ({"usage": {"cost": -1}}, 0.0),
        ({}, 0.0),
        (None, 0.0),
        ("not a body", 0.0),
    ],
)
def test_the_cost_is_read_from_the_usage_openrouter_reports(body, cost):
    assert cost_of(body) == cost


def counting():
    """A meter whose costs are kept in a list, and which runs late lookups straight away."""
    added: list[float] = []
    tasks: list[asyncio.Task] = []
    generations: list[str] = []

    def generation(request: httpx.Request) -> httpx.Response:
        generations.append(request.url.params["id"])
        return httpx.Response(200, json={"data": {"id": request.url.params["id"], "total_cost": 0.0021}})

    meter = CostMeter(
        added.append,
        api_key="sk-or-test",
        base_url=BASE,
        spawn=lambda coroutine: tasks.append(asyncio.ensure_future(coroutine)),
        client=httpx.AsyncClient(transport=httpx.MockTransport(generation)),
        delays=(0.0,),
    )
    return meter, added, tasks, generations


def test_a_meter_adds_what_each_answer_says_it_cost():
    meter, added, _, _ = counting()
    assert meter.charge({"usage": {"cost": 0.002}}) == 0.002
    assert meter.charge({"usage": {}}) == 0.0
    assert added == [0.002]


async def test_a_request_cut_off_is_looked_up_once_openrouter_has_counted_it():
    asked = []

    def generation(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        if len(asked) < 3:
            return httpx.Response(404, json={"error": {"message": "Generation not found"}})
        return httpx.Response(200, json={"data": {"total_cost": 0.00003, "cancelled": True}})

    added: list[float] = []
    meter = CostMeter(
        added.append,
        api_key="sk-or-test",
        base_url=BASE,
        spawn=lambda coroutine: None,
        client=httpx.AsyncClient(transport=httpx.MockTransport(generation)),
        delays=(0.0, 0.0, 0.0, 0.0),
    )
    await meter._look_up("gen-123")
    assert added == [0.00003]
    assert len(asked) == 3  # asked again until it was there
    assert str(asked[0].url) == f"{BASE}/generation?id=gen-123"
    assert asked[0].headers["authorization"] == "Bearer sk-or-test"


async def test_a_lookup_that_never_finds_the_cost_gives_up_quietly():
    added: list[float] = []
    meter = CostMeter(
        added.append,
        api_key="sk-or-test",
        base_url=BASE,
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
        delays=(0.0, 0.0),
    )
    await meter._look_up("gen-123")
    assert added == []


def test_nothing_is_looked_up_without_an_id():
    meter, _, tasks, _ = counting()
    meter.charge_later("")
    assert tasks == []


# Every request a call makes is counted toward it


def stream_with_cost(text: str, cost: float):
    """A spoken answer whose last chunk says what it cost, as OpenRouter's do."""
    chunks = list(answer_stream(text))
    usage = f'data: {json.dumps({"id": "gen-test", "choices": [], "usage": {"cost": cost}})}\n\n'
    body = "".join(chunks[:-1]) + usage + chunks[-1]
    return lambda request: httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})


async def test_a_reply_and_its_transcript_are_counted():
    meter, added, tasks, _ = counting()

    def written(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"text": "What time do you open?", "usage": {"seconds": 1.4, "cost": 0.0002}})

    call = await run_voice(
        [*caller_turn(), SleepFrame(0.6)],
        chat=stream_with_cost("We open at nine.", 0.004),
        transcription=written,
        meter=meter,
    )
    assert call.errors == []
    assert sorted(added) == [0.0002, 0.004]
    assert tasks == []  # nothing was cut off
    assert all(body["usage"] == {"include": True} for body in call.chat + call.transcriptions)


async def test_a_reply_cut_off_part_way_is_looked_up_by_its_id():
    meter, added, tasks, generations = counting()

    def cut(request: httpx.Request) -> httpx.Response:
        response = slowly("We open at nine in the morning, every day.", gap=0.1)(request)
        response.headers["x-generation-id"] = "gen-cut"
        return response

    await run_voice([GreetingFrame("Hi."), SleepFrame(0.25), InterruptionFrame(), SleepFrame(0.3)], chat=cut, meter=meter)
    await asyncio.gather(*tasks)
    assert generations == ["gen-cut"]
    assert added == [0.0021]


async def test_the_lookup_decision_and_its_answer_are_counted():
    meter, added, _, _ = counting()

    def openrouter(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["usage"] == {"include": True}
        return httpx.Response(200, json={"choices": [{"message": {"content": "NO"}}], "usage": {"cost": 0.0019}})

    lookup = WebLookup(
        firecrawl_api_key="fc-test",
        openrouter_api_key="sk-or-test",
        openrouter_base_url=BASE,
        agent_name="Oscar",
        agent_instructions="",
        today="Friday 20 November 2026",
        knowledge="We open at nine.",
        client=httpx.AsyncClient(transport=httpx.MockTransport(openrouter)),
        meter=meter,
    )
    await lookup.decide([], {"role": "user", "content": "hello"})
    await lookup.answer_from_facts("When do you open?", [])
    assert added == [0.0019, 0.0019]


async def test_the_script_keeper_is_counted():
    meter, added, _, _ = counting()

    def openrouter(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["usage"] == {"include": True}
        return httpx.Response(200, json={"choices": [{"message": {"content": "2"}}], "usage": {"cost": 0.0004}})

    keeper = ScriptKeeper(
        ["Ask who is calling.", "Ask what they need."],
        agent_name="Oscar",
        api_key="sk-or-test",
        base_url=BASE,
        client=httpx.AsyncClient(transport=httpx.MockTransport(openrouter)),
        meter=meter,
    )
    await keeper.find_place([{"role": "user", "content": "It's Sam."}])
    assert added == [0.0004]


async def test_the_summary_and_the_research_notes_are_counted():
    meter, added, _, _ = counting()

    def openrouter(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["usage"] == {"include": True}
        answer = '{"summary": "The caller asked the time.", "notes": []}'
        return httpx.Response(200, json={"choices": [{"message": {"content": answer}}], "usage": {"cost": 0.0001}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(openrouter))
    messages = [{"role": "user", "text": "What time is it?"}, {"role": "agent", "text": "Nine."}]
    await write_summary(messages, kind="browser", agent_name="Oscar", api_key="sk-or-test", base_url=BASE, client=client, meter=meter)
    searches = [{"query": "time", "sources": [{"title": "Clock", "url": "https://clock.example", "excerpt": "Nine."}]}]
    await write_research("Caller: what time?", searches, caller_said="what time", agent_name="Oscar", api_key="sk-or-test", base_url=BASE, client=client, meter=meter)
    assert added == [0.0001, 0.0001]


# The call's total


@pytest.fixture
def calls(tmp_path):
    store = Store(tmp_path / "oscar.db")
    yield CallManager(store, EventBus())
    store.close()


def test_a_calls_cost_adds_up_and_is_shown_as_it_grows(calls):
    queue = calls.bus.subscribe()
    call = calls.create("browser")
    calls.add_cost(call["id"], 0.004)
    calls.add_cost(call["id"], 0.0021)
    calls.add_cost(call["id"], 0)  # nothing to show
    assert calls.store.get_call(call["id"])["cost"] == pytest.approx(0.0061)
    shown = []
    while not queue.empty():
        shown.append(queue.get_nowait())
    assert [event["call"]["cost"] for event in shown if event["type"] == "call"] == [0, pytest.approx(0.004), pytest.approx(0.0061)]


def test_what_calls_cost_lately_is_added_up(calls):
    old = calls.create("browser")
    recent = calls.create("browser")
    calls.add_cost(old["id"], 1.0)
    calls.add_cost(recent["id"], 0.25)
    calls.store._db.execute(
        "UPDATE calls SET started_at = ? WHERE id = ?",
        ((datetime.now(timezone.utc) - timedelta(days=2)).isoformat(timespec="seconds"), old["id"]),
    )
    now = datetime.now(timezone.utc)
    assert calls.store.calls_cost_since(now - timedelta(days=1)) == pytest.approx(0.25)
    assert calls.store.calls_cost_since(now - timedelta(days=30)) == pytest.approx(1.25)


def test_a_database_from_before_costs_gets_the_column(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    Store(path).close()
    db = sqlite3.connect(path)
    db.execute("ALTER TABLE calls DROP COLUMN cost")
    db.commit()
    db.close()
    store = Store(path)
    call = store.create_call("browser", status="ended")
    assert store.get_call(call["id"])["cost"] == 0
    store.close()


# What is left on the key


def key_answer(**data):
    full = {"limit": 50, "limit_remaining": 7.540461606, "limit_reset": "monthly", "usage_daily": 0.587388274, "usage_monthly": 42.459538394}
    return {"data": {**full, **data}}


def credit_with(answer, asked=None) -> KeyCredit:
    def respond(request: httpx.Request) -> httpx.Response:
        if asked is not None:
            asked.append(request)
        return answer(request) if callable(answer) else httpx.Response(200, json=answer)

    return KeyCredit(api_key="sk-or-test", base_url=BASE, client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))


async def test_what_the_key_has_spent_and_has_left_is_read_from_openrouter():
    asked = []
    credit = credit_with(key_answer(), asked)
    assert await credit.get() == {"limit": 50.0, "left": 7.5405, "resets": "monthly", "today": 0.5874, "this_month": 42.4595}
    await credit.get()
    assert len(asked) == 1  # kept for a while
    assert str(asked[0].url) == f"{BASE}/key" and asked[0].headers["authorization"] == "Bearer sk-or-test"
    await credit.get(fresh=True)
    assert len(asked) == 2


async def test_a_key_without_a_limit_holds_no_call_back():
    credit = credit_with(key_answer(limit=None, limit_remaining=None, limit_reset=None))
    assert (await credit.get())["left"] is None
    assert await credit.too_little_for_a_call() == ""


async def test_a_key_all_but_used_up_holds_calls_back_and_says_why():
    credit = credit_with(key_answer(limit_remaining=CALL_MINIMUM_DOLLARS / 2))
    reason = await credit.too_little_for_a_call()
    assert "has $0.03 left of its $50.00 limit" in reason
    assert "when the limit resets (monthly)" in reason


async def test_credit_left_without_a_limit_named_still_says_why():
    credit = credit_with(key_answer(limit=None, limit_remaining=0.01))
    assert "has $0.01 left. " in await credit.too_little_for_a_call()


async def test_when_openrouter_cannot_say_calls_go_ahead():
    credit = credit_with(lambda request: httpx.Response(500))
    assert await credit.get() is None
    assert await credit.too_little_for_a_call() == ""


async def test_what_was_known_is_kept_when_openrouter_stops_answering(monkeypatch):
    answers = [httpx.Response(200, json=key_answer()), httpx.Response(503)]
    credit = credit_with(lambda request: answers.pop(0))
    first = await credit.get()
    assert await credit.get(fresh=True) == first


# The dashboard, and calls held back


def app_with(tmp_path, credit: KeyCredit, **config):
    from fastapi.testclient import TestClient

    cfg = make_config(tmp_path, **config)
    return TestClient(create_app(cfg, credit=credit), base_url="http://localhost")


def test_the_dashboard_is_told_what_calls_and_the_key_cost(tmp_path):
    with app_with(tmp_path, credit_with(key_answer())) as client:
        store = client.app.state.store
        call = store.create_call("browser", status="ended")
        store.add_call_cost(call["id"], 0.0123)
        spending = client.get("/api/spending").json()
        assert client.get(f"/api/calls/{call['id']}").json()["call"]["cost"] == pytest.approx(0.0123)
    assert spending == {
        "key": {"limit": 50.0, "left": 7.5405, "resets": "monthly", "today": 0.5874, "this_month": 42.4595},
        "calls": {"last_24_hours": 0.0123, "last_30_days": 0.0123},
        "call_minimum": CALL_MINIMUM_DOLLARS,
        "recent_calls": [[call["started_at"], 0.0123]],  # for the day-by-day chart
    }


def test_no_call_starts_when_the_key_is_all_but_used_up(tmp_path):
    with app_with(tmp_path, credit_with(key_answer(limit_remaining=0.01))) as client:
        browser = client.post("/api/calls/browser", json={"sdp": "v=0", "type": "offer"})
        phone = client.post("/api/calls/phone", json={"to": "+14155550123"})
    for response in (browser, phone):
        assert response.status_code == 402
        assert "the OpenRouter key has $0.01 left of its $50.00 limit" in response.json()["detail"]


def test_an_incoming_call_is_turned_away_politely_and_kept_in_the_history(tmp_path):
    with app_with(tmp_path, credit_with(key_answer(limit_remaining=0.0))) as client:
        response = signed_post(client, "/twilio/voice", {"CallSid": "CA1", "From": "+14155550123", "To": "+15005550006"})
        assert response.status_code == 200
        assert "cannot take calls right now" in response.text and "<Hangup" in response.text
        (missed,) = client.get("/api/calls").json()["calls"]
    assert (missed["kind"], missed["status"], missed["from_number"]) == ("phone_inbound", "failed", "+14155550123")
    assert "has $0.00 left" in missed["error"]
