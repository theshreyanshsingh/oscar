"""Web server: the dashboard, its JSON API, browser calls (WebRTC) and Twilio webhooks."""

import asyncio
import csv
import io
import json
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi import Query
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import IceServer, SmallWebRTCConnection
from pipecat.transports.smallwebrtc.request_handler import (
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pydantic import BaseModel, Field

from app import agent as voice_agent
from app.access import CloudflareAccess, RequireAccess
from app.calls import CallManager
from app.catalog import ModelCatalog
from app.costs import CALL_MINIMUM_DOLLARS, KeyCredit
from app.preview import PreviewFailed, voice_sample
from app.notify import SAMPLE_CALL, WebhookRefused, send_summary, summary_payload, webhook_url
from app.config import (
    ROOT_DIR,
    Config,
    browser_call_udp_ports,
    call_audio_unreachable,
    load_config,
)
from app.events import EventBus
from app.phone import (
    PhoneCallError,
    create_phone_router,
    hang_up_phone_call,
    place_outbound_call,
)
from app.store import (
    ACCENTS,
    OPEN_STATUSES,
    VOICES,
    AgentSettings,
    Store,
    WhatsAppSettings,
    utc_now,
    without_excerpts,
)
from app.speech import Recordings
from app.summary import start_summary

STATIC_DIR = ROOT_DIR / "app" / "static"
STUN_SERVER = "stun:stun.l.google.com:19302"


class BrowserCallOffer(BaseModel):
    sdp: str = Field(min_length=1, max_length=100_000)
    type: Literal["offer"]
    # The caller is on headphones, so talking over Oscar can safely interrupt it.
    interruptible: bool = False


class EndCallRequest(BaseModel):
    # Why the browser hung up, when it was not the caller's choice: "audio_failed"
    # when the call's audio never connected (the browser gave up on it).
    reason: Literal["audio_failed"] | None = None


class TodoDone(BaseModel):
    done: bool


class SummaryWebhook(BaseModel):
    url: str = Field(default="", max_length=4000)


class PhoneCallRequest(BaseModel):
    to: str = Field(pattern=r"^\+[1-9]\d{7,14}$", description="E.164 number, e.g. +14155550123")


# The most calls one export holds.
CSV_MOST_CALLS = 10000
CSV_COLUMNS = [
    ("Started (UTC)", "started_at"),
    ("Type", "kind"),
    ("Status", "status"),
    ("From", "from_number"),
    ("To", "to_number"),
    ("Length (seconds)", "duration_seconds"),
    ("Cost (dollars)", "cost"),
    ("Caller's name", "caller_name"),
    ("Call back on", "callback_number"),
    ("Summary", "summary"),
    ("To do", "follow_up"),
    ("To do done (UTC)", "follow_up_done_at"),
    ("Problem", "error"),
]


def spreadsheet_safe(value: object) -> str:
    """A cell as text a spreadsheet shows as written. Something a caller said that
    begins like a formula ("=HYPERLINK(...)") is kept from running as one; a phone
    number such as +61412345678 is left as it is."""
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "-", "@", "\t", "\r") and not re.fullmatch(r"[+-]?\d+(\.\d+)?", text):
        return "'" + text
    return text


def calls_csv(found: list[dict]) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([heading for heading, _ in CSV_COLUMNS])
    for call in found:
        writer.writerow([spreadsheet_safe(call.get(field)) for _, field in CSV_COLUMNS])
    # A byte order mark, so Excel reads it as UTF-8 (names in any script).
    return "\ufeff" + out.getvalue()


def create_app(
    cfg: Config | None = None,
    *,
    access: CloudflareAccess | None = None,
    credit: KeyCredit | None = None,
    catalog: ModelCatalog | None = None,
) -> FastAPI:
    """The web server.

    `access`, `credit` and `catalog` replace the Cloudflare Access checker the
    settings ask for, the reader of what is left on the OpenRouter key, and the
    list of OpenRouter's models (for tests).
    """
    cfg = cfg or load_config()
    credit = credit or KeyCredit(api_key=cfg.openrouter_api_key, base_url=cfg.openrouter_base_url)
    catalog = catalog or ModelCatalog(base_url=cfg.openrouter_base_url)
    if access is None and cfg.access_required:
        access = CloudflareAccess(cfg.access_team_domain, cfg.access_audience)
    store = Store(cfg.db_path)
    bus = EventBus()
    calls = CallManager(store, bus)
    webrtc = SmallWebRTCRequestHandler(ice_servers=[IceServer(urls=STUN_SERVER)])

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stale = store.fail_open_calls("The server stopped during this call.")
        if stale:
            logger.info(f"Closed {stale} call(s) left open by a previous run")
        store.forget_unfinished_summaries()
        if access is not None:
            logger.info(
                f"Only people signed in through Cloudflare Access ({access.team_domain}) can use the "
                "dashboard. /healthz and Twilio's webhooks stay open."
            )
        elif not cfg.local_only:
            logger.warning(
                "Anyone who can reach this port can use the dashboard: it has no password. "
                "Put Cloudflare Access in front of it (README.md, Protect the dashboard)."
            )
        if not cfg.local_only:
            low, high = browser_call_udp_ports()
            logger.info(
                f"Browser calls send their audio straight to this server over UDP ports {low}-{high}, "
                "not through the web address: let those in through the firewall (on EC2, the security group)"
            )
        yield
        await calls.shutdown()
        await webrtc.close()
        store.close()
        if access is not None:
            await access.aclose()
        await credit.aclose()
        await catalog.aclose()

    app = FastAPI(title="Oscar", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.cfg = cfg
    app.state.store = store
    app.state.calls = calls

    if cfg.local_only:
        # Only this computer can open it: refusing other Host names stops web
        # pages from reaching this server through DNS rebinding. The address in
        # PUBLIC_URL (a tunnel such as ngrok) is Oscar's own, chosen by the owner:
        # tunnels pass its name on as the Host, and Twilio's webhooks and media
        # stream come in through it.
        allowed = ["localhost", "127.0.0.1"]
        public_host = urlsplit(cfg.public_url).hostname if cfg.public_url else None
        if public_host:
            allowed.append(public_host)
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed)
    # Otherwise (HOST=0.0.0.0 on a server) it answers at any address. There is no
    # password: Cloudflare Access decides who gets in when it is set up, and
    # otherwise the firewall does.
    if access is not None:
        app.add_middleware(RequireAccess, access=access)

    app.include_router(create_phone_router(cfg, calls, store, credit))
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    def require_agent_keys() -> None:
        missing = cfg.missing_agent_keys()
        if missing:
            raise HTTPException(
                status_code=400,
                detail=f"Add {' and '.join(missing)} to the .env file, then restart the server.",
            )

    async def require_credit() -> None:
        """No new call when the OpenRouter key's own limit is all but used up."""
        too_little = await credit.too_little_for_a_call()
        if too_little:
            raise HTTPException(status_code=402, detail=too_little)

    @app.get("/", include_in_schema=False)
    async def dashboard() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/healthz")
    async def healthz() -> dict:
        # Open even behind Cloudflare Access: deploy.sh and scripts/autostart.py
        # ask it whether Oscar is up and how many calls it is on.
        return {"ok": True, "calls": len(calls.active_call_ids)}

    @app.get("/api/status")
    async def status() -> dict:
        return {
            "agent": {"ready": not cfg.missing_agent_keys(), "missing": cfg.missing_agent_keys()},
            "phone": {
                "enabled": cfg.phone_enabled,
                "missing": cfg.missing_phone_settings(),
                "number": cfg.twilio_phone_number,
                "voice_webhook": f"{cfg.public_url}/twilio/voice" if cfg.public_url else "",
            },
            "search": {"enabled": cfg.search_enabled},
            "voices": VOICES,
            "accents": ACCENTS,
            "active_call_ids": calls.active_call_ids,
        }

    @app.get("/api/spending")
    async def spending() -> dict:
        """What Oscar's calls cost lately, and what the OpenRouter key has spent and has left."""
        now = datetime.now(timezone.utc)
        return {
            # As OpenRouter counts it: everything that uses the key (WhatsApp too,
            # and any other app given the same key). None when it could not say.
            "key": await credit.get(),
            # What Oscar counted for its own calls (app/costs.py).
            "calls": {
                "last_24_hours": round(store.calls_cost_since(now - timedelta(days=1)), 4),
                "last_30_days": round(store.calls_cost_since(now - timedelta(days=30)), 4),
            },
            "call_minimum": CALL_MINIMUM_DOLLARS,
            # Each call of the last 15 days that cost something, as [started_at, dollars]:
            # the dashboard adds them up by day in its own time zone.
            "recent_calls": [
                [started_at, round(cost, 4)]
                for started_at, cost in store.call_costs_since(now - timedelta(days=15))
            ],
        }

    @app.get("/api/models")
    async def chat_models() -> dict:
        """OpenRouter's chat models, as suggestions for the Chat model field ([] when unknown)."""
        return {"models": await catalog.get()}

    @app.get("/api/summary-webhook")
    async def get_summary_webhook() -> dict:
        return store.get_summary_webhook()

    @app.put("/api/summary-webhook")
    async def save_summary_webhook(webhook: SummaryWebhook) -> dict:
        try:
            url = webhook_url(webhook.url)
        except WebhookRefused as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return store.save_summary_webhook_url(url)

    @app.post("/api/summary-webhook/test")
    async def test_summary_webhook() -> dict:
        """Send a made-up summary to the saved address, to see it arrive."""
        url = store.get_summary_webhook()["url"]
        if not url:
            raise HTTPException(status_code=400, detail="Save an address first.")
        problem = await send_summary(url, summary_payload(SAMPLE_CALL, agent_name=store.get_agent().name))
        store.record_summary_webhook_result(problem or "sent")
        return {"sent": not problem, "problem": problem, **store.get_summary_webhook()}

    @app.post("/api/agent/preview")
    async def preview_voice(agent: AgentSettings) -> Response:
        """A short sample of the voice and accent in the form, before they are saved (app/preview.py)."""
        require_agent_keys()
        await require_credit()
        try:
            audio = await voice_sample(
                agent,
                api_key=cfg.openrouter_api_key,
                base_url=cfg.openrouter_base_url,
                recordings=Recordings(cfg.db_path.parent / "recordings"),
            )
        except PreviewFailed as exc:
            raise HTTPException(status_code=502, detail=f"No sample could be had: {exc}") from None
        return Response(audio, media_type="audio/wav", headers={"Cache-Control": "no-store"})

    @app.get("/api/agent")
    async def get_agent() -> AgentSettings:
        return store.get_agent()

    @app.put("/api/agent")
    async def save_agent(agent: AgentSettings) -> AgentSettings:
        return store.save_agent(agent)

    @app.get("/api/calls")
    async def list_calls(
        limit: int = Query(default=100, ge=1, le=200),
        before: str = Query(default="", max_length=40),
        before_id: str = Query(default="", max_length=64),
        q: str = Query(default="", max_length=200),
        show: Literal["all", "todo", "incoming", "outgoing", "browser", "failed"] = "all",
    ) -> dict:
        """Calls for Call history, newest first. `before_id` (the last call shown) gives the next page."""
        found, more = store.find_calls(limit=limit, before=before, before_id=before_id, query=q, show=show)
        return {"calls": found, "more": more, "open_todos": store.count_open_todos()}

    @app.put("/api/calls/{call_id}/todo")
    async def tick_todo(call_id: str, todo: TodoDone) -> dict:
        """Tick the call's to-do off (or back on)."""
        call = store.get_call(call_id)
        if call is None:
            raise HTTPException(status_code=404, detail="Call not found.")
        if not call["follow_up"]:
            raise HTTPException(status_code=409, detail="This call has nothing to do.")
        done_at = utc_now() if todo.done else ""
        return {"call": calls.update(call_id, follow_up_done_at=done_at)}

    @app.get("/api/calls.csv")
    async def export_calls(
        q: str = Query(default="", max_length=200),
        show: Literal["all", "todo", "incoming", "outgoing", "browser", "failed"] = "all",
    ) -> Response:
        """Call history as a spreadsheet: the calls the search and filter show, newest first."""
        found, _ = store.find_calls(limit=CSV_MOST_CALLS, query=q, show=show)
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return Response(
            content=calls_csv(found),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="oscar-calls-{day}.csv"'},
        )

    @app.delete("/api/calls/{call_id}")
    async def delete_call(call_id: str) -> dict:
        """Delete a call for good: its transcript, searches and summary."""
        call = store.get_call(call_id)
        if call is None:
            raise HTTPException(status_code=404, detail="Call not found.")
        if call["status"] in OPEN_STATUSES:
            raise HTTPException(status_code=409, detail="End the call before you delete it.")
        if call["summary_status"] == "writing":
            raise HTTPException(
                status_code=409, detail="Its summary is still being written. Delete it once that is done."
            )
        store.delete_call(call_id)
        bus.publish({"type": "call_deleted", "id": call_id})
        return {"deleted": call_id}

    @app.get("/api/calls/{call_id}")
    async def get_call(call_id: str) -> dict:
        call = store.get_call(call_id)
        if call is None:
            raise HTTPException(status_code=404, detail="Call not found.")
        return {
            "call": call,
            "messages": store.get_messages(call_id),
            # What the call's web searches found, as links: the pages' words stay here.
            "searches": [without_excerpts(search) for search in store.get_searches(call_id)],
        }

    @app.post("/api/calls/browser")
    async def start_browser_call(offer: BrowserCallOffer) -> dict:
        require_agent_keys()
        await require_credit()
        agent = store.get_agent()
        started: dict[str, str] = {}

        async def on_connection(connection: SmallWebRTCConnection) -> None:
            call = calls.create("browser", status="connecting")
            started["call_id"] = call["id"]
            try:
                transport = SmallWebRTCTransport(
                    webrtc_connection=connection,
                    params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
                )
                calls.spawn(
                    voice_agent.run_call(
                        transport=transport,
                        call_id=call["id"],
                        calls=calls,
                        cfg=cfg,
                        agent=agent,
                        sample_rates=voice_agent.BROWSER_SAMPLE_RATES,
                        barge_in=offer.interruptible,
                    )
                )
            except Exception as exc:
                # Pipecat swallows errors raised here, so record them ourselves.
                logger.exception("Could not start the browser call pipeline")
                started["error"] = str(exc)
                calls.finish(call["id"], f"Call setup failed: {exc}")

        try:
            answer = await webrtc.handle_web_request(
                SmallWebRTCRequest(sdp=offer.sdp, type=offer.type), on_connection
            )
        except Exception as exc:
            logger.exception("Browser call setup failed")
            if "call_id" in started:
                calls.finish(started["call_id"], f"Call setup failed: {exc}")
            raise HTTPException(status_code=400, detail="Could not set up the audio connection.")
        if not answer or "call_id" not in started or "error" in started:
            raise HTTPException(status_code=500, detail="Could not start the call.")
        return {"sdp": answer["sdp"], "type": answer["type"], "call_id": started["call_id"]}

    @app.post("/api/calls/phone")
    async def start_phone_call(request: PhoneCallRequest) -> dict:
        require_agent_keys()
        await require_credit()
        missing = cfg.missing_phone_settings()
        if missing:
            raise HTTPException(
                status_code=400,
                detail=f"Phone calls need {', '.join(missing)} in the .env file.",
            )
        try:
            call = await place_outbound_call(cfg, calls, request.to)
        except PhoneCallError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"call": call}

    @app.post("/api/calls/{call_id}/end")
    async def end_call(call_id: str, request: EndCallRequest | None = None) -> dict:
        call = store.get_call(call_id)
        if call is None:
            raise HTTPException(status_code=404, detail="Call not found.")
        reason = request.reason if request else None
        if reason == "audio_failed" and call["kind"] == "browser" and call["status"] == "connecting":
            # Say why before stopping it, so the call is not put down as merely
            # never connected: on a server this is almost always the firewall.
            calls.finish(call_id, call_audio_unreachable(cfg))
        if await calls.hang_up(call_id):
            return {"ended": True}
        if call["status"] not in OPEN_STATUSES:
            return {"ended": False}
        # Not running here yet: a ringing phone call, or a browser call still connecting.
        # Close it here first so its audio stream can no longer start.
        calls.finish(call_id)
        if call["kind"] != "browser" and call["twilio_sid"] and cfg.phone_enabled:
            try:
                await hang_up_phone_call(cfg, call["twilio_sid"])
            except PhoneCallError as exc:
                raise HTTPException(
                    status_code=502, detail=f"The call was closed here, but {exc}"
                ) from exc
        return {"ended": True}

    @app.post("/api/calls/{call_id}/summary")
    async def write_call_summary(call_id: str) -> dict:
        """Write a finished call's summary: again, or for the first time on an older call."""
        require_agent_keys()
        call = store.get_call(call_id)
        if call is None:
            raise HTTPException(status_code=404, detail="Call not found.")
        if call["status"] in OPEN_STATUSES:
            raise HTTPException(
                status_code=409, detail="This call is still going. Its summary is written when it ends."
            )
        if call["summary_status"] == "writing":
            raise HTTPException(status_code=409, detail="The summary is already being written.")
        call = start_summary(cfg, calls, call, store.get_agent())
        if call is None:
            raise HTTPException(
                status_code=400, detail="Nothing was said on this call, so there is nothing to sum up."
            )
        return {"call": call}

    @app.get("/api/whatsapp")
    async def whatsapp() -> dict:
        return {
            "state": store.get_whatsapp_state(),
            "settings": store.get_whatsapp_settings(),
            "activity": store.list_whatsapp_activity(limit=30),
            "agent_name": store.get_agent().name,
            "openrouter_ready": bool(cfg.openrouter_api_key),
            "installed": (ROOT_DIR / ".venv-whatsapp" / "bin" / "python").exists(),
        }

    @app.put("/api/whatsapp/settings")
    async def save_whatsapp_settings(settings: WhatsAppSettings) -> WhatsAppSettings:
        return store.save_whatsapp_settings(settings)

    @app.get("/api/whatsapp/qr.svg", include_in_schema=False)
    async def whatsapp_qr() -> Response:
        svg = store.get_whatsapp_qr_svg()
        if not svg:
            raise HTTPException(status_code=404, detail="There is no linking code right now.")
        return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "no-store"})

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        queue = bus.subscribe()

        async def stream():
            try:
                yield "retry: 2000\n\n"
                while not await request.is_disconnected():
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    yield f"data: {json.dumps(event)}\n\n"
            finally:
                bus.unsubscribe(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app
