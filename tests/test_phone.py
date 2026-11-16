import re
import time
import xml.etree.ElementTree as ET

import pytest
from starlette.websockets import WebSocketDisconnect
from twilio.base.exceptions import TwilioRestException
from twilio.request_validator import RequestValidator

from app import calls as calls_module
from app import phone
from app.calls import AUDIO_NEVER_CONNECTED
from app.store import Store
from tests.conftest import PUBLIC_URL, TWILIO_AUTH_TOKEN, local_client, make_config


def signed_post(client, path: str, params: dict, token: str = TWILIO_AUTH_TOKEN):
    signature = RequestValidator(token).compute_signature(PUBLIC_URL + path, params)
    return client.post(path, data=params, headers={"X-Twilio-Signature": signature})


def stream_parameters(twiml: str) -> tuple[str, dict]:
    root = ET.fromstring(twiml)
    stream = root.find("./Connect/Stream")
    assert stream is not None, twiml
    params = {p.get("name"): p.get("value") for p in stream.findall("Parameter")}
    return stream.get("url"), params


def media_socket(client):
    """Open /twilio/media. Starlette's test client says Host: testserver for websockets."""
    return client.websocket_connect("/twilio/media", headers={"host": "localhost"})


INBOUND = {"CallSid": "CA" + "1" * 32, "From": "+14155550100", "To": "+15005550006"}


def test_inbound_call_returns_stream_twiml_and_logs_call(client):
    response = signed_post(client, "/twilio/voice", INBOUND)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")

    url, params = stream_parameters(response.text)
    assert url == "wss://oscar.example.test/twilio/media"
    assert set(params) == {"call_id", "token"}
    assert len(params["token"]) >= 30

    call = client.get(f"/api/calls/{params['call_id']}").json()["call"]
    assert call["kind"] == "phone_inbound"
    assert call["status"] == "connecting"
    assert call["from_number"] == "+14155550100"
    assert call["twilio_sid"] == INBOUND["CallSid"]


def test_inbound_call_with_bad_signature_is_rejected(client):
    assert signed_post(client, "/twilio/voice", INBOUND, token="wrong-token").status_code == 403
    no_signature = client.post("/twilio/voice", data=INBOUND)
    assert no_signature.status_code == 403
    assert client.get("/api/calls").json()["calls"] == []


def test_inbound_call_when_phone_not_configured(tmp_path):
    cfg = make_config(tmp_path, twilio_phone_number="")
    with local_client(cfg) as client:
        assert signed_post(client, "/twilio/voice", INBOUND).status_code == 403


def test_inbound_call_without_agent_keys_says_sorry(tmp_path):
    cfg = make_config(tmp_path, openrouter_api_key="")
    with local_client(cfg) as client:
        response = signed_post(client, "/twilio/voice", INBOUND)
        assert response.status_code == 200
        assert "<Say>" in response.text and "<Hangup" in response.text
        assert client.get("/api/calls").json()["calls"] == []


def test_status_callback_marks_unanswered_call_failed(client, cfg):
    store = Store(cfg.db_path)
    call = store.create_call("phone_outbound", status="connecting", to_number="+14155550100")
    store.update_call(call["id"], twilio_sid="CA" + "2" * 32)
    store.close()

    ringing = {"CallSid": "CA" + "2" * 32, "CallStatus": "ringing"}
    assert signed_post(client, "/twilio/status", ringing).status_code == 204
    assert client.get(f"/api/calls/{call['id']}").json()["call"]["status"] == "ringing"

    no_answer = {"CallSid": "CA" + "2" * 32, "CallStatus": "no-answer"}
    assert signed_post(client, "/twilio/status", no_answer).status_code == 204
    final = client.get(f"/api/calls/{call['id']}").json()["call"]
    assert final["status"] == "failed"
    assert final["error"] == "No one answered."

    # A late callback must not reopen or change a finished call.
    assert signed_post(client, "/twilio/status", ringing).status_code == 204
    assert client.get(f"/api/calls/{call['id']}").json()["call"]["status"] == "failed"


def test_status_callback_requires_signature(client):
    response = client.post("/twilio/status", data={"CallSid": "CA1", "CallStatus": "completed"})
    assert response.status_code == 403


class FakeTwilioCalls:
    """Stands in for twilio.rest.Client().calls: create() and calls(sid).update()."""

    def __init__(self, error: Exception | None = None, update_error: Exception | None = None):
        self.error = error
        self.update_error = update_error
        self.created = []
        self.updates = []
        self.while_creating = None  # optional hook, called with the create() kwargs

    def create(self, **kwargs):
        if self.while_creating:
            self.while_creating(kwargs)
        if self.error:
            raise self.error
        self.created.append(kwargs)
        return type("TwilioCall", (), {"sid": "CA" + "3" * 32})()

    def __call__(self, sid: str):
        fake = self

        class CallContext:
            def update(self, **kwargs):
                if fake.update_error:
                    raise fake.update_error
                fake.updates.append((sid, kwargs))

        return CallContext()


class FakeTwilioClient:
    def __init__(self, calls: FakeTwilioCalls):
        self.calls = calls


@pytest.fixture
def twilio(monkeypatch) -> FakeTwilioCalls:
    fake_calls = FakeTwilioCalls()
    monkeypatch.setattr(phone, "_twilio_client", lambda cfg: FakeTwilioClient(fake_calls))
    return fake_calls


def test_outbound_call_is_placed_with_stream_twiml(client, monkeypatch):
    fake_calls = FakeTwilioCalls()
    monkeypatch.setattr(phone, "_twilio_client", lambda cfg: FakeTwilioClient(fake_calls))

    response = client.post("/api/calls/phone", json={"to": "+14155550123"})
    assert response.status_code == 200, response.text
    call = response.json()["call"]
    assert call["kind"] == "phone_outbound"
    assert call["to_number"] == "+14155550123"
    assert call["from_number"] == "+15005550006"
    assert call["twilio_sid"] == "CA" + "3" * 32

    (request,) = fake_calls.created
    assert request["to"] == "+14155550123"
    assert request["from_"] == "+15005550006"
    assert request["status_callback"] == "https://oscar.example.test/twilio/status"
    url, params = stream_parameters(request["twiml"])
    assert url == "wss://oscar.example.test/twilio/media"
    assert params["call_id"] == call["id"]


def test_outbound_call_rejected_by_twilio(client, monkeypatch):
    error = TwilioRestException(status=400, uri="/Calls", msg="The 'To' number is not valid.")
    monkeypatch.setattr(
        phone, "_twilio_client", lambda cfg: FakeTwilioClient(FakeTwilioCalls(error))
    )
    response = client.post("/api/calls/phone", json={"to": "+14155550123"})
    assert response.status_code == 502
    assert "not valid" in response.json()["detail"]
    (call,) = client.get("/api/calls").json()["calls"]
    assert call["status"] == "failed"
    assert "not valid" in call["error"]


def _start_message(call_id: str, token: str) -> dict:
    return {
        "event": "start",
        "sequenceNumber": "1",
        "streamSid": "MZ" + "4" * 32,
        "start": {
            "streamSid": "MZ" + "4" * 32,
            "callSid": "CA" + "1" * 32,
            "accountSid": "AC" + "0" * 32,
            "customParameters": {"call_id": call_id, "token": token},
            "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
        },
    }


@pytest.mark.parametrize("token_kind", ["wrong", "missing", "reused"])
def test_media_stream_rejects_bad_tokens(client, token_kind):
    _, params = stream_parameters(signed_post(client, "/twilio/voice", INBOUND).text)
    token = {"wrong": "not-the-token", "missing": "", "reused": params["token"]}[token_kind]

    if token_kind == "reused":
        # Burn the token first, as a finished call would have.
        assert client.app.state.calls.redeem_phone_token(params["call_id"], params["token"])

    with media_socket(client) as ws:
        ws.send_json({"event": "connected", "protocol": "Call", "version": "1.0.0"})
        ws.send_json(_start_message(params["call_id"], token))
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    assert closed.value.code == 1008


def test_media_stream_without_start_message_is_closed(client):
    with media_socket(client) as ws:
        ws.send_text("not json")
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    assert closed.value.code == 1008


def test_twiml_escapes_values(cfg):
    twiml = phone.stream_twiml(cfg, call_id='a"b<c', token="t&k")
    _, params = stream_parameters(twiml)
    assert params == {"call_id": 'a"b<c', "token": "t&k"}
    assert re.search(r'url="wss://oscar\.example\.test/twilio/media"', twiml)


# Calls that end, or never connect, before their audio starts


def _connect_stream(client, call_id: str, token: str) -> int:
    """Open the media stream like Twilio does and return the close code."""
    with media_socket(client) as ws:
        ws.send_json({"event": "connected", "protocol": "Call", "version": "1.0.0"})
        ws.send_json(_start_message(call_id, token))
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    return closed.value.code


def test_ended_call_cannot_start_its_audio(client, twilio):
    _, params = stream_parameters(signed_post(client, "/twilio/voice", INBOUND).text)
    assert client.post(f"/api/calls/{params['call_id']}/end").json() == {"ended": True}
    # Twilio is told to hang up, whether the call is ringing or answered.
    assert twilio.updates == [(INBOUND["CallSid"], {"status": "completed"})]

    # Even with the right token, the stream is refused and no agent runs.
    assert _connect_stream(client, params["call_id"], params["token"]) == 1008
    call = client.get(f"/api/calls/{params['call_id']}").json()["call"]
    assert call["status"] == "ended"


def test_call_whose_audio_never_connects_is_marked_failed(client, monkeypatch):
    monkeypatch.setattr(calls_module, "PHONE_TOKEN_TTL_SECONDS", 0.2)
    _, params = stream_parameters(signed_post(client, "/twilio/voice", INBOUND).text)
    time.sleep(0.6)
    call = client.get(f"/api/calls/{params['call_id']}").json()["call"]
    assert call["status"] == "failed"
    assert call["error"] == AUDIO_NEVER_CONNECTED
    # The expired token no longer opens the stream.
    assert _connect_stream(client, params["call_id"], params["token"]) == 1008


def test_wrong_token_does_not_burn_the_real_one(client):
    _, params = stream_parameters(signed_post(client, "/twilio/voice", INBOUND).text)
    assert not client.app.state.calls.redeem_phone_token(params["call_id"], "guess")
    assert client.app.state.calls.redeem_phone_token(params["call_id"], params["token"])


def test_answered_call_without_audio_is_marked_failed(client, cfg):
    store = Store(cfg.db_path)
    call = store.create_call("phone_outbound", status="ringing", to_number="+14155550100")
    store.update_call(call["id"], twilio_sid="CA" + "6" * 32)
    store.close()
    completed = {"CallSid": "CA" + "6" * 32, "CallStatus": "completed"}
    assert signed_post(client, "/twilio/status", completed).status_code == 204
    final = client.get(f"/api/calls/{call['id']}").json()["call"]
    assert final["status"] == "failed"
    assert final["error"] == AUDIO_NEVER_CONNECTED


def test_outbound_call_ended_while_twilio_is_placing_it(client, twilio):
    def end_it_meanwhile(request):
        _, params = stream_parameters(request["twiml"])
        client.app.state.calls.finish(params["call_id"])

    twilio.while_creating = end_it_meanwhile
    response = client.post("/api/calls/phone", json={"to": "+14155550123"})
    assert response.status_code == 200
    assert response.json()["call"]["status"] == "ended"
    # The call Twilio had just placed is hung up straight away.
    assert twilio.updates == [("CA" + "3" * 32, {"status": "completed"})]


def test_outbound_call_when_twilio_is_unreachable(client, monkeypatch):
    unreachable = FakeTwilioCalls(error=ConnectionError("Network is unreachable"))
    monkeypatch.setattr(phone, "_twilio_client", lambda cfg: FakeTwilioClient(unreachable))
    response = client.post("/api/calls/phone", json={"to": "+14155550123"})
    assert response.status_code == 502
    assert "Could not reach Twilio" in response.json()["detail"]
    (call,) = client.get("/api/calls").json()["calls"]
    assert call["status"] == "failed"


def test_end_call_still_closes_it_when_twilio_hang_up_fails(client, twilio):
    twilio.update_error = ConnectionError("Network is unreachable")
    _, params = stream_parameters(signed_post(client, "/twilio/voice", INBOUND).text)
    response = client.post(f"/api/calls/{params['call_id']}/end")
    assert response.status_code == 502
    assert response.json()["detail"].startswith("The call was closed here, but Twilio")
    call = client.get(f"/api/calls/{params['call_id']}").json()["call"]
    assert call["status"] == "ended"


@pytest.mark.parametrize("bad_message", ["[1, 2]", '"text"', "42", '{"event": "start", "start": []}'])
def test_media_stream_ignores_malformed_messages(client, bad_message):
    with media_socket(client) as ws:
        ws.send_text(bad_message)
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    assert closed.value.code == 1008


def test_media_stream_with_non_object_parameters(client):
    start = _start_message("x", "y")
    start["start"]["customParameters"] = ["not", "a", "dict"]
    with media_socket(client) as ws:
        ws.send_json(start)
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    assert closed.value.code == 1008
