"""Small helpers shared by everything that talks to OpenRouter.

Only needs httpx, so both the voice server and the WhatsApp process can use it.
"""

import httpx

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

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
