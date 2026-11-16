"""Asking the model (through OpenRouter) for Oscar's reply.

When Oscar may search the web, the model is offered a search tool. Each search
it asks for is run through Firecrawl (app/search.py) and the pages found are
handed back, until it answers.
"""

import asyncio
import base64
import json
import logging
from dataclasses import dataclass

import httpx

from app.openrouter import DEFAULT_BASE_URL, auth_headers, describe_failure, error_detail
from app.search import DEFAULT_FIRECRAWL_URL, LookupFailed, search_web

logger = logging.getLogger("oscar.whatsapp")

REQUEST_TIMEOUT_SECONDS = 90
MAX_OUTPUT_TOKENS = 900

# What the model is offered when Oscar may search. It decides when to use it.
WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the web for current information. Returns the top pages, with an excerpt "
            "of each, and recent news."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to search for, as you would type it into a search engine.",
                }
            },
            "required": ["query"],
        },
    },
}
# How many times one reply may go back to search again, and how many searches
# it may run each time. After that the model has to answer with what it has.
MAX_SEARCH_ROUNDS = 3
MAX_SEARCHES_PER_ROUND = 3

# What OpenRouter answers when a request asks for something the model or its
# provider cannot do (tools, photos). Worth one more try with less.
UNSUPPORTED_STATUS_CODES = (400, 404, 422)

NO_IMAGES_NOTE = (
    "\n\n[A photo was attached, but you cannot see images. "
    "Say so if the photo matters to the answer.]"
)


class BrainError(RuntimeError):
    """The model could not produce a reply; the message says why, for the dashboard."""


@dataclass(frozen=True)
class Image:
    data: bytes
    mime_type: str  # e.g. image/jpeg


def _user_content(text: str, images: list[Image]) -> str | list[dict]:
    if not images:
        return text
    parts: list[dict] = [{"type": "text", "text": text}]
    for image in images:
        encoded = base64.b64encode(image.data).decode()
        parts.append(
            {"type": "image_url", "image_url": {"url": f"data:{image.mime_type};base64,{encoded}"}}
        )
    return parts


def _message(response: httpx.Response) -> dict:
    """The model's message from a successful response."""
    try:
        body = response.json()
    except ValueError:
        raise BrainError("OpenRouter sent back something that is not JSON.") from None
    if isinstance(body, dict) and body.get("error"):
        # OpenRouter reports some provider failures inside a 200 response.
        raise BrainError(f"The model's provider reported an error. {error_detail(body)}".strip())
    try:
        message = body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        raise BrainError("The model sent back an empty answer.") from None
    if not isinstance(message, dict):
        raise BrainError("The model sent back an empty answer.")
    return message


def _reply_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, list):  # some providers return content parts
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if not isinstance(content, str) or not content.strip():
        raise BrainError("The model sent back an empty answer.")
    return content


def _tool_calls(message: dict) -> list[dict]:
    calls = message.get("tool_calls")
    return [call for call in calls if isinstance(call, dict)] if isinstance(calls, list) else []


def _query(call: dict) -> str:
    """What a tool call asks to search for. Empty when it is not a usable search."""
    function = call.get("function")
    function = function if isinstance(function, dict) else {}
    if function.get("name") != WEB_SEARCH_TOOL["function"]["name"]:
        return ""
    try:
        arguments = json.loads(function.get("arguments") or "{}")
    except ValueError:
        return ""
    query = arguments.get("query") if isinstance(arguments, dict) else None
    return query.strip() if isinstance(query, str) else ""


async def generate_reply(
    *,
    api_key: str,
    model: str,
    system_prompt: str,
    request_text: str,
    images: list[Image],
    web_search: bool,
    system_prompt_without_search: str | None = None,
    firecrawl_api_key: str = "",
    firecrawl_base_url: str = DEFAULT_FIRECRAWL_URL,
    search_problems: list[str] | None = None,
    base_url: str = DEFAULT_BASE_URL,
    client: httpx.AsyncClient | None = None,
) -> str:
    """Ask the model for a reply.

    Tries the fullest request first (web search and photos, when asked for). If
    OpenRouter says the model cannot do that, it retries without web search, then
    without photos. `system_prompt_without_search` is used whenever the model is
    not offered the search, so it is never told it can search when it cannot.

    Searching needs a Firecrawl key as well as `web_search`. A search that
    fails does not fail the reply: the model is told, and the reason is added
    to `search_problems` for the dashboard.
    """
    if not api_key:
        raise BrainError("OPENROUTER_API_KEY is not set in .env.")

    url = f"{base_url}/chat/completions"
    headers = auth_headers(api_key)
    own_client = None if client is not None else httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS)
    http = client or own_client

    async def post(body: dict) -> httpx.Response:
        return await http.post(url, json=body, headers=headers)

    def payload(search: bool, with_images: bool) -> dict:
        prompt = system_prompt if search or not web_search else (
            system_prompt_without_search or system_prompt
        )
        text = request_text if with_images or not images else request_text + NO_IMAGES_NOTE
        body: dict = {
            "model": model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": _user_content(text, images if with_images else [])},
            ],
            "max_tokens": MAX_OUTPUT_TOKENS,
        }
        if search:
            body["tools"] = [WEB_SEARCH_TOOL]
        return body

    async def searched(call: dict, allowed: bool) -> dict:
        """The result of one search the model asked for, as the message it gets back."""
        query = _query(call)
        if not query:
            found = "No search was run: the request gave nothing to search for."
        elif not allowed:
            found = "No search was run: that is too many searches at once."
        else:
            try:
                found = await search_web(
                    http, api_key=firecrawl_api_key, base_url=firecrawl_base_url, query=query
                ) or "The search found nothing."
            except LookupFailed as exc:
                logger.warning("Web search for %r failed: %s", query, exc)
                if search_problems is not None:
                    search_problems.append(str(exc))
                # The reason is for the owner (the dashboard), not for the chat:
                # told it, the model passed "an authentication error" on to the group.
                found = (
                    "The search could not be run just now. Say that you could not search, "
                    "and do not guess."
                )
        return {"role": "tool", "tool_call_id": call.get("id") or "", "content": found}

    async def search_until_answered(body: dict, response: httpx.Response) -> httpx.Response:
        """Run the searches the model asks for and hand back what they find, until it answers."""
        for round_number in range(1, MAX_SEARCH_ROUNDS + 1):
            if response.status_code != 200:
                break
            message = _message(response)
            calls = _tool_calls(message)
            if not calls:
                break
            results = await asyncio.gather(
                *(searched(call, n < MAX_SEARCHES_PER_ROUND) for n, call in enumerate(calls))
            )
            # Its own message goes back as it came: what it said, the searches
            # it asked for, and any reasoning the provider wants returned.
            asked = {
                key: message[key]
                for key in ("role", "content", "tool_calls", "reasoning_details")
                if message.get(key) is not None
            }
            body = {**body, "messages": [*body["messages"], asked, *results]}
            if round_number == MAX_SEARCH_ROUNDS:
                body["tool_choice"] = "none"  # it has searched enough: now it answers
            response = await post(body)
        return response

    # (web search, photos), from the fullest request to the plainest.
    attempts: list[tuple[bool, bool]] = []
    if web_search and firecrawl_api_key:
        attempts.append((True, bool(images)))
    attempts.append((False, bool(images)))
    if images:
        attempts.append((False, False))

    try:
        for number, (search, with_images) in enumerate(attempts, start=1):
            body = payload(search, with_images)
            response = await post(body)
            if response.status_code not in UNSUPPORTED_STATUS_CODES or number == len(attempts):
                break
            logger.warning(
                "OpenRouter refused the request (%s) with web search %s and photos %s; "
                "retrying with less",
                response.status_code,
                "on" if search else "off",
                "on" if with_images else "off",
            )
        if search:
            response = await search_until_answered(body, response)
    except httpx.TimeoutException:
        raise BrainError("The model took too long to answer.") from None
    except httpx.HTTPError as exc:
        raise BrainError(f"Could not reach OpenRouter: {exc}") from None
    finally:
        if own_client is not None:
            await own_client.aclose()

    if response.status_code != 200:
        raise BrainError(
            describe_failure(
                response,
                not_found="OpenRouter does not know that model. Check the model name in Agent settings.",
            )
        )
    return _reply_text(_message(response))
