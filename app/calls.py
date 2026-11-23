"""Call bookkeeping shared by browser and phone calls.

Every change is saved to the store and published to the dashboard.
"""

import asyncio
import secrets
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from loguru import logger

from app.events import EventBus
from app.store import (
    OPEN_STATUSES,
    AgentSettings,
    CallKind,
    CallStatus,
    MessageRole,
    Store,
    utc_now,
    without_excerpts,
)

# How long a phone call may take to connect its audio stream after we hand
# Twilio the stream instructions (covers the outbound ring time). A call whose
# audio has not connected by then is marked failed.
PHONE_TOKEN_TTL_SECONDS = 180
AUDIO_NEVER_CONNECTED = "The call audio never connected."
# How long stopping the server waits for calls to end and their summaries to be written.
SHUTDOWN_PATIENCE_SECONDS = 10


def lines_busy(store, cfg) -> str:
    """Why no more calls can start now (Oscar is on as many as MAX_CALLS lets it), or ""."""
    on = store.count_open_calls()
    if on < cfg.max_calls:
        return ""
    return (
        f"Oscar is already on {on} call{'s' if on != 1 else ''}, the most it takes at once "
        "(MAX_CALLS in .env). Try again when one ends."
    )


class CallManager:
    def __init__(self, store: Store, bus: EventBus):
        self.store = store
        self.bus = bus
        self._workers: dict[str, Any] = {}
        self._tasks: set[asyncio.Task] = set()
        self._phone_tokens: dict[str, tuple[str, asyncio.TimerHandle]] = {}
        # What a running call does when a voicemail answers it (app/agent.py).
        self._voicemail: dict[str, Callable[[], Awaitable[None]]] = {}
        # Settings made for one phone call before it connects (a call list's), by call.
        self._agents: dict[str, AgentSettings] = {}

    # Records

    def create(
        self,
        kind: CallKind,
        *,
        status: CallStatus = "connecting",
        from_number: str = "",
        to_number: str = "",
        campaign_id: str = "",
    ) -> dict:
        call = self.store.create_call(
            kind, status=status, from_number=from_number, to_number=to_number, campaign_id=campaign_id
        )
        self._publish_call(call)
        return call

    def update(self, call_id: str, **fields) -> dict | None:
        call = self.store.update_call(call_id, **fields)
        if call:
            self._publish_call(call)
        return call

    def set_status_if_open(self, call_id: str, status: CallStatus, error: str = "") -> None:
        """Move a call to a new status unless it has already finished."""
        call = self.store.get_call(call_id)
        if not call or call["status"] not in OPEN_STATUSES:
            return
        fields: dict[str, str] = {"status": status}
        if status in ("ended", "failed"):
            fields["ended_at"] = utc_now()
        if error:
            fields["error"] = error
        self.update(call_id, **fields)

    def finish(self, call_id: str, error: str = "") -> None:
        # A finished call must not be able to start its audio stream afterwards.
        self._drop_phone_token(call_id)
        self._agents.pop(call_id, None)
        self.set_status_if_open(call_id, "failed" if error else "ended", error)

    def note_error(self, call_id: str, error: str) -> None:
        """Record a problem that happened during a call without ending it."""
        call = self.store.get_call(call_id)
        if call and not call["error"]:
            self.update(call_id, error=error[:500])

    def add_cost(self, call_id: str, dollars: float) -> None:
        """Count what one of the call's requests cost (app/costs.py), and show the new total."""
        if dollars <= 0:
            return
        self.store.add_call_cost(call_id, dollars)
        call = self.store.get_call(call_id)
        if call:
            self._publish_call(call)

    def add_message(self, call_id: str, role: MessageRole, text: str) -> None:
        text = text.strip()
        if not text:
            return
        message = self.store.add_message(call_id, role, text)
        self.bus.publish({"type": "message", "message": message})

    def add_search(self, call_id: str, query: str, sources: list[dict]) -> None:
        """Keep the pages a web search found, for the notes written when the call ends."""
        search = self.store.add_search(call_id, query, sources)
        self.bus.publish({"type": "search", "search": without_excerpts(search)})

    def _publish_call(self, call: dict) -> None:
        self.bus.publish({"type": "call", "call": call})

    # One-time tokens that tie a Twilio media stream to the call we created

    def issue_phone_token(self, call_id: str) -> str:
        self._drop_phone_token(call_id)
        token = secrets.token_urlsafe(24)
        expiry = asyncio.get_running_loop().call_later(
            PHONE_TOKEN_TTL_SECONDS, self._expire_phone_token, call_id, token
        )
        self._phone_tokens[call_id] = (token, expiry)
        return token

    def redeem_phone_token(self, call_id: str, token: str) -> bool:
        """Use up the call's token. Wrong tokens are refused and leave the real one valid."""
        entry = self._phone_tokens.get(call_id)
        if entry is None or not token or not secrets.compare_digest(entry[0], token):
            return False
        self._drop_phone_token(call_id)
        return True

    def _expire_phone_token(self, call_id: str, token: str) -> None:
        entry = self._phone_tokens.get(call_id)
        if entry and entry[0] == token:
            self.finish(call_id, AUDIO_NEVER_CONNECTED)

    def _drop_phone_token(self, call_id: str) -> None:
        entry = self._phone_tokens.pop(call_id, None)
        if entry:
            entry[1].cancel()

    # Running calls

    def spawn(self, coro: Coroutine) -> asyncio.Task:
        """Run a call in the background while keeping a reference to its task."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def register_worker(self, call_id: str, worker: Any) -> None:
        self._workers[call_id] = worker

    def unregister_worker(self, call_id: str) -> None:
        self._workers.pop(call_id, None)
        self._voicemail.pop(call_id, None)

    def use_agent(self, call_id: str, agent: AgentSettings) -> None:
        """Run this phone call with these settings instead of the saved ones."""
        self._agents[call_id] = agent

    def agent_for(self, call_id: str) -> AgentSettings | None:
        """The settings made for this call, if any (handed over once)."""
        return self._agents.pop(call_id, None)

    def on_voicemail(self, call_id: str, leave: Callable[[], Awaitable[None]]) -> None:
        self._voicemail[call_id] = leave

    def voicemail_answered(self, call_id: str) -> bool:
        """A voicemail answered the call: have it leave its message. False when it is not running here."""
        leave = self._voicemail.pop(call_id, None)
        if leave is None:
            return False
        self.spawn(leave())
        return True

    @property
    def active_call_ids(self) -> list[str]:
        return list(self._workers)

    async def hang_up(self, call_id: str) -> bool:
        """Stop a running call. Returns False when the call is not running here."""
        worker = self._workers.get(call_id)
        if worker is None:
            return False
        await worker.cancel(reason="Ended from the dashboard")
        return True

    async def shutdown(self) -> None:
        for call_id in list(self._workers):
            try:
                await self.hang_up(call_id)
            except Exception:
                logger.exception(f"Could not stop call {call_id} during shutdown")
        # A call that ends here starts the summary of what was said (app/summary.py):
        # wait for that too, then let nothing outlive the store.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SHUTDOWN_PATIENCE_SECONDS
        while self._tasks and loop.time() < deadline:
            await asyncio.wait(set(self._tasks), timeout=deadline - loop.time())
        for task in self._tasks:
            task.cancel()
