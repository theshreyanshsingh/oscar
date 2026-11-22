"""Writing down WhatsApp voice notes (app/whatsapp/voice.py), with OpenRouter faked.

Run with the WhatsApp environment: .venv-whatsapp/bin/python -m pytest tests_whatsapp
"""

import base64
import json

import httpx
import pytest

from app.whatsapp import voice
from app.whatsapp.voice import TranscriptionFailed, audio_format, transcribe


def openrouter(answer) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    asked: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        return answer(request) if callable(answer) else answer

    return httpx.AsyncClient(transport=httpx.MockTransport(handle)), asked


def fails_with(error: Exception):
    def handle(request: httpx.Request) -> httpx.Response:
        raise error

    return handle


async def test_a_voice_note_is_sent_as_it_is_to_the_transcriber_calls_use():
    client, asked = openrouter(httpx.Response(200, json={"text": " Hi there. ", "usage": {"cost": 0.0002}}))
    words = await transcribe(
        api_key="sk-or-test", audio=b"OggS opus", mime_type="audio/ogg; codecs=opus",
        base_url="https://openrouter.test/api/v1", client=client,
    )
    assert words == "Hi there."
    (request,) = asked
    assert str(request.url) == "https://openrouter.test/api/v1/audio/transcriptions"
    assert request.headers["Authorization"] == "Bearer sk-or-test"
    body = json.loads(request.content)
    assert (body["model"], body["language"], body["usage"]) == ("deepgram/nova-3", "multi", {"include": True})
    assert body["input_audio"] == {"data": base64.b64encode(b"OggS opus").decode(), "format": "ogg"}


@pytest.mark.parametrize(
    ("mime_type", "format"),
    [
        ("audio/ogg; codecs=opus", "ogg"),
        ("audio/mpeg", "mp3"),
        ("audio/mp4", "m4a"),
        ("AUDIO/AAC", "aac"),
        ("", "ogg"),  # WhatsApp's own voice notes, when the type is missing
    ],
)
def test_the_format_comes_from_the_type_whatsapp_gives_the_file(mime_type, format):
    assert audio_format(mime_type) == format


async def test_a_voice_note_with_no_words_in_it_is_empty():
    client, _ = openrouter(httpx.Response(200, json={"text": ""}))
    assert await transcribe(api_key="k", audio=b"x", mime_type="audio/ogg", client=client) == ""


@pytest.mark.parametrize(
    ("answer", "why"),
    [
        (httpx.Response(402, json={"error": {"message": "Insufficient credits"}}), "out of credits"),
        (httpx.Response(200, text="not json"), "not a transcript"),
        (fails_with(httpx.ConnectError("refused")), "Could not reach OpenRouter"),
        (fails_with(httpx.ReadTimeout("slow")), "took too long"),
    ],
)
async def test_a_failure_says_why(answer, why):
    client, _ = openrouter(answer)
    with pytest.raises(TranscriptionFailed, match=why):
        await transcribe(api_key="k", audio=b"x", mime_type="audio/ogg", client=client)


async def test_without_a_key_nothing_is_sent():
    client, asked = openrouter(httpx.Response(200, json={"text": "x"}))
    with pytest.raises(TranscriptionFailed, match="OPENROUTER_API_KEY"):
        await transcribe(api_key="", audio=b"x", mime_type="audio/ogg", client=client)
    assert asked == []


def test_the_transcriber_is_the_one_calls_use():
    # The WhatsApp process cannot load app/speech.py (it has no voice pipeline), so the
    # model and language are written down twice. They must not drift apart.
    speech = (voice.__file__.rsplit("/whatsapp/", 1)[0]) + "/speech.py"
    with open(speech) as source:
        text = source.read()
    assert f'TRANSCRIPT_MODEL = "{voice.TRANSCRIPT_MODEL}"' in text
    assert f'TRANSCRIPT_LANGUAGE = "{voice.TRANSCRIPT_LANGUAGE}"' in text
