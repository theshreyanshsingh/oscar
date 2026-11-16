"""Stand-ins for OpenRouter and Firecrawl that answer the way the real services do.

OpenRouter's two endpoints, the ones a call uses:

    POST /api/v1/chat/completions       -> the speech-to-speech model: a streamed
                                           answer as audio plus its text
    POST /api/v1/audio/transcriptions   -> {"text": ...}

The streamed answer copies what openai/gpt-audio sent through OpenRouter on
2026-11-11: chunks whose delta carries `audio.transcript` (text) and
`audio.data` (base64, 16-bit mono at 24 kHz), arriving faster than they play.

With web search set up, the chat endpoint gets two more kinds of request, both
answered in one piece of JSON as the real service answers them: the decision
whether a turn needs a search (the speech model asked for text only), and the
reading of search results (a text model).

With a script, a text model is asked after each reply which step Oscar is on
(app/script.py). When a call ends, the chat endpoint gets one more request: the
transcript, for a text model to sum up (app/summary.py). And one beside it when
the call searched the web: the transcript and the pages found, for a text model
to write up (app/research.py). All are answered in one piece.

Firecrawl's one endpoint:

    POST /v2/search   -> {"success": true, "data": {"web": [...], "news": [...]}}

Every request is recorded so tests can check exactly what Oscar sent.

A spoken answer can be refused once, the way the real service refused one on
2026-11-13 (HTTP 503, "Provider returned error"): put its words in
`app.state.refuse_once`.
"""

import asyncio
import base64
import io
import json
import math
import re
import struct
import wave

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

SPEECH_RATE = 24000


def tone(seconds: float, rate: int = SPEECH_RATE) -> bytes:
    """16-bit mono audio of a steady 440 Hz tone."""
    count = int(seconds * rate)
    return struct.pack(
        f"<{count}h", *(int(8000 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(count))
    )


def spoken_seconds(text: str) -> float:
    """How long the stand-in takes to "say" a text: 60 ms a character, 2 s at most."""
    return min(2.0, 0.06 * len(text))


def sse(delta: dict, finish_reason: str | None = None) -> str:
    body = {
        "id": "gen-test",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "openai/gpt-audio",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(body)}\n\n"


def answer_stream(text: str, *, piece_bytes: int = 4800):
    """The chunks of one spoken answer: its words, then its audio, then the end."""
    yield sse({"role": "assistant", "content": ""})
    words = text.split(" ")
    for index, word in enumerate(words):
        yield sse({"audio": {"transcript": word + (" " if index < len(words) - 1 else "")}})
    audio = tone(spoken_seconds(text))
    for start in range(0, len(audio), piece_bytes):
        piece = audio[start : start + piece_bytes]
        yield sse({"audio": {"data": base64.b64encode(piece).decode()}})
    yield sse({}, finish_reason="stop")
    yield "data: [DONE]\n\n"


def requested_greeting(body: dict) -> str | None:
    """The greeting Oscar asked to have spoken word for word, if this is that request."""
    last = body["messages"][-1]["content"]
    if not isinstance(last, str):
        return None
    match = re.search(r'saying exactly this and nothing else: "(.*)"\]$', last, flags=re.DOTALL)
    return match.group(1) if match else None


def requested_line(body: dict) -> str | None:
    """The line Oscar handed the model to say word for word (during a web search), if any."""
    last = body["messages"][-1]["content"]
    if not isinstance(last, str):
        return None
    match = re.search(
        r'^\[Say exactly this to the caller, and nothing else: "(.*)"\]$', last, flags=re.DOTALL
    )
    return match.group(1) if match else None


def whole_answer(text: str, model: str) -> dict:
    """A chat answer that arrives in one piece (a request that did not ask for a stream)."""
    return {
        "id": "gen-test",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
        ],
    }


def is_summary_request(body: dict) -> bool:
    """Whether a chat request asks for the note written when a call ends."""
    first = body["messages"][0]["content"]
    return isinstance(first, str) and first.startswith("You write the note")


def is_research_request(body: dict) -> bool:
    """Whether a chat request asks for the notes on what a call's searches found."""
    first = body["messages"][0]["content"]
    return isinstance(first, str) and first.startswith("You write the research notes")


def is_script_request(body: dict) -> bool:
    """Whether a chat request asks which step of its script Oscar is on."""
    first = body["messages"][0]["content"]
    return isinstance(first, str) and "working through a script" in first


NO_RESEARCH = json.dumps({"notes": []})

NO_FOLLOW_UP = json.dumps(
    {
        "summary": "The caller asked when the shop opens tomorrow and was told nine in the morning.",
        "caller_name": "",
        "callback_number": "",
        "follow_up": "",
    }
)


def clip_seconds(body: dict) -> float:
    """How long the WAV clip in a transcription request is."""
    with wave.open(io.BytesIO(base64.b64decode(body["input_audio"]["data"]))) as clip:
        return clip.getnframes() / clip.getframerate()


def turn_seconds(body: dict) -> float:
    """How long the caller's turn (the audio that ends a chat request) is."""
    return clip_seconds(body["messages"][-1]["content"][0])


def create_fake_openrouter(
    *,
    heard: str,
    reply: str,
    api_key: str,
    answer_delay: float = 0.0,
    heard_in_brief_clips: str | None = None,
    decision: str = "NO",
    found: str = "",
    summary: str = NO_FOLLOW_UP,
    research: str = NO_RESEARCH,
    step: str = "2",
) -> FastAPI:
    """`heard_in_brief_clips` is the transcript of clips under 1.5 seconds, when it differs.

    `decision` answers "does this turn need a web search?" (always "NO" for a
    turn under 1.5 seconds), `found` is what the model reading the search
    results writes, `summary` is what the model summing up the call writes,
    `research` what the model writing up its searches writes, and `step` is the
    step of the script the model keeping Oscar's place names.
    """
    app = FastAPI()
    app.state.requests = []  # {"endpoint", "body"} in arrival order
    app.state.refuse_once = set()  # spoken answers to refuse the first time they are asked for

    async def record(request: Request, endpoint: str) -> dict | JSONResponse:
        if request.headers.get("authorization") != f"Bearer {api_key}":
            return JSONResponse({"error": {"message": "No auth credentials found"}}, status_code=401)
        body = await request.json()
        if endpoint == "chat" and not body.get("stream"):
            # Not a spoken answer: the search decision (the speech model asked
            # for text only), or a text model reading search results, summing
            # up the call or writing up its searches.
            if body.get("modalities") == ["text"]:
                endpoint = "decision"
            elif is_script_request(body):
                endpoint = "script"
            elif is_research_request(body):
                endpoint = "research"
            else:
                endpoint = "summary" if is_summary_request(body) else "reading"
        app.state.requests.append({"endpoint": endpoint, "body": body})
        return body

    @app.post("/api/v1/audio/transcriptions")
    async def transcriptions(request: Request):
        body = await record(request, "transcriptions")
        if isinstance(body, JSONResponse):
            return body
        brief = heard_in_brief_clips is not None and clip_seconds(body) < 1.5
        text = heard_in_brief_clips if brief else heard
        return {"text": text, "usage": {"seconds": 2.0, "cost": 0.0001}}

    @app.post("/api/v1/chat/completions")
    async def chat_completions(request: Request):
        body = await record(request, "chat")
        if isinstance(body, JSONResponse):
            return body
        if not body.get("stream"):
            if body.get("modalities") != ["text"]:
                if is_script_request(body):
                    return whole_answer(step, body["model"])
                if is_research_request(body):
                    return whole_answer(research, body["model"])
                text = summary if is_summary_request(body) else found
                return whole_answer(text, body["model"])
            return whole_answer("NO" if turn_seconds(body) < 1.5 else decision, body["model"])
        text = requested_greeting(body) or requested_line(body) or reply
        if text in app.state.refuse_once:
            app.state.refuse_once.discard(text)
            return JSONResponse(
                {"error": {"message": "Provider returned error", "code": 503}}, status_code=503
            )

        async def stream():
            await asyncio.sleep(answer_delay)
            for chunk in answer_stream(text):
                yield chunk
                await asyncio.sleep(0.005)  # faster than the audio plays, like the real model

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app


def create_fake_firecrawl(
    *, api_key: str, web: list[dict] | None = None, news: list[dict] | None = None
) -> FastAPI:
    """Answers searches with `web` and `news`, the way Firecrawl's v2 API was seen to."""
    app = FastAPI()
    app.state.requests = []  # request bodies in arrival order

    @app.post("/v2/search")
    async def search(request: Request):
        if request.headers.get("authorization") != f"Bearer {api_key}":
            return JSONResponse(
                {"success": False, "error": "Unauthorized: Invalid token"}, status_code=401
            )
        app.state.requests.append(await request.json())
        data = {"web": web or []}
        if news is not None:
            data["news"] = news
        return {"success": True, "data": data, "creditsUsed": 2, "id": "search-test"}

    return app
