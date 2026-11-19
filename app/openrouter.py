"""Small helpers shared by everything that talks to OpenRouter.

Only needs httpx, so both the voice server and the WhatsApp process can use it.
"""

import httpx

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

# How long a connection to OpenRouter stays open between requests. httpx's own
# default, 5 seconds, closed it between nearly every two turns of a call (a turn
# and its reply take longer than that), so each turn first had to set up a new
# connection: 0.05 to 0.1 seconds from a home connection, measured 2026-11-19.
KEEPALIVE_SECONDS = 120
LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=KEEPALIVE_SECONDS)
# A tiny public answer, asked for only to have a connection open before it is needed.
WARM_UP_PATH = "/models/count"

_REASONS = {
    401: "OpenRouter rejected the API key (check OPENROUTER_API_KEY).",
    402: "The OpenRouter account is out of credits.",
    429: "OpenRouter is rate limiting requests. Try again in a minute.",
}


def auth_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "X-Title": "Oscar"}


def error_detail(body) -> str:
    """The provider's own error message from a response body, if it has one."""
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return str(error.get("message", ""))
    if isinstance(error, str):
        return error
    return ""


def describe_failure(response: httpx.Response, *, not_found: str = "") -> str:
    """One readable sentence (or two) for a failed OpenRouter response.

    `not_found` replaces the generic text for a 404, whose meaning depends on
    what was asked for.
    """
    try:
        detail = error_detail(response.json())
    except ValueError:
        detail = ""
    reasons = {**_REASONS, **({404: not_found} if not_found else {})}
    reason = reasons.get(
        response.status_code, f"OpenRouter returned an error ({response.status_code})."
    )
    return f"{reason} {detail}".strip()


def new_client(**options) -> httpx.AsyncClient:
    """An HTTP client whose connections stay open between the turns of a call."""
    return httpx.AsyncClient(limits=LIMITS, **options)


async def warm_up(client: httpx.AsyncClient, base_url: str = DEFAULT_BASE_URL) -> None:
    """Open a connection to OpenRouter now, so the first request that counts finds one ready."""
    try:
        await client.get(f"{base_url}{WARM_UP_PATH}", timeout=5)
    except httpx.HTTPError:
        pass  # only a head start: the request that counts opens its own
