"""Real phone calls through Twilio.

Inbound:  Twilio calls POST /twilio/voice when someone dials your number.
Outbound: the dashboard (or a call list) calls place_outbound_call().
Both answer with TwiML that streams the call audio to the /twilio/media
WebSocket, where the voice agent takes over.

On a call Oscar makes, Twilio listens for a voicemail (answering machine
detection) and says what answered at /twilio/answered-by. A caller who asks for
a person is handed to a <Dial> (put_call_through), and how that went comes to
/twilio/put-through. When calls are recorded, Twilio records that part and says
when it is ready at /twilio/put-through-recording (app/put_through.py).

Every request from Twilio is checked against its X-Twilio-Signature, and the
media stream must present a one-time token minted for that call, so nobody
else can start or hijack a call.
"""

import asyncio
import json
import re
import socket

import requests
import urllib3
from fastapi import APIRouter, Request, Response, WebSocket, WebSocketDisconnect
from loguru import logger
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
from twilio.request_validator import RequestValidator
from twilio.rest import Client
from twilio.twiml.voice_response import Connect, VoiceResponse

from app import agent as voice_agent
from app.calls import AUDIO_NEVER_CONNECTED, CallManager, lines_busy
from app.config import Config
from app.costs import KeyCredit
from app.notify import not_put_through_payload
from app.numbers import KEYS_REFUSED, keys_refused, twilio_client, twilio_message, twilio_refusal
from app.put_through import forget_on_twilio, keep_put_through
from app.store import OPEN_STATUSES, AgentSettings, Store
from app.summary import alert_if_missed, alert_owner

# A blocked number matches a caller however the owner wrote it: "+61 412 345 678",
# "0412 345 678" (without the country code, with the 0 before the area code) or
# "61412345678". It needs this many digits (leading zeros aside), so a short
# entry cannot block every number that happens to end the same way.
BLOCKED_DIGITS_LEAST = 8
BLOCKED_NUMBERS_MOST = 500
# The most of a request from Twilio read (its forms are a few kilobytes).
TWILIO_FORM_MOST_BYTES = 64 * 1024


def number_digits(number: str) -> str:
    return re.sub(r"\D", "", number)


def blocked_entry(number: str) -> str:
    """A number for the blocked list, tidied. Raises ValueError when it is not one."""
    number = re.sub(r"\s+", " ", number).strip()
    digits = number_digits(number).lstrip("0")
    if not BLOCKED_DIGITS_LEAST <= len(digits) <= 15 or re.search(r"[^\d\s()+.\-]", number):
        raise ValueError(f"{number!r} is not a phone number (8 to 15 digits).")
    return number


def is_blocked(caller: str, blocked: list[str]) -> bool:
    """Whether a call from `caller` (as Twilio gives it, +61412345678) is refused."""
    digits = number_digits(caller)
    if not digits:
        return False
    for entry in blocked:
        wanted = number_digits(entry).lstrip("0")
        if len(wanted) >= BLOCKED_DIGITS_LEAST and digits.endswith(wanted):
            return True
    return False


# What someone phoning in hears while Oscar is on as many calls as MAX_CALLS lets it.
LINES_BUSY_LINE = "Sorry, all our lines are busy right now. Please call back in a few minutes. Goodbye."

# How long the person a caller is put through to is rung before giving up.
TRANSFER_RING_SECONDS = 25
# What a caller hears when no one takes the call they were put through to.
NOBODY_THERE_LINE = "Sorry, no one could take your call just now. We'll call you back as soon as we can. Goodbye."
TRANSFER_RESULTS = {
    "completed": "answered",
    "answered": "answered",
    "busy": "the line was busy",
    "no-answer": "no one answered",
    "failed": "the call could not be made",
    "canceled": "the caller hung up first",
}
# What answered a call Oscar made, as Twilio's machine detection tells it.
MACHINE_ANSWERS = {"machine_end_beep", "machine_end_silence", "machine_end_other", "machine_start"}

FAILED_CALL_REASONS = {
    "busy": "The line was busy.",
    "no-answer": "No one answered.",
    "failed": "Twilio could not connect the call.",
    "canceled": "The call was canceled before it was answered.",
}


class PhoneCallError(RuntimeError):
    """Twilio refused to place or change a call.

    `stops_lists`: the refusal is about the account or the number calls come from,
    not the person called, so a call list pauses instead of failing everyone on it.
    `try_later`: Twilio was too busy, or could not be reached; the person is called again later.
    `call_id`: Twilio may have placed the call all the same (it did not answer in time):
    the call is left open for Twilio's updates, or the stream token's expiry, to settle."""

    def __init__(self, message: str, *, stops_lists: bool = False, try_later: bool = False, call_id: str = ""):
        super().__init__(message)
        self.stops_lists = stops_lists
        self.try_later = try_later
        self.call_id = call_id


# Twilio's refusals (https://www.twilio.com/docs/api/errors) that are about the account
# or the number calls come from, so every other call would be refused too.
ACCOUNT_REFUSALS = {
    10001,  # Account is not active
    20003,  # Permission Denied (the Account SID and Auth Token)
    20005,  # Account not active (suspended, or no balance)
    20008,  # Test Credentials (they cannot place real calls)
    21210,  # 'From' phone number not verified
    21212,  # Invalid 'From' Number
}
TOO_MANY_REQUESTS = 20429


NO_NUMBER_TO_CALL_FROM = (
    "Choose the number Oscar calls from first: Phone, Your numbers (or set TWILIO_PHONE_NUMBER in .env)."
)


def stream_twiml(cfg: Config, call_id: str, token: str) -> str:
    response = VoiceResponse()
    connect = Connect()
    stream = connect.stream(url=cfg.media_stream_url)
    stream.parameter(name="call_id", value=call_id)
    stream.parameter(name="token", value=token)
    response.append(connect)
    return str(response)


def _twiml_response(twiml: str) -> Response:
    return Response(content=twiml, media_type="application/xml")


def _twilio_client(cfg: Config) -> Client:
    # With a timeout: a request Twilio never answers must not hold up a call list for good.
    return twilio_client(cfg)


def _never_reached(exc: Exception) -> bool:
    """Whether a request failed before Twilio had it (no address, no connection), so
    nothing it asked for can have happened. A timeout or a reset after sending is not."""
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return True
    if isinstance(exc, requests.exceptions.ConnectionError):
        reason = getattr(exc.args[0], "reason", None) if exc.args else None
        return isinstance(reason, (urllib3.exceptions.NewConnectionError, urllib3.exceptions.ConnectTimeoutError))
    return isinstance(exc, (ConnectionRefusedError, socket.gaierror))


async def place_outbound_call(
    cfg: Config,
    calls: CallManager,
    to_number: str,
    *,
    agent: AgentSettings | None = None,
    campaign_id: str = "",
    from_number: str = "",
) -> dict:
    """Dial a number from `from_number` (TWILIO_PHONE_NUMBER when not given; see
    app.numbers.calls_from). `agent`: settings for this call only (a call list's),
    instead of the saved ones."""
    from_number = from_number or cfg.twilio_phone_number
    if not from_number:
        raise PhoneCallError(NO_NUMBER_TO_CALL_FROM)
    call = calls.create(
        "phone_outbound",
        status="connecting",
        from_number=from_number,
        to_number=to_number,
        campaign_id=campaign_id,
    )
    if agent is not None:
        calls.use_agent(call["id"], agent)
    token = calls.issue_phone_token(call["id"])
    try:
        twilio_call = await asyncio.to_thread(
            _twilio_client(cfg).calls.create,
            to=to_number,
            from_=from_number,
            twiml=stream_twiml(cfg, call["id"], token),
            # The call's id rides along (Twilio signs it with the rest), so an
            # update that arrives before Twilio has answered with the call's SID
            # still finds the call.
            status_callback=f"{cfg.public_url}/twilio/status?call_id={call['id']}",
            status_callback_event=["initiated", "ringing", "answered", "completed"],
            status_callback_method="POST",
            # Oscar starts talking at once; if a voicemail answered, Twilio says so once
            # its greeting has ended (at the beep), so the message lands on the recording.
            machine_detection="DetectMessageEnd",
            async_amd="true",
            async_amd_status_callback=f"{cfg.public_url}/twilio/answered-by?call_id={call['id']}",
            async_amd_status_callback_method="POST",
        )
    except Exception as exc:
        stops_lists = try_later = False
        refusal = twilio_refusal(exc)
        if refusal is not None and refusal.status in (500, 502, 504):
            # Twilio failed part-way: it may have placed the call. As with no answer in time
            # (below), the call stays open for its updates, or its token's expiry, to settle.
            logger.warning(f"Twilio answered {refusal.status} when placing a call: {exc!r}")
            raise PhoneCallError(
                "Twilio had a problem placing the call, so it may still go through: Call history shows how it went.",
                call_id=call["id"],
            ) from exc
        if refusal is not None:
            if keys_refused(refusal):
                message = KEYS_REFUSED
            else:
                message = f"Twilio refused the call: {twilio_message(refusal)}."
            stops_lists = keys_refused(refusal) or refusal.code in ACCOUNT_REFUSALS
            # Too many at once, or Twilio unavailable (503): not the person's doing; later.
            try_later = refusal.status in (429, 503) or refusal.code == TOO_MANY_REQUESTS
        elif _never_reached(exc):
            # What went wrong on this server's side goes to the log, not the dashboard.
            logger.warning(f"Could not reach Twilio to place a call: {exc!r}")
            message = "Could not reach Twilio. Try again in a moment."
            try_later = True
        else:
            # Twilio may have placed it. Its updates name the call (call_id rides on them) and
            # the stream token's expiry closes it if none come, so it is not closed here: a
            # call closed now would be silence to someone who answers, and calling them
            # again would ring them twice.
            logger.warning(f"Twilio did not answer in time when placing a call: {exc!r}")
            raise PhoneCallError(
                "Twilio did not answer in time, so the call may still go through: Call history shows how it went.",
                call_id=call["id"],
            ) from exc
        calls.finish(call["id"], message)
        raise PhoneCallError(message, stops_lists=stops_lists, try_later=try_later) from exc

    call = calls.update(call["id"], twilio_sid=twilio_call.sid)
    if call["status"] not in OPEN_STATUSES:
        # Ended from the dashboard while Twilio was still placing the call.
        try:
            await hang_up_phone_call(cfg, twilio_call.sid)
        except PhoneCallError as exc:
            logger.warning(f"Call {call['id']}: {exc}")
    return call


async def hang_up_phone_call(cfg: Config, twilio_sid: str) -> None:
    """End a Twilio call whether it is still ringing or already answered."""
    try:
        await asyncio.to_thread(_twilio_client(cfg).calls(twilio_sid).update, status="completed")
    except Exception as exc:
        refusal = twilio_refusal(exc)
        detail = twilio_message(refusal) if refusal is not None else str(exc)
        raise PhoneCallError(f"Twilio could not hang up the call: {detail}.") from exc


async def put_call_through(
    cfg: Config, call_id: str, twilio_sid: str, number: str, *, record: bool = False
) -> None:
    """Hand a call that is going on over to `number`: Twilio rings it and joins the caller to
    whoever answers. Oscar's audio stream on the call ends. With `record`, Twilio records
    the two of them from the answer, each on a channel of their own (app/put_through.py)."""
    response = VoiceResponse()
    recording = (
        {
            "record": "record-from-answer-dual",
            "recording_status_callback": f"{cfg.public_url}/twilio/put-through-recording?call_id={call_id}",
            "recording_status_callback_method": "POST",
            "recording_status_callback_event": "completed",
        }
        if record
        else {}
    )
    response.dial(
        number,
        timeout=TRANSFER_RING_SECONDS,
        action=f"{cfg.public_url}/twilio/put-through?call_id={call_id}",
        method="POST",
        **recording,
    )
    try:
        await asyncio.to_thread(_twilio_client(cfg).calls(twilio_sid).update, twiml=str(response))
    except Exception as exc:
        refusal = twilio_refusal(exc)
        detail = twilio_message(refusal) if refusal is not None else str(exc)
        raise PhoneCallError(f"Twilio could not put the call through: {detail}.") from exc


async def _read_start_message(websocket: WebSocket) -> dict | None:
    """Twilio sends a 'connected' message, then 'start' with the stream details."""
    for _ in range(5):
        try:
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=10)
            message = json.loads(raw)
        except (asyncio.TimeoutError, WebSocketDisconnect, json.JSONDecodeError):
            return None
        if not isinstance(message, dict):
            return None
        if message.get("event") == "start" and isinstance(message.get("start"), dict):
            return message["start"]
    return None


def create_phone_router(
    cfg: Config, calls: CallManager, store: Store, credit: KeyCredit | None = None
) -> APIRouter:
    router = APIRouter(prefix="/twilio")
    validator = RequestValidator(cfg.twilio_auth_token) if cfg.twilio_auth_token else None

    async def signed_form(request: Request) -> dict[str, str] | None:
        """The posted form if the request really comes from Twilio, else None."""
        if validator is None or not cfg.phone_enabled:
            return None
        # Twilio sends a small form: anything else is refused before it is read (a body of
        # files, or a huge one, from anyone, would otherwise be read in before the signature).
        kind = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        length = request.headers.get("content-length", "")
        if kind != "application/x-www-form-urlencoded" or not length.isdigit() or int(length) > TWILIO_FORM_MOST_BYTES:
            return None
        form = {key: str(value) for key, value in (await request.form()).items()}
        # Twilio signs the public URL it called, not the address we see behind the tunnel.
        url = cfg.public_url + request.url.path
        if request.url.query:
            url += "?" + request.url.query
        signature = request.headers.get("X-Twilio-Signature", "")
        if not validator.validate(url, form, signature):
            logger.warning(f"Rejected a request to {request.url.path} with a bad Twilio signature")
            return None
        return form

    @router.post("/voice")
    async def incoming_call(request: Request) -> Response:
        form = await signed_form(request)
        if form is None:
            return Response(status_code=403)

        if is_blocked(form.get("From", ""), store.get_blocked_numbers()):
            # Refused before it is answered, so Twilio does not charge for it, and
            # kept out of the call history (blocked numbers are mostly spam).
            logger.info("Refused a call from a blocked number")
            response = VoiceResponse()
            response.reject(reason="rejected")
            return _twiml_response(str(response))

        if cfg.missing_agent_keys():
            response = VoiceResponse()
            response.say("Sorry, this agent is not set up yet. Goodbye.")
            response.hangup()
            return _twiml_response(str(response))

        def turn_away(reason: str, said: str) -> Response:
            # Kept in the call history, so the owner sees the call it missed and why.
            missed = calls.create(
                "phone_inbound",
                status="connecting",
                from_number=form.get("From", ""),
                to_number=form.get("To", ""),
            )
            calls.finish(missed["id"], reason)
            alert_if_missed(calls, calls.store.get_call(missed["id"]) or missed, store.get_agent().name)
            response = VoiceResponse()
            response.say(said)
            response.hangup()
            return _twiml_response(str(response))

        too_little = await credit.too_little_for_a_call() if credit else ""
        # After the wait for the credit, and with none from here to the call being made,
        # so two callers at once cannot both take the last line (MAX_CALLS).
        busy = lines_busy(store, cfg)
        if busy:
            return turn_away(busy, LINES_BUSY_LINE)
        if too_little:
            return turn_away(too_little, "Sorry, this agent cannot take calls right now. Please try again later. Goodbye.")

        call = calls.create(
            "phone_inbound",
            status="connecting",
            from_number=form.get("From", ""),
            to_number=form.get("To", ""),
        )
        calls.update(call["id"], twilio_sid=form.get("CallSid", ""))
        token = calls.issue_phone_token(call["id"])
        return _twiml_response(stream_twiml(cfg, call["id"], token))

    @router.post("/status")
    async def call_status(request: Request) -> Response:
        form = await signed_form(request)
        if form is None:
            return Response(status_code=403)

        sid = form.get("CallSid", "")
        call = store.find_call_by_twilio_sid(sid)
        if call is None and sid:
            # An outgoing call whose SID is not saved yet: Twilio can send an update
            # (a quick "failed" or "busy") before its answer to placing the call.
            named = store.get_call(request.query_params.get("call_id", ""))
            if named and named["kind"] == "phone_outbound" and not named["twilio_sid"]:
                call = calls.update(named["id"], twilio_sid=sid)
        status = form.get("CallStatus", "")
        if call:
            if status == "ringing" and call["status"] == "connecting":
                calls.update(call["id"], status="ringing")
            elif status in FAILED_CALL_REASONS:
                calls.set_status_if_open(call["id"], "failed", FAILED_CALL_REASONS[status])
            elif status == "completed" and call["id"] not in calls.active_call_ids and call["status"] in OPEN_STATUSES:
                # The call is over, no agent is running it and nothing closed it: it was
                # answered but its audio stream never started. (One the agent ended has
                # been closed, and alerted for if missed, already: Twilio's update often
                # comes after, sometimes twice.)
                calls.finish(call["id"], "" if call["status"] == "live" else AUDIO_NEVER_CONNECTED)
                if call["status"] != "live":
                    alert_if_missed(calls, calls.store.get_call(call["id"]) or call, store.get_agent().name)
        return Response(status_code=204)

    @router.post("/answered-by")
    async def answered_by(request: Request) -> Response:
        """What answered a call Oscar made: a person, a voicemail or a fax (Twilio's detection)."""
        form = await signed_form(request)
        if form is None:
            return Response(status_code=403)
        call = store.find_call_by_twilio_sid(form.get("CallSid", "")) or store.get_call(
            request.query_params.get("call_id", "")
        )
        answer = form.get("AnsweredBy", "")
        if call is None or call["kind"] != "phone_outbound":
            return Response(status_code=204)
        if answer in MACHINE_ANSWERS:
            calls.update(call["id"], answered_by="voicemail")
            if not calls.voicemail_answered(call["id"]):
                logger.info(f"Call {call['id']}: a voicemail answered after the call ended")
        elif answer == "fax":
            calls.update(call["id"], answered_by="fax")
            calls.add_message(call["id"], "note", "A fax machine answered, so the call was ended.")
            if not await calls.hang_up(call["id"]) and call["twilio_sid"]:
                try:
                    await hang_up_phone_call(cfg, call["twilio_sid"])
                except PhoneCallError as exc:
                    logger.warning(f"Call {call['id']}: {exc}")
        return Response(status_code=204)

    @router.post("/put-through")
    async def put_through_result(request: Request) -> Response:
        """How putting a caller through went (the <Dial> has ended)."""
        form = await signed_form(request)
        if form is None:
            return Response(status_code=403)
        call = store.get_call(request.query_params.get("call_id", ""))
        status = form.get("DialCallStatus", "")
        result = TRANSFER_RESULTS.get(status, f"it ended as {status or 'unknown'}")
        response = VoiceResponse()
        if call is None:
            return _twiml_response(str(response))
        number = call["transferred_to"] or "the number in Agent settings"
        if result == "answered":
            try:
                seconds = int(form.get("DialCallDuration") or 0)
            except ValueError:
                seconds = 0
            minutes, seconds = divmod(seconds, 60)
            length = f"{minutes} min {seconds} s" if minutes else f"{seconds} s"
            calls.update(call["id"], transfer_result="answered")
            calls.add_message(call["id"], "note", f"Put through to {number}: they talked for {length}.")
        else:
            calls.update(call["id"], transfer_result=result)
            calls.add_message(call["id"], "note", f"Put through to {number}, but {result}.")
            alert_owner(
                calls,
                not_put_through_payload(call, agent_name=store.get_agent().name, number=number, result=result),
            )
            response.say(NOBODY_THERE_LINE)
            response.hangup()
        return _twiml_response(str(response))

    # Recordings already being kept: Twilio may tell of one more than once.
    recordings_kept: set[str] = set()

    @router.post("/put-through-recording")
    async def put_through_recording(request: Request) -> Response:
        """Twilio has recorded the part of a call after it was put through."""
        form = await signed_form(request)
        if form is None:
            return Response(status_code=403)
        call = store.get_call(request.query_params.get("call_id", ""))
        sid = form.get("RecordingSid", "")
        status = form.get("RecordingStatus", "")
        if call is None:
            # The call was deleted: none of it is kept, Twilio's copy included.
            if sid and status == "completed":
                calls.spawn(forget_on_twilio(cfg, sid))
            return Response(status_code=204)
        if status == "failed":
            calls.add_message(call["id"], "note", "Twilio could not record the call after it was put through.")
        if status != "completed" or not sid or sid in recordings_kept:
            return Response(status_code=204)
        recordings_kept.add(sid)
        calls.spawn(keep_put_through(cfg, calls, call["id"], sid, form.get("RecordingUrl", "")))
        return Response(status_code=204)

    @router.websocket("/media")
    async def media_stream(websocket: WebSocket) -> None:
        await websocket.accept()
        start = await _read_start_message(websocket)
        params = start.get("customParameters") if start else None
        if not isinstance(params, dict):
            params = {}
        call_id = str(params.get("call_id", ""))
        token = str(params.get("token", ""))
        call = store.get_call(call_id) if call_id else None

        if (
            start is None
            or not cfg.phone_enabled
            or not start.get("streamSid")
            or not start.get("callSid")
            or call is None
            or call["status"] not in OPEN_STATUSES
            or not calls.redeem_phone_token(call_id, token)
        ):
            logger.warning("Rejected a Twilio media stream without a valid call token")
            await websocket.close(code=1008)
            return

        serializer = TwilioFrameSerializer(
            stream_sid=start["streamSid"],
            call_sid=start["callSid"],
            account_sid=cfg.twilio_account_sid,
            auth_token=cfg.twilio_auth_token,
        )
        transport = FastAPIWebsocketTransport(
            websocket,
            FastAPIWebsocketParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                add_wav_header=False,
                serializer=serializer,
                # Twilio's media connection carries no Origin header; the token above authenticates it.
                allowed_origins=[],
            ),
        )
        calls.update(call_id, twilio_sid=start["callSid"])
        listed = calls.agent_for(call_id)  # a call list's settings for this person
        agent = listed or store.get_agent()

        async def transfer(number: str) -> None:
            # Twilio carries on with the call: stopping Oscar's side must not hang it up.
            serializer._params.auto_hang_up = False
            try:
                await put_call_through(cfg, call_id, start["callSid"], number, record=agent.record_calls)
            except PhoneCallError:
                serializer._params.auto_hang_up = True
                raise

        await voice_agent.run_call(
            transport=transport,
            call_id=call_id,
            calls=calls,
            cfg=cfg,
            agent=agent,
            sample_rates=voice_agent.PHONE_SAMPLE_RATES,
            # A phone keeps its own speaker out of its microphone.
            barge_in=True,
            transfer=transfer,
            # Lines said the same way on every call are said (and kept) by the owner's own settings.
            line_agent=store.get_agent() if listed else None,
        )

    return router
