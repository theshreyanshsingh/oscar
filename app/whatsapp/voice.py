"""Writing down what a WhatsApp voice note says, so Oscar can answer it.

Asks OpenRouter's transcription endpoint, with the transcriber Oscar's calls
use (app/speech.py). Only httpx is needed: the WhatsApp process has none of
the voice pipeline's packages.
"""

import base64

import httpx

from app.costs import USAGE
from app.openrouter import DEFAULT_BASE_URL, auth_headers, describe_failure

# The same as on calls (app/speech.py): "multi" takes several languages, mixed
# in one sentence too, where the transcriber left to itself expects English.
TRANSCRIPT_MODEL = "deepgram/nova-3"
TRANSCRIPT_LANGUAGE = "multi"
REQUEST_TIMEOUT_SECONDS = 60

# What a voice note's file is, from the type WhatsApp gives it. Voice notes
# recorded in WhatsApp are Ogg files of Opus audio ("audio/ogg; codecs=opus"),
# which the transcriber takes as they are.
FORMATS = {
    "audio/ogg": "ogg",
    "audio/opus": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "audio/aac": "aac",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/webm": "webm",
    "audio/flac": "flac",
}


class TranscriptionFailed(RuntimeError):
    """The voice note could not be written down; the message says why, for the dashboard."""


def audio_format(mime_type: str) -> str:
    """The format to tell the transcriber, from a type such as "audio/ogg; codecs=opus"."""
    return FORMATS.get(mime_type.split(";", 1)[0].strip().lower(), "ogg")


async def transcribe(
    *,
    api_key: str,
    audio: bytes,
    mime_type: str,
    base_url: str = DEFAULT_BASE_URL,
    client: httpx.AsyncClient | None = None,
) -> str:
    """The words in a voice note, or "" when there were none to make out."""
    if not api_key:
        raise TranscriptionFailed("OPENROUTER_API_KEY is not set in .env.")
    payload = {
        "model": TRANSCRIPT_MODEL,
        "language": TRANSCRIPT_LANGUAGE,
        "usage": USAGE,
        "input_audio": {"data": base64.b64encode(audio).decode(), "format": audio_format(mime_type)},
    }
    own_client = None if client is not None else httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS)
    try:
        response = await (client or own_client).post(
            f"{base_url}/audio/transcriptions", json=payload, headers=auth_headers(api_key)
        )
    except httpx.TimeoutException:
        raise TranscriptionFailed("The transcriber took too long to answer.") from None
    except httpx.HTTPError as exc:
        raise TranscriptionFailed(f"Could not reach OpenRouter: {exc}") from None
    finally:
        if own_client is not None:
            await own_client.aclose()
    if response.status_code != 200:
        raise TranscriptionFailed(describe_failure(response))
    try:
        text = response.json().get("text")
    except (ValueError, AttributeError):
        raise TranscriptionFailed("OpenRouter sent back something that is not a transcript.") from None
    return text.strip() if isinstance(text, str) else ""
