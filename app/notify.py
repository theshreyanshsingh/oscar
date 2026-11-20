"""Sends each call's summary to the owner, at a webhook address set on the dashboard.

When a call ends and its summary has been written, Oscar posts it as JSON:

    {"text": "<the summary as a short message>", "content": "<the same>", "call": {...}}

"text" is what Slack, Google Chat and Microsoft Teams webhooks show, and
"content" what Discord's do, so the message reads well in any of them. Zapier,
Make, n8n or your own server can use the details in "call" instead: who
called, when, for how long, what it cost, the summary, the caller's name and
number, the to-do and the research notes.

A call where only Oscar spoke, and a summary written again from the
dashboard, are not sent.
"""

import asyncio
from urllib.parse import urlsplit

import httpx

WEBHOOK_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
URL_CHARS = 2000
# A webhook that fails for a moment (a 5xx answer, or no answer) is tried once
# more, this long after.
RETRY_AFTER_SECONDS = 3.0
KIND_NAMES = {"browser": "browser call", "phone_inbound": "incoming call", "phone_outbound": "outgoing call"}
CALL_FIELDS = (
    "id",
    "kind",
    "status",
    "error",
    "from_number",
    "to_number",
    "started_at",
    "ended_at",
    "duration_seconds",
    "cost",
    "summary",
    "caller_name",
    "callback_number",
    "follow_up",
    "research",
)


class WebhookRefused(ValueError):
    """The address given is not one Oscar will post summaries to. The message says why."""


def webhook_url(value: str) -> str:
    """The address as it will be used ("" to send nothing). Raises WebhookRefused when it will not do."""
    url = value.strip()
    if not url:
        return ""
    if len(url) > URL_CHARS:
        raise WebhookRefused(f"That address is too long (at most {URL_CHARS} characters).")
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise WebhookRefused("The address must start with https://, like the one your app gives you.")
    return url


def _duration(seconds: object) -> str:
    if not isinstance(seconds, int) or seconds < 0:
        return ""
    return f"{seconds // 60}:{seconds % 60:02d}"


def summary_message(call: dict, *, agent_name: str) -> str:
    """The summary as a short message, the way it reads in a chat."""
    kind = KIND_NAMES.get(call.get("kind", ""), "call")
    other = call.get("from_number") if call.get("kind") == "phone_inbound" else call.get("to_number")
    heading = f"{agent_name}'s {kind}" + (f" with {other}" if other else "")
    details = [part for part in (_duration(call.get("duration_seconds")), _cost(call.get("cost"))) if part]
    lines = [heading + (f" ({', '.join(details)})" if details else "")]
    if call.get("summary"):
        lines.append(f"Summary: {call['summary']}")
    if call.get("caller_name"):
        lines.append(f"Name: {call['caller_name']}")
    if call.get("callback_number"):
        lines.append(f"Call back on: {call['callback_number']}")
    if call.get("follow_up"):
        lines.append(f"To do: {call['follow_up']}")
    research = call.get("research") or []
    if research:
        lines.append(f"Research: {len(research)} note{'s' if len(research) != 1 else ''}, in Call history")
    if call.get("status") == "failed" and call.get("error"):
        lines.append(f"The call failed: {call['error']}")
    return "\n".join(lines)


def _cost(dollars: object) -> str:
    if not isinstance(dollars, (int, float)) or dollars <= 0:
        return ""
    return f"${dollars:.3f}" if dollars < 1 else f"${dollars:.2f}"


def summary_payload(call: dict, *, agent_name: str) -> dict:
    text = summary_message(call, agent_name=agent_name)
    return {"text": text, "content": text, "call": {field: call.get(field) for field in CALL_FIELDS}}


async def send_summary(url: str, payload: dict, client: httpx.AsyncClient | None = None) -> str:
    """Post the summary. Returns "" when the webhook took it, or what went wrong."""
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT)
    try:
        problem = ""
        for attempt in (1, 2):
            try:
                response = await client.post(url, json=payload)
            except httpx.HTTPError as exc:
                problem = f"The webhook could not be reached ({type(exc).__name__})."
            else:
                if response.status_code < 300:
                    return ""
                problem = f"The webhook answered {response.status_code}: {response.text.strip()[:200]}".strip()
                if response.status_code < 500:
                    return problem  # asking again would get the same answer
            if attempt == 1:
                await asyncio.sleep(RETRY_AFTER_SECONDS)
        return problem
    finally:
        if own_client:
            await client.aclose()


# What "Send a test" posts: a call as one would be, so the message can be checked.
SAMPLE_CALL = {
    "id": "test",
    "kind": "phone_inbound",
    "status": "ended",
    "error": "",
    "from_number": "+14155550123",
    "to_number": "",
    "started_at": "",
    "ended_at": "",
    "duration_seconds": 125,
    "cost": 0.031,
    "summary": "This is a test from the dashboard. A real summary says what the caller wanted.",
    "caller_name": "Sam",
    "callback_number": "+14155550123",
    "follow_up": "Nothing: this is only a test.",
    "research": [],
}
