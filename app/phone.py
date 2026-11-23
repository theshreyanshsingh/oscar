"""Real phone calls through Twilio.

Inbound:  Twilio calls POST /twilio/voice when someone dials your number.
Outbound: the dashboard (or a call list) calls place_outbound_call().
Both answer with TwiML that streams the call audio to the /twilio/media
WebSocket, where the voice agent takes over.

On a call Oscar makes, Twilio listens for a voicemail (answering machine
detection) and says what answered at /twilio/answered-by. A caller who asks for
a person is handed to a <Dial> (put_call_through), and how that went comes to
/twilio/put-through.

Every request from Twilio is checked against its X-Twilio-Signature, and the
media stream must present a one-time token minted for that call, so nobody
else can start or hijack a call.
"""

import asyncio
import json
import re

from fastapi import APIRouter, Request, Response, WebSocket, WebSocketDisconnect
from loguru import logger
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
from twilio.base.exceptions import TwilioRestException
from twilio.request_validator import RequestValidator
from twilio.rest import Client
from twilio.twiml.voice_response import Connect, VoiceResponse

from app import agent as voice_agent
from app.calls import AUDIO_NEVER_CONNECTED, CallManager, lines_busy
from app.config import Config
from app.costs import KeyCredit
from app.notify import not_put_through_payload
from app.store import OPEN_STATUSES, AgentSettings, Store
from app.summary import alert_if_missed, alert_owner

# A blocked number matches a caller however the owner wrote it: "+61 412 345 678",
# "0412 345 678" (without the country code, with the 0 before the area code) or
# "61412345678". It needs this many digits (leading zeros aside), so a short
# entry cannot block every number that happens to end the same way.
BLOCKED_DIGITS_LEAST = 8
BLOCKED_NUMBERS_MOST = 500


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
    """Twilio refused to place or change a call."""


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
    return Client(cfg.twilio_account_sid, cfg.twilio_auth_token)


async def place_outbound_call(
    cfg: Config,
    calls: CallManager,
    to_number: str,
    *,
    agent: AgentSettings | None = None,
    campaign_id: str = "",
) -> dict:
    """Dial a number. `agent`: settings for this call only (a call list's), instead of the saved ones."""
    call = calls.create(
        "phone_outbound",
        status="connecting",
        from_number=cfg.twilio_phone_number,
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
            from_=cfg.twilio_phone_number,
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
        if isinstance(exc, TwilioRestException):
            message = f"Twilio refused the call: {exc.msg}"
        else:
            message = f"Could not reach Twilio: {exc}"
        calls.finish(call["id"], message)
        raise PhoneCallError(message) from exc

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
        detail = exc.msg if isinstance(exc, TwilioRestException) else str(exc)
        raise PhoneCallError(f"Twilio could not hang up the call: {detail}") from exc


async def put_call_through(cfg: Config, call_id: str, twilio_sid: str, number: str) -> None:
    """Hand a call that is going on over to `number`: Twilio rings it and joins the caller to
    whoever answers. Oscar's audio stream on the call ends."""
    response = VoiceResponse()
    response.dial(
        number,
        timeout=TRANSFER_RING_SECONDS,
        action=f"{cfg.public_url}/twilio/put-through?call_id={call_id}",
        method="POST",
    )
    try:
        await asyncio.to_thread(_twilio_client(cfg).calls(twilio_sid).update, twiml=str(response))
    except Exception as exc:
        detail = exc.msg if isinstance(exc, TwilioRestException) else str(exc)
        raise PhoneCallError(f"Twilio could not put the call through: {detail}") from exc


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

        busy = lines_busy(store, cfg)
        if busy:
            return turn_away(busy, LINES_BUSY_LINE)
        too_little = await credit.too_little_for_a_call() if credit else ""
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
            elif status == "completed" and call["id"] not in calls.active_call_ids:
                # The call is over and no agent is running it: either it already
                # finished, or it was answered but its audio stream never started.
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

        async def transfer(number: str) -> None:
            # Twilio carries on with the call: stopping Oscar's side must not hang it up.
            serializer._params.auto_hang_up = False
            try:
                await put_call_through(cfg, call_id, start["callSid"], number)
            except PhoneCallError:
                serializer._params.auto_hang_up = True
                raise

        await voice_agent.run_call(
            transport=transport,
            call_id=call_id,
            calls=calls,
            cfg=cfg,
            agent=calls.agent_for(call_id) or store.get_agent(),
            sample_rates=voice_agent.PHONE_SAMPLE_RATES,
            # A phone keeps its own speaker out of its microphone.
            barge_in=True,
            transfer=transfer,
        )

    return router
