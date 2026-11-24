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


@pytest.mark.parametrize("missing", ["twilio_account_sid", "twilio_auth_token", "public_url"])
def test_inbound_call_when_phone_not_configured(tmp_path, missing):
    cfg = make_config(tmp_path, **{missing: ""})
    with local_client(cfg) as client:
        assert signed_post(client, "/twilio/voice", INBOUND).status_code == 403


def test_calls_come_in_without_a_number_in_the_env_file(tmp_path):
    # Any number of the account that is connected to Oscar is answered (app/numbers.py).
    with local_client(make_config(tmp_path, twilio_phone_number="")) as client:
        response = signed_post(client, "/twilio/voice", {**INBOUND, "To": "+61255550199"})
        _, params = stream_parameters(response.text)
        call = client.get(f"/api/calls/{params['call_id']}").json()["call"]
        assert call["to_number"] == "+61255550199"


def test_calls_out_come_from_the_number_chosen_in_the_dashboard(client, twilio):
    client.app.state.store.set_calls_from("+61255550199")
    call = client.post("/api/calls/phone", json={"to": "+14155550123"}).json()["call"]
    assert call["from_number"] == "+61255550199"
    assert twilio.created[0]["from_"] == "+61255550199"


def test_without_a_number_to_call_from_oscar_says_how_to_choose_one(tmp_path, monkeypatch):
    fake = FakeTwilioCalls()
    monkeypatch.setattr(phone, "_twilio_client", lambda cfg: FakeTwilioClient(fake))
    with local_client(make_config(tmp_path, twilio_phone_number="")) as client:
        response = client.post("/api/calls/phone", json={"to": "+14155550123"})
        assert response.status_code == 400
        assert response.json()["detail"] == phone.NO_NUMBER_TO_CALL_FROM
        assert fake.created == [] and client.get("/api/calls").json()["calls"] == []


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


def test_a_status_update_that_beats_the_call_sid_still_finds_its_call(client, cfg):
    store = Store(cfg.db_path)
    call = store.create_call("phone_outbound", status="connecting", to_number="+14155550100")
    other = store.create_call("phone_inbound", status="live", from_number="+14155550199")
    store.close()

    busy = {"CallSid": "CA" + "4" * 32, "CallStatus": "busy"}
    assert signed_post(client, f"/twilio/status?call_id={call['id']}", busy).status_code == 204
    final = client.get(f"/api/calls/{call['id']}").json()["call"]
    assert (final["status"], final["twilio_sid"]) == ("failed", "CA" + "4" * 32)
    # Only an outgoing call still waiting for its SID can be named this way.
    assert signed_post(client, f"/twilio/status?call_id={other['id']}", {**busy, "CallSid": "CA" + "5" * 32}).status_code == 204
    assert client.get(f"/api/calls/{other['id']}").json()["call"]["status"] == "live"


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
    assert request["status_callback"] == f"https://oscar.example.test/twilio/status?call_id={call['id']}"
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


@pytest.mark.parametrize(
    ("status", "code", "msg", "stops_lists", "try_later"),
    [
        (401, 20003, "Authenticate", True, False),
        (400, 21210, "The source phone number provided, +15005550006, is not yet verified for your account.", True, False),
        (400, 21212, "Invalid From Number (caller ID): +15005550006", True, False),
        (401, 20005, "Account not active", True, False),
        (400, 21219, "The number +14155550123 is unverified. Trial accounts may only make calls to verified numbers.", False, False),
        (400, 21215, "Account not authorized to call +14155550123.", False, False),
        (429, 20429, "Too Many Requests", False, True),
    ],
)
def test_a_refused_call_says_whether_it_is_the_account_or_the_person(tmp_path, monkeypatch, status, code, msg, stops_lists, try_later):
    import asyncio

    from app.calls import CallManager
    from app.events import EventBus
    from app.numbers import KEYS_REFUSED

    cfg = make_config(tmp_path)
    calls = CallManager(Store(cfg.db_path), EventBus())
    error = TwilioRestException(status=status, uri="/Calls", msg=f"Unable to create record: {msg}", code=code)
    monkeypatch.setattr(phone, "_twilio_client", lambda cfg: FakeTwilioClient(FakeTwilioCalls(error)))
    with pytest.raises(phone.PhoneCallError) as refused:
        asyncio.run(phone.place_outbound_call(cfg, calls, "+14155550123"))
    assert (refused.value.stops_lists, refused.value.try_later) == (stops_lists, try_later)
    if code == 20003:
        # Twilio's "Authenticate" (or "auth account ... does not exist") says nothing about the fix.
        assert str(refused.value) == KEYS_REFUSED
    else:
        assert str(refused.value) == f"Twilio refused the call: {msg.rstrip('.')}."


def test_a_call_refused_in_twilios_newer_form_is_explained_in_its_words(client, monkeypatch):
    from twilio.base.exceptions import TwilioServiceException

    error = TwilioServiceException(
        type_uri="https://www.twilio.com/docs/errors/21215", title="Geo permission",
        status=400, code=21215, detail="Account not authorized to call +14155550123", method="POST", uri="/Calls",
    )
    monkeypatch.setattr(phone, "_twilio_client", lambda cfg: FakeTwilioClient(FakeTwilioCalls(error)))
    response = client.post("/api/calls/phone", json={"to": "+14155550123"})
    assert response.status_code == 502
    assert response.json()["detail"] == "Twilio refused the call: Account not authorized to call +14155550123."


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


def _unreachable() -> Exception:
    """What requests raises when Twilio's address cannot be reached at all."""
    import requests
    import urllib3

    reason = urllib3.exceptions.NewConnectionError(None, "Failed to establish a new connection: Network is unreachable")
    return requests.exceptions.ConnectionError(urllib3.exceptions.MaxRetryError(None, "/Calls.json", reason))


def test_a_call_twilio_may_have_placed_is_left_for_twilio_to_settle(client, monkeypatch):
    import requests

    slow = FakeTwilioCalls(error=requests.exceptions.ReadTimeout("read timed out"))
    monkeypatch.setattr(phone, "_twilio_client", lambda cfg: FakeTwilioClient(slow))
    response = client.post("/api/calls/phone", json={"to": "+14155550123"})
    assert response.status_code == 502
    assert "may still go through" in response.json()["detail"]
    (call,) = client.get("/api/calls").json()["calls"]
    # Still open, with its stream token: Twilio's updates (or the token's expiry) close it.
    assert call["status"] == "connecting"


def test_outbound_call_when_twilio_is_unreachable(client, monkeypatch):
    unreachable = FakeTwilioCalls(error=_unreachable())
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


# Voicemail on calls Oscar makes, and putting callers through to a person


def test_calls_oscar_makes_listen_for_a_voicemail(client, twilio):
    call = client.post("/api/calls/phone", json={"to": "+14155550123"}).json()["call"]
    (request,) = twilio.created
    assert request["machine_detection"] == "DetectMessageEnd"
    assert request["async_amd"] == "true"
    assert request["async_amd_status_callback"] == f"https://oscar.example.test/twilio/answered-by?call_id={call['id']}"


def _outbound_call(cfg, sid: str = "CA" + "7" * 32) -> dict:
    store = Store(cfg.db_path)
    call = store.create_call("phone_outbound", status="live", to_number="+14155550123")
    store.update_call(call["id"], twilio_sid=sid)
    store.close()
    return call


@pytest.mark.parametrize("answer", ["machine_end_beep", "machine_end_silence", "machine_end_other"])
def test_a_voicemail_answering_has_the_call_leave_its_message(client, cfg, answer):
    call = _outbound_call(cfg)
    left = []

    async def leave():
        left.append(call["id"])

    client.app.state.calls.on_voicemail(call["id"], leave)
    form = {"CallSid": "CA" + "7" * 32, "AnsweredBy": answer}
    assert signed_post(client, f"/twilio/answered-by?call_id={call['id']}", form).status_code == 204
    time.sleep(0.1)
    assert left == [call["id"]]
    assert client.get(f"/api/calls/{call['id']}").json()["call"]["answered_by"] == "voicemail"


@pytest.mark.parametrize("answer", ["human", "unknown"])
def test_a_person_answering_changes_nothing(client, cfg, answer):
    call = _outbound_call(cfg)
    left = []

    async def leave():
        left.append(call["id"])

    client.app.state.calls.on_voicemail(call["id"], leave)
    form = {"CallSid": "CA" + "7" * 32, "AnsweredBy": answer}
    assert signed_post(client, f"/twilio/answered-by?call_id={call['id']}", form).status_code == 204
    assert left == []
    assert client.get(f"/api/calls/{call['id']}").json()["call"]["answered_by"] == ""


def test_a_fax_answering_is_hung_up_on(client, cfg, twilio):
    call = _outbound_call(cfg)
    form = {"CallSid": "CA" + "7" * 32, "AnsweredBy": "fax"}
    assert signed_post(client, f"/twilio/answered-by?call_id={call['id']}", form).status_code == 204
    assert twilio.updates == [("CA" + "7" * 32, {"status": "completed"})]
    detail = client.get(f"/api/calls/{call['id']}").json()
    assert detail["call"]["answered_by"] == "fax"
    assert detail["messages"][-1]["text"] == "A fax machine answered, so the call was ended."


def test_what_answered_needs_twilios_signature(client, cfg):
    call = _outbound_call(cfg)
    form = {"CallSid": "CA" + "7" * 32, "AnsweredBy": "machine_end_beep"}
    assert client.post(f"/twilio/answered-by?call_id={call['id']}", data=form).status_code == 403
    assert client.get(f"/api/calls/{call['id']}").json()["call"]["answered_by"] == ""


def test_putting_a_call_through_hands_it_to_a_dial(cfg, twilio):
    import asyncio

    asyncio.run(phone.put_call_through(cfg, "abc123", "CA" + "8" * 32, "+61412345678"))
    ((sid, update),) = twilio.updates
    assert sid == "CA" + "8" * 32
    dial = ET.fromstring(update["twiml"]).find("./Dial")
    assert dial.text == "+61412345678"
    assert dial.get("action") == "https://oscar.example.test/twilio/put-through?call_id=abc123"
    assert dial.get("timeout") == str(phone.TRANSFER_RING_SECONDS)
    assert dial.get("record") is None  # calls are not recorded: nor is this part


def test_a_recorded_call_put_through_is_recorded_on_two_channels(cfg, twilio):
    import asyncio

    asyncio.run(phone.put_call_through(cfg, "abc123", "CA" + "8" * 32, "+61412345678", record=True))
    ((_, update),) = twilio.updates
    dial = ET.fromstring(update["twiml"]).find("./Dial")
    assert dial.get("record") == "record-from-answer-dual"
    assert dial.get("recordingStatusCallback") == "https://oscar.example.test/twilio/put-through-recording?call_id=abc123"
    assert (dial.get("recordingStatusCallbackMethod"), dial.get("recordingStatusCallbackEvent")) == ("POST", "completed")


RECORDED = {
    "AccountSid": "AC" + "0" * 32,
    "CallSid": "CA" + "1" * 32,
    "RecordingSid": "RE" + "3" * 32,
    "RecordingUrl": "https://api.twilio.com/2010-04-01/Accounts/AC" + "0" * 32 + "/Recordings/RE" + "3" * 32,
    "RecordingStatus": "completed",
    "RecordingChannels": "2",
    "RecordingDuration": "75",
}


def test_twilios_recording_of_the_part_put_through_is_kept_once(client, cfg, monkeypatch):
    kept = []

    async def keep(cfg, calls, call_id, sid, url):
        kept.append((call_id, sid, url))

    monkeypatch.setattr(phone, "keep_put_through", keep)
    call = _put_through_call(cfg)
    path = f"/twilio/put-through-recording?call_id={call['id']}"
    assert client.post(path, data=RECORDED).status_code == 403  # not signed by Twilio
    assert signed_post(client, path, RECORDED).status_code == 204
    assert signed_post(client, path, RECORDED).status_code == 204  # told twice
    time.sleep(0.1)
    assert kept == [(call["id"], RECORDED["RecordingSid"], RECORDED["RecordingUrl"])]


def test_a_recording_twilio_could_not_make_is_noted(client, cfg, monkeypatch):
    monkeypatch.setattr(phone, "keep_put_through", lambda *args: pytest.fail("nothing to keep"))
    call = _put_through_call(cfg)
    form = {**RECORDED, "RecordingStatus": "failed"}
    assert signed_post(client, f"/twilio/put-through-recording?call_id={call['id']}", form).status_code == 204
    detail = client.get(f"/api/calls/{call['id']}").json()
    assert detail["messages"][-1]["text"] == "Twilio could not record the call after it was put through."


def test_the_recording_of_a_deleted_call_is_deleted_from_twilio(client, cfg, monkeypatch):
    forgotten = []

    async def forget(cfg, sid):
        forgotten.append(sid)

    monkeypatch.setattr(phone, "forget_on_twilio", forget)
    assert signed_post(client, "/twilio/put-through-recording?call_id=" + "f" * 32, RECORDED).status_code == 204
    time.sleep(0.1)
    assert forgotten == [RECORDED["RecordingSid"]]


def test_a_call_twilio_will_not_put_through_says_why(cfg, monkeypatch):
    import asyncio

    error = TwilioRestException(status=400, uri="/Calls", msg="Call is not in-progress.")
    monkeypatch.setattr(phone, "_twilio_client", lambda cfg: FakeTwilioClient(FakeTwilioCalls(update_error=error)))
    with pytest.raises(phone.PhoneCallError, match="not in-progress"):
        asyncio.run(phone.put_call_through(cfg, "abc123", "CA" + "8" * 32, "+61412345678"))


def _put_through_call(cfg) -> dict:
    store = Store(cfg.db_path)
    call = store.create_call("phone_inbound", status="ended", from_number="+14155550100")
    store.update_call(call["id"], twilio_sid=INBOUND["CallSid"], transferred_to="+61412345678")
    store.close()
    return call


def test_a_caller_put_through_and_answered_is_noted(client, cfg):
    call = _put_through_call(cfg)
    form = {"CallSid": INBOUND["CallSid"], "DialCallStatus": "completed", "DialCallDuration": "125"}
    response = signed_post(client, f"/twilio/put-through?call_id={call['id']}", form)
    assert response.status_code == 200
    assert ET.fromstring(response.text).findall("*") == []  # nothing more: the call ends there
    detail = client.get(f"/api/calls/{call['id']}").json()
    assert detail["call"]["transfer_result"] == "answered"
    assert detail["messages"][-1]["text"] == "Put through to +61412345678: they talked for 2 min 5 s."


@pytest.mark.parametrize(
    "status, result", [("no-answer", "no one answered"), ("busy", "the line was busy"), ("failed", "the call could not be made")]
)
def test_a_caller_put_through_to_nobody_hears_so_and_the_owner_is_told(client, cfg, monkeypatch, status, result):
    alerts = []
    monkeypatch.setattr(phone, "alert_owner", lambda calls, payload: alerts.append(payload))
    call = _put_through_call(cfg)
    form = {"CallSid": INBOUND["CallSid"], "DialCallStatus": status}
    response = signed_post(client, f"/twilio/put-through?call_id={call['id']}", form)
    root = ET.fromstring(response.text)
    assert root.find("./Say").text == phone.NOBODY_THERE_LINE
    assert root.find("./Hangup") is not None
    detail = client.get(f"/api/calls/{call['id']}").json()
    assert detail["call"]["transfer_result"] == result
    assert detail["messages"][-1]["text"] == f"Put through to +61412345678, but {result}."
    (alert,) = alerts
    assert alert["event"] == "missed_call"
    assert alert["text"] == (
        f"Oscar tried to put +14155550100 through to +61412345678, but {result}. "
        "They were told someone would call them back."
    )


def test_the_media_stream_gives_the_call_a_way_to_put_it_through(client, monkeypatch, twilio):
    ran = {}

    async def fake_run_call(**kwargs):
        ran.update(kwargs)
        await kwargs["transfer"]("+61412345678")

    monkeypatch.setattr(phone.voice_agent, "run_call", fake_run_call)
    _, params = stream_parameters(signed_post(client, "/twilio/voice", INBOUND).text)
    with media_socket(client) as ws:
        ws.send_json({"event": "connected", "protocol": "Call", "version": "1.0.0"})
        ws.send_json(_start_message(params["call_id"], params["token"]))
        time.sleep(0.3)
    ((sid, update),) = twilio.updates
    assert sid == INBOUND["CallSid"] and "<Dial" in update["twiml"]
    # Calls are recorded (Agent settings, by default), so the part put through is too.
    assert 'record="record-from-answer-dual"' in update["twiml"]
    # Oscar's side stopping must not hang up the call Twilio is putting through.
    serializer = ran["transport"]._params.serializer
    assert serializer._params.auto_hang_up is False


async def test_two_callers_at_once_cannot_both_take_the_last_line(tmp_path):
    """MAX_CALLS holds when calls come in together: the wait for the key's credit came
    between the check for a free line and the call being made."""
    import asyncio

    import httpx

    from app.main import create_app

    class SlowCredit:
        async def too_little_for_a_call(self):
            await asyncio.sleep(0.05)
            return ""

        async def aclose(self):
            pass

    cfg = make_config(tmp_path, max_calls=1)
    app = create_app(cfg, credit=SlowCredit())

    def callers(n):
        params = {"CallSid": "CA" + str(n) * 32, "From": f"+6141234567{n}", "To": "+15005550006"}
        signature = RequestValidator(TWILIO_AUTH_TOKEN).compute_signature(PUBLIC_URL + "/twilio/voice", params)
        return params, {"X-Twilio-Signature": signature}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as http:
        answers = await asyncio.gather(
            *(http.post("/twilio/voice", data=params, headers=headers) for params, headers in map(callers, (1, 2, 3)))
        )
    assert all(answer.status_code == 200 for answer in answers)
    streamed = [answer for answer in answers if "<Stream" in answer.text]
    assert len(streamed) == 1  # the other two were told the lines are busy
    app.state.store.close()


@pytest.mark.parametrize("status, placed_maybe", [(503, False), (500, True), (502, True), (504, True)])
def test_a_twilio_server_error_is_not_the_persons_doing(cfg, monkeypatch, status, placed_maybe):
    import asyncio

    from app.calls import CallManager
    from app.events import EventBus

    error = TwilioRestException(status=status, uri="/Calls", msg="Twilio is having trouble.")
    monkeypatch.setattr(phone, "_twilio_client", lambda cfg: FakeTwilioClient(FakeTwilioCalls(error=error)))
    store = Store(cfg.db_path)
    calls = CallManager(store, EventBus())

    async def place():
        return await phone.place_outbound_call(cfg, calls, "+14155550123")

    with pytest.raises(phone.PhoneCallError) as raised:
        asyncio.run(place())
    if placed_maybe:
        # It may have gone through: the call stays open for Twilio's updates to settle.
        assert raised.value.call_id and store.get_call(raised.value.call_id)["status"] == "connecting"
    else:
        # Twilio could not take it now: the list tries the person again later.
        assert raised.value.try_later and not raised.value.call_id
    store.close()


def test_twilios_webhooks_read_only_a_small_form(client, cfg):
    call = _put_through_call(cfg)
    path = f"/twilio/put-through?call_id={call['id']}"
    form = {"CallSid": INBOUND["CallSid"], "DialCallStatus": "completed"}
    signature = RequestValidator(TWILIO_AUTH_TOKEN).compute_signature(PUBLIC_URL + path, form)
    # A body of files, even signed for its fields, is refused before it is read.
    files = client.post(path, data=form, files={"x": ("x.bin", b"0" * 1000)}, headers={"X-Twilio-Signature": signature})
    assert files.status_code == 403
    huge = client.post(path, data={**form, "Padding": "x" * (phone.TWILIO_FORM_MOST_BYTES + 1)}, headers={"X-Twilio-Signature": signature})
    assert huge.status_code == 403
    assert signed_post(client, path, form).status_code == 200
