"""Real phone calls through Twilio.

Inbound:  Twilio calls POST /twilio/voice when someone dials your number.
Outbound: the dashboard calls place_outbound_call().
Both answer with TwiML that streams the call audio to the /twilio/media
WebSocket, where the voice agent takes over.

Every request from Twilio is checked against its X-Twilio-Signature, and the
media stream must present a one-time token minted for that call, so nobody
else can start or hijack a call.
"""

import asyncio
import json

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
from app.store import OPEN_STATUSES, Store

# What someone phoning in hears while Oscar is on as many calls as MAX_CALLS lets it.
LINES_BUSY_LINE = "Sorry, all our lines are busy right now. Please call back in a few minutes. Goodbye."

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


async def place_outbound_call(cfg: Config, calls: CallManager, to_number: str) -> dict:
    call = calls.create(
        "phone_outbound",
        status="connecting",
        from_number=cfg.twilio_phone_number,
        to_number=to_number,
    )
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
        await voice_agent.run_call(
            transport=transport,
            call_id=call_id,
            calls=calls,
            cfg=cfg,
            agent=store.get_agent(),
            sample_rates=voice_agent.PHONE_SAMPLE_RATES,
            # A phone keeps its own speaker out of its microphone.
            barge_in=True,
        )

    return router
