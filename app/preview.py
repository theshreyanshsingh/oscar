"""A short sample of how Oscar sounds, for "Hear this voice" in Agent settings.

It is said with the voice, accent and name in the form, before they are saved,
and told the way a line is on a call (app/agent.py, line_instruction), so it
sounds as calls will. Each sample is kept in data/recordings/ (one per voice), so
playing the same one again costs nothing; one sample costs about a third of a cent.
"""

import base64
import json

import httpx

from app.agent import accent_reminder, line_instruction
from app.costs import USAGE
from app.openrouter import DEFAULT_BASE_URL, auth_headers, describe_failure, error_detail, new_client
from app.speech import VOICE_MODEL, VOICE_SAMPLE_RATE, Recordings, say_request, same_words, wav_file
from app.store import AgentSettings

SAMPLE_TEXT = "Hi, this is {name}. This is how I'll sound on your calls. How can I help you today?"
PREVIEW_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class PreviewFailed(Exception):
    """The sample could not be had. The message says why."""


def sample_text(agent: AgentSettings) -> str:
    return SAMPLE_TEXT.format(name=agent.name)


async def voice_sample(
    agent: AgentSettings,
    *,
    api_key: str,
    base_url: str = DEFAULT_BASE_URL,
    recordings: Recordings | None = None,
    client: httpx.AsyncClient | None = None,
) -> bytes:
    """The sample, as a WAV file. Raises PreviewFailed."""
    text = sample_text(agent)
    instructions = line_instruction(agent)
    reminder = accent_reminder(agent)
    key = Recordings.key(VOICE_MODEL, agent.voice, reminder, instructions, text)
    line = f"preview-{agent.voice}"
    kept = recordings.get(line, key) if recordings else None
    if kept:
        return wav_file(kept, VOICE_SAMPLE_RATE)

    messages = [{"role": "system", "content": instructions}]
    if reminder:
        messages.append({"role": "system", "content": reminder})
    messages.append({"role": "user", "content": say_request(text)})
    body = {
        "model": VOICE_MODEL,
        "stream": True,
        "modalities": ["text", "audio"],
        "audio": {"voice": agent.voice, "format": "pcm16"},
        "messages": messages,
        "usage": USAGE,
    }
    own_client = client is None
    client = client or new_client(timeout=PREVIEW_TIMEOUT)
    speech, said = bytearray(), ""
    try:
        async with client.stream(
            "POST", f"{base_url}/chat/completions", json=body, headers=auth_headers(api_key)
        ) as response:
            if response.status_code != 200:
                await response.aread()
                raise PreviewFailed(describe_failure(response))
            async for line_text in response.aiter_lines():
                if not line_text.startswith("data:") or line_text[5:].strip() == "[DONE]":
                    continue
                try:
                    chunk = json.loads(line_text[5:])
                except ValueError:
                    continue
                if isinstance(chunk, dict) and chunk.get("error"):
                    raise PreviewFailed(error_detail(chunk) or "The voice model's provider failed.")
                for choice in (chunk.get("choices") or []) if isinstance(chunk, dict) else []:
                    audio = (choice.get("delta") or {}).get("audio") or {}
                    if audio.get("data"):
                        try:
                            speech += base64.b64decode(audio["data"])
                        except ValueError:
                            pass
                    said += audio.get("transcript") or ""
    except httpx.HTTPError as exc:
        raise PreviewFailed(f"OpenRouter was unreachable ({type(exc).__name__}).") from None
    finally:
        if own_client:
            await client.aclose()
    audio = bytes(speech[: len(speech) & ~1])
    if not audio:
        raise PreviewFailed("The voice model sent no audio.")
    if recordings and same_words(said, text):
        recordings.put(line, key, audio)  # said as asked: worth keeping
    return wav_file(audio, VOICE_SAMPLE_RATE)
