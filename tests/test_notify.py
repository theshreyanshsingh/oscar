"""Each call's summary, sent to the owner's webhook."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import notify, summary
from app.main import create_app
from app.notify import SAMPLE_CALL, WebhookRefused, send_summary, summary_message, summary_payload, webhook_url
from app.store import DEFAULT_AGENT
from app.summary import CallSummary, start_summary
from tests.conftest import make_config
from tests.test_summary import OPENROUTER, SAMS_CALL, SAMS_NOTE, calls, ended_call, finished, writing_with  # noqa: F401

HOOK = "https://hooks.example.test/services/T000/B000/xyz"


# The address


@pytest.mark.parametrize(
    ("given", "kept"),
    [("", ""), ("  ", ""), (f"  {HOOK} ", HOOK), ("https://example.test:8443/hook?x=1", "https://example.test:8443/hook?x=1")],
)
def test_an_https_address_is_kept(given, kept):
    assert webhook_url(given) == kept


@pytest.mark.parametrize(
    "refused",
    ["http://hooks.example.test/x", "hooks.example.test/x", "ftp://example.test", "https://", "javascript:alert(1)", "https://x.test/" + "a" * 2000,
     "https://[oops", "https://example.test:abc/hook", "https://example.test:99999/hook"],
)
def test_an_address_oscar_will_not_post_to_is_refused(refused):
    with pytest.raises(WebhookRefused):
        webhook_url(refused)


# The message


def test_the_message_reads_like_a_note_about_the_call():
    call = {
        **SAMPLE_CALL,
        "kind": "phone_inbound",
        "from_number": "+61412345678",
        "duration_seconds": 125,
        "cost": 0.0314,
        "summary": "Sam asked for a quote for a fence.",
        "caller_name": "Sam Taylor",
        "callback_number": "0412345678",
        "follow_up": "Call Sam back with a quote.",
        "research": [{"text": "a", "sources": []}, {"text": "b", "sources": []}],
    }
    assert summary_message(call, agent_name="Ava") == (
        "Ava's incoming call with +61412345678 (2:05, $0.031)\n"
        "Summary: Sam asked for a quote for a fence.\n"
        "Name: Sam Taylor\n"
        "Call back on: 0412345678\n"
        "To do: Call Sam back with a quote.\n"
        "Research: 2 notes, in Call history"
    )


def test_a_browser_call_has_no_number_and_says_only_what_it_has():
    call = {"kind": "browser", "duration_seconds": 34, "cost": 0, "summary": "Asked the time.", "research": []}
    assert summary_message(call, agent_name="Oscar") == "Oscar's browser call (0:34)\nSummary: Asked the time."


def test_the_payload_suits_chat_apps_and_automations():
    payload = summary_payload(SAMPLE_CALL, agent_name="Oscar")
    assert payload["text"] == payload["content"] == summary_message(SAMPLE_CALL, agent_name="Oscar")
    assert set(payload["call"]) == set(notify.CALL_FIELDS)
    assert payload["call"]["follow_up"] == SAMPLE_CALL["follow_up"]
    json.dumps(payload)  # all of it can be sent


# Sending


def hook(*answers):
    """A webhook that gives these answers in turn. Returns the client and what it was sent."""
    sent: list[httpx.Request] = []
    queue = list(answers)

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return httpx.AsyncClient(transport=httpx.MockTransport(respond)), sent


async def test_a_summary_the_webhook_takes_is_sent_once(monkeypatch):
    client, sent = hook(httpx.Response(200, text="ok"))
    assert await send_summary(HOOK, {"text": "hi"}, client) == ""
    assert len(sent) == 1 and json.loads(sent[0].content) == {"text": "hi"}


async def test_a_webhook_that_fails_for_a_moment_is_tried_once_more(monkeypatch):
    monkeypatch.setattr(notify, "RETRY_AFTER_SECONDS", 0)
    client, sent = hook(httpx.Response(503, text="busy"), httpx.Response(204))
    assert await send_summary(HOOK, {"text": "hi"}, client) == ""
    assert len(sent) == 2


async def test_a_webhook_that_refuses_is_not_asked_again_and_says_why(monkeypatch):
    monkeypatch.setattr(notify, "RETRY_AFTER_SECONDS", 0)
    client, sent = hook(httpx.Response(404, text="no_service"))
    assert await send_summary(HOOK, {"text": "hi"}, client) == "The webhook answered 404: no_service"
    assert len(sent) == 1


async def test_a_webhook_that_cannot_be_reached_says_so(monkeypatch):
    monkeypatch.setattr(notify, "RETRY_AFTER_SECONDS", 0)
    client, sent = hook(httpx.ConnectError("down"), httpx.ConnectError("down"))
    assert await send_summary(HOOK, {"text": "hi"}, client) == "The webhook could not be reached (ConnectError)."
    assert len(sent) == 2


# When a call ends


@pytest.fixture
def delivered(monkeypatch):
    """Summaries sent, as (address, payload), instead of posting them."""
    sent: list[tuple[str, dict]] = []

    async def send(url, payload, client=None):
        sent.append((url, payload))
        return ""

    monkeypatch.setattr(summary, "send_summary", send)
    return sent


async def test_a_calls_summary_is_sent_once_it_is_written(calls, tmp_path, monkeypatch, delivered):
    writing_with(monkeypatch, CallSummary(**SAMS_NOTE))
    calls.store.save_summary_webhook_url(HOOK)
    cfg = make_config(tmp_path, openrouter_api_key="sk-or-test", openrouter_base_url=OPENROUTER)
    call = ended_call(calls, SAMS_CALL)

    start_summary(cfg, calls, call, DEFAULT_AGENT, notify=True)
    await finished(calls)

    ((url, payload),) = delivered
    assert url == HOOK
    assert payload["call"]["id"] == call["id"]
    assert payload["call"]["follow_up"] == SAMS_NOTE["follow_up"]
    assert "Summary: Sam Taylor asked for a quote" in payload["text"]
    assert calls.store.get_summary_webhook()["last_result"] == "sent"


async def test_nothing_is_sent_without_an_address_or_when_written_again(calls, tmp_path, monkeypatch, delivered):
    writing_with(monkeypatch, CallSummary(**SAMS_NOTE))
    cfg = make_config(tmp_path, openrouter_api_key="sk-or-test", openrouter_base_url=OPENROUTER)
    start_summary(cfg, calls, ended_call(calls, SAMS_CALL), DEFAULT_AGENT, notify=True)  # no address yet
    await finished(calls)
    calls.store.save_summary_webhook_url(HOOK)
    start_summary(cfg, calls, ended_call(calls, SAMS_CALL), DEFAULT_AGENT)  # written again from the dashboard
    await finished(calls)
    assert delivered == []


async def test_a_call_where_only_oscar_spoke_is_not_sent(calls, tmp_path, monkeypatch, delivered):
    calls.store.save_summary_webhook_url(HOOK)
    cfg = make_config(tmp_path)
    only_oscar = ended_call(calls, [{"role": "agent", "text": "Hi, this is Oscar."}])
    start_summary(cfg, calls, only_oscar, DEFAULT_AGENT, notify=True)
    await finished(calls)
    assert delivered == []


async def test_a_summary_that_could_not_be_written_is_not_sent(calls, tmp_path, monkeypatch, delivered):
    writing_with(monkeypatch, summary.SummaryFailed("No credit."))
    calls.store.save_summary_webhook_url(HOOK)
    cfg = make_config(tmp_path, openrouter_api_key="sk-or-test", openrouter_base_url=OPENROUTER)
    start_summary(cfg, calls, ended_call(calls, SAMS_CALL), DEFAULT_AGENT, notify=True)
    await finished(calls)
    assert delivered == []


async def test_a_summary_that_could_not_be_sent_says_why_on_the_dashboard(calls, tmp_path, monkeypatch):
    writing_with(monkeypatch, CallSummary(**SAMS_NOTE))

    async def refuse(url, payload, client=None):
        return "The webhook answered 404: no_service"

    monkeypatch.setattr(summary, "send_summary", refuse)
    calls.store.save_summary_webhook_url(HOOK)
    cfg = make_config(tmp_path, openrouter_api_key="sk-or-test", openrouter_base_url=OPENROUTER)
    start_summary(cfg, calls, ended_call(calls, SAMS_CALL), DEFAULT_AGENT, notify=True)
    await finished(calls)
    webhook = calls.store.get_summary_webhook()
    assert webhook["last_result"] == "The webhook answered 404: no_service" and webhook["last_at"]


# The dashboard


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(make_config(tmp_path)), base_url="http://localhost") as test_client:
        yield test_client


def test_the_address_is_saved_and_shown(client):
    assert client.get("/api/summary-webhook").json() == {"url": "", "last_result": "", "last_at": ""}
    saved = client.put("/api/summary-webhook", json={"url": f" {HOOK} "}).json()
    assert saved == {"url": HOOK, "last_result": "", "last_at": ""}
    assert client.get("/api/summary-webhook").json()["url"] == HOOK
    assert client.put("/api/summary-webhook", json={"url": ""}).json()["url"] == ""


def test_an_address_that_will_not_do_is_refused_with_why(client):
    response = client.put("/api/summary-webhook", json={"url": "http://hooks.example.test/x"})
    assert response.status_code == 400
    assert "https://" in response.json()["detail"]


def test_a_test_needs_a_saved_address(client):
    response = client.post("/api/summary-webhook/test")
    assert (response.status_code, response.json()["detail"]) == (400, "Save an address first.")


def test_a_test_sends_a_made_up_summary_and_says_how_it_went(client, monkeypatch):
    sent = []

    async def send(url, payload, client=None):
        sent.append((url, payload))
        return "" if len(sent) == 1 else "The webhook answered 404: no_service"

    monkeypatch.setattr("app.main.send_summary", send)
    client.put("/api/summary-webhook", json={"url": HOOK})
    first = client.post("/api/summary-webhook/test").json()
    second = client.post("/api/summary-webhook/test").json()
    assert sent[0][0] == HOOK and "This is a test" in sent[0][1]["text"]
    assert (first["sent"], first["last_result"]) == (True, "sent")
    assert (second["sent"], second["problem"], second["last_result"]) == (
        False,
        "The webhook answered 404: no_service",
        "The webhook answered 404: no_service",
    )


def test_the_owners_own_details_are_in_the_message():
    call = {**SAMPLE_CALL, "details": {"Budget": "5000", "Preferred time": "Mornings"}}
    text = summary_message(call, agent_name="Oscar")
    assert text.splitlines()[-2:] == ["Budget: 5000", "Preferred time: Mornings"]
    assert summary_payload(call, agent_name="Oscar")["call"]["details"] == {"Budget": "5000", "Preferred time": "Mornings"}
