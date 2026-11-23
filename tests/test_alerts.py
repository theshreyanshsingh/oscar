"""Missed calls and a key running low, sent to the owner's webhook (app/summary.py)."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import summary
from app.costs import KeyCredit
from app.summary import alert_if_low_credit, alert_if_missed
from tests.conftest import local_client, make_config
from tests.test_costs import key_answer
from tests.test_notify import HOOK, delivered  # noqa: F401
from tests.test_phone import signed_post
from tests.test_summary import calls, finished  # noqa: F401

CALLER = {"CallSid": "CA" + "6" * 32, "From": "+61412345678", "To": "+15005550006"}


def credit_with(**answer) -> KeyCredit:
    return KeyCredit(
        api_key="sk-or-test",
        base_url="https://or.test/api/v1",
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=key_answer(**answer)))),
    )


# Missed calls


async def test_an_incoming_call_no_one_spoke_on_is_sent(calls, delivered):
    calls.store.save_summary_webhook_url(HOOK)
    call = calls.create("phone_inbound", status="live", from_number="+61412345678")
    calls.finish(call["id"], "The call audio never connected.")
    alert_if_missed(calls, calls.store.get_call(call["id"]), "Oscar")
    await finished(calls)
    ((url, payload),) = delivered
    assert url == HOOK and payload["event"] == "missed_call"
    assert payload["text"] == "Oscar missed a call from +61412345678: The call audio never connected."
    assert payload["call"]["id"] == call["id"]
    assert calls.store.get_summary_webhook()["last_result"] == "sent"


async def test_a_call_the_caller_spoke_on_or_one_not_coming_in_is_not_missed(calls, delivered):
    calls.store.save_summary_webhook_url(HOOK)
    spoke = calls.create("phone_inbound", status="live", from_number="+61412345678")
    calls.add_message(spoke["id"], "user", "Hi, it's Sam.")
    out = calls.create("phone_outbound", status="live", to_number="+61412345678")
    browser = calls.create("browser", status="live")
    for call in (spoke, out, browser):
        alert_if_missed(calls, calls.store.get_call(call["id"]), "Oscar")
    await finished(calls)
    assert delivered == []


async def test_nothing_is_sent_with_alerts_off_or_no_address(calls, delivered):
    call = calls.create("phone_inbound", status="failed", from_number="+61412345678")
    alert_if_missed(calls, call, "Oscar")  # no address yet
    calls.store.save_summary_webhook_url(HOOK, alerts=False)
    alert_if_missed(calls, call, "Oscar")
    await finished(calls)
    assert delivered == []


def test_a_caller_turned_away_when_the_lines_are_busy_is_sent(tmp_path, monkeypatch):
    sent = []

    async def send(url, payload, client=None):
        sent.append(payload)
        return ""

    monkeypatch.setattr(summary, "send_summary", send)
    with local_client(make_config(tmp_path, max_calls=1)) as client:
        client.app.state.store.save_summary_webhook_url(HOOK)
        client.app.state.store.create_call("browser", status="live")
        signed_post(client, "/twilio/voice", CALLER)
    (payload,) = sent
    assert payload["text"].startswith("Oscar missed a call from +61412345678: Oscar is already on 1 call")


def test_twilios_update_after_a_missed_call_ended_sends_no_second_alert(tmp_path, monkeypatch):
    sent = []

    async def send(url, payload, client=None):
        sent.append(payload)
        return ""

    monkeypatch.setattr(summary, "send_summary", send)
    with local_client(make_config(tmp_path)) as client:
        store = client.app.state.store
        store.save_summary_webhook_url(HOOK)
        # Ended (and alerted for) by the agent: the caller hung up during the greeting.
        ended = store.create_call("phone_inbound", status="live", from_number="+61412345678")
        store.update_call(ended["id"], twilio_sid="CA" + "4" * 32)
        store.update_call(ended["id"], status="failed", error="The caller hung up.")
        # Never got its audio: only Twilio's update closes it.
        stuck = store.create_call("phone_inbound", status="connecting", from_number="+61498765432")
        store.update_call(stuck["id"], twilio_sid="CA" + "5" * 32)
        for sid in ("CA" + "4" * 32, "CA" + "4" * 32, "CA" + "5" * 32, "CA" + "5" * 32):
            signed_post(client, "/twilio/status", {"CallSid": sid, "CallStatus": "completed"})
    (payload,) = sent
    assert payload["text"].startswith("Oscar missed a call from +61498765432")


# A key running low


async def test_a_key_running_low_is_sent_once_a_day(calls, delivered):
    calls.store.save_summary_webhook_url(HOOK)
    credit = credit_with(limit_remaining=0.84)
    assert await alert_if_low_credit(calls, credit, "Oscar") is True
    await finished(calls)
    ((_, payload),) = delivered
    assert payload["event"] == "low_credit"
    assert payload["text"].startswith("Oscar's OpenRouter key has $0.84 left of its $50.00 limit. It resets monthly.")
    # Not again the same day ...
    assert await alert_if_low_credit(calls, credit, "Oscar") is False
    # ... but again the next.
    yesterday = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(timespec="seconds")
    calls.store._db.execute("UPDATE summary_webhook SET low_credit_alerted_at = ?", (yesterday,))
    assert await alert_if_low_credit(calls, credit, "Oscar") is True


@pytest.mark.parametrize("answer", [{"limit_remaining": 5.0}, {"limit": None, "limit_remaining": None}])
async def test_a_key_with_enough_left_or_no_limit_sends_nothing(calls, delivered, answer):
    calls.store.save_summary_webhook_url(HOOK)
    assert await alert_if_low_credit(calls, credit_with(**answer), "Oscar") is False
    await finished(calls)
    assert delivered == []
