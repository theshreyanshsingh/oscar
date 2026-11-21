"""What Oscar spends on OpenRouter: each call's cost, and what is left on the key.

(Only httpx is needed here: the WhatsApp process loads this too, through app/search.py.)

Each call's cost is added up from what OpenRouter reports for each of its
requests (the answers, the transcripts, the lookup decisions and answers, the
script keeper, and the summary and research notes afterwards). Asked to, it
reports what a request cost, to a hundred-thousandth of a cent:

    "usage": {"cost": 0.0015895, ...}   (in the answer, or in a stream's last chunk)

An answer cut off part-way (the caller carried on talking) never gets to its
last chunk. Its cost is looked up afterwards by its id, which OpenRouter sends
in a header: it is there about ten to fifteen seconds later (measured
2026-11-20). A request cut off before OpenRouter answered at all is not
counted. So a call's cost is a close account, and the key's own figures (what
`KeyCredit` reads) are the exact ones.

What is left on the key is what decides whether a call can start: OpenRouter
lets each key have a spending limit of its own, which it enforces itself.
"""

import asyncio
import time
from collections.abc import Callable, Coroutine
from typing import Any

import httpx

from app.openrouter import DEFAULT_BASE_URL, auth_headers, new_client

# Asked for in every request, so that the answer says what it cost.
USAGE = {"include": True}
# When to look up the cost of a request that was cut off (seconds after it was,
# one after another): OpenRouter has it about ten to fifteen seconds later.
LOOKUP_DELAYS = (5.0, 10.0, 20.0, 40.0)
# How long what the key has left is trusted before it is asked again.
KEY_CHECK_SECONDS = 30.0
# Below this much left on the key, no call starts: a few turns of a call cost
# about this much, and one that runs out part-way stops answering mid-call.
CALL_MINIMUM_DOLLARS = 0.05


def cost_of(body: object) -> float:
    """What OpenRouter charged for a request, from the usage it reported (0 when there is none)."""
    usage = body.get("usage") if isinstance(body, dict) else None
    cost = usage.get("cost") if isinstance(usage, dict) else None
    if isinstance(cost, bool) or not isinstance(cost, (int, float)) or cost <= 0:
        return 0.0
    return float(cost)


class CostMeter:
    """Adds what one call's requests cost to that call.

    `add` is given each request's cost in dollars. `spawn` runs the lookups of
    requests that were cut off, in the background (they outlast the call).
    """

    def __init__(
        self,
        add: Callable[[float], None],
        *,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        spawn: Callable[[Coroutine], Any] | None = None,
        client: httpx.AsyncClient | None = None,
        delays: tuple[float, ...] = LOOKUP_DELAYS,
    ):
        self._add = add
        self._headers = auth_headers(api_key)
        self._generation_url = f"{base_url}/generation"
        self._spawn = spawn
        self._client = client
        self._delays = delays

    def charge(self, body: object) -> float:
        """Count what a request's answer (or a stream's last chunk) says it cost. Returns that."""
        cost = cost_of(body)
        if cost:
            self._add(cost)
        return cost

    def charge_later(self, generation_id: str) -> None:
        """Count a request that was cut off before it said what it cost, once OpenRouter knows."""
        if generation_id and self._spawn is not None:
            self._spawn(self._look_up(generation_id))

    async def _look_up(self, generation_id: str) -> None:
        client = self._client or new_client()
        try:
            for delay in self._delays:
                await asyncio.sleep(delay)
                try:
                    response = await client.get(
                        self._generation_url,
                        params={"id": generation_id},
                        headers=self._headers,
                        timeout=10,
                    )
                except httpx.HTTPError:
                    continue
                if response.status_code == 404:
                    continue  # not counted up yet
                if response.status_code != 200:
                    break
                try:
                    cost = response.json()["data"]["total_cost"]
                except (ValueError, LookupError, TypeError):
                    break
                if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost > 0:
                    self._add(float(cost))
                return
            # Not found in time: the call's cost is left without it.
        finally:
            if self._client is None:
                await client.aclose()


class KeyCredit:
    """What the OpenRouter key has spent and has left, as OpenRouter itself counts it."""

    def __init__(
        self, *, api_key: str, base_url: str = DEFAULT_BASE_URL, client: httpx.AsyncClient | None = None
    ):
        self._url = f"{base_url}/key"
        self._headers = auth_headers(api_key) if api_key else {}
        self._client = client or new_client(timeout=10)
        self._known: dict | None = None
        self._known_at = 0.0
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(self, *, fresh: bool = False) -> dict | None:
        """{"limit", "left", "resets", "today", "this_month"} in dollars ("limit" and "left" None when
        the key has no limit), or None when OpenRouter could not say."""
        if not self._headers:
            return None
        async with self._lock:
            if not fresh and self._known is not None and time.monotonic() - self._known_at < KEY_CHECK_SECONDS:
                return self._known
            try:
                response = await self._client.get(self._url, headers=self._headers)
                data = response.json()["data"] if response.status_code == 200 else None
            except (httpx.HTTPError, ValueError, LookupError, TypeError):
                data = None
            if not isinstance(data, dict):
                return self._known  # what was known before, if anything
            self._known = {
                "limit": _dollars(data.get("limit")),
                "left": _dollars(data.get("limit_remaining")),
                "resets": data.get("limit_reset") if isinstance(data.get("limit_reset"), str) else None,
                "today": _dollars(data.get("usage_daily")) or 0.0,
                "this_month": _dollars(data.get("usage_monthly")) or 0.0,
            }
            self._known_at = time.monotonic()
            return self._known

    async def too_little_for_a_call(self) -> str:
        """Why no call should start now (the key's limit is all but used up), or "" when one can."""
        credit = await self.get()
        if not credit or credit["left"] is None or credit["left"] >= CALL_MINIMUM_DOLLARS:
            return ""
        resets = f" It starts again when the limit resets ({credit['resets']})." if credit["resets"] else ""
        of_limit = f" of its ${credit['limit']:.2f} limit" if credit["limit"] is not None else ""
        return (
            f"Calls are off: the OpenRouter key has ${max(credit['left'], 0):.2f} left{of_limit}."
            f"{resets} You can raise the limit on openrouter.ai, under Keys."
        )


def _dollars(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(float(value), 4)
