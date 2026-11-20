"""Oscar's ears and voice on calls (app/speech.py).

The voice service runs inside a real Pipecat pipeline; only OpenRouter itself
is faked, answering the way the real service was seen to answer.
"""

import asyncio
import base64
import io
import json
import wave
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    ErrorFrame,
    InputAudioRawFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMServiceMetadataFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.pipeline.worker import PipelineParams
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.tests.utils import SleepFrame, run_test

from app import speech
from app.script import ScriptLost
from app.search import FactsRequest, LookupFailed, SearchRequest, Source
from app.speech import (
    ANSWER_ATTEMPTS,
    FACTS_FAILED_LINE,
    HOLD_LINE,
    SEARCH_FAILED_LINE,
    SPOKEN_CHARS_PER_SECOND,
    STILL_LOOKING_LINE,
    TRANSCRIPT_LANGUAGE,
    TRANSCRIPT_MODEL,
    VOICE_MODEL,
    EchoGate,
    GreetingFrame,
    OpenRouterVoiceService,
    Recordings,
    SearchingFrame,
    TurnTiming,
    greeting_request,
    same_words,
    say_request,
    wav_file,
)
from tests.fake_openrouter import (
    answer_stream,
    requested_greeting,
    requested_line,
    spoken_seconds,
    sse,
    tone,
)

BASE_URL = "https://openrouter.test/api/v1"
INSTRUCTIONS = "Your name is Oscar.\n\nBe brief."
HEARD = "What time do you open tomorrow?"
REPLY = "We open at nine in the morning."  # the stand-in "speaks" it for 1.86 seconds
REPLY_SAMPLES = int(0.06 * len(REPLY) * 24000)


# A stand-in for OpenRouter


def spoken(text: str = REPLY, **stream_options):
    """A chat endpoint that answers with `text` as speech."""
    body = "".join(answer_stream(text, **stream_options))
    return lambda request: httpx.Response(
        200, content=body.encode(), headers={"content-type": "text/event-stream"}
    )


def slowly(text: str = REPLY, *, first_after: float = 0.0, gap: float = 0.1):
    """A chat endpoint whose answer arrives a piece at a time."""

    def respond(request):
        async def pieces():
            await asyncio.sleep(first_after)
            for chunk in answer_stream(text):
                yield chunk.encode()
                if '"data"' in chunk:
                    await asyncio.sleep(gap)

        return httpx.Response(200, content=pieces(), headers={"content-type": "text/event-stream"})

    return respond


def saying_lines(reply: str = REPLY, *, hold_line: str = HOLD_LINE):
    """A chat endpoint that says a line it is handed word for word, and `reply` to anything else.

    `hold_line` is what it says when handed the "one moment" line, for a model
    that will not say it.
    """

    def respond(request):
        body = json.loads(request.content)
        line = requested_line(body)
        if line == HOLD_LINE:
            line = hold_line
        return spoken(requested_greeting(body) or line or reply)(request)

    return respond


# The pages a search by the stand-in below finds.
PAGES = [Source(title="Kanpur weather", url="https://weather.example/kanpur", excerpt="Sunny, 30°.")]


class Lookup:
    """A stand-in for app.search.WebLookup."""

    def __init__(
        self,
        search: SearchRequest | None = None,
        *,
        answer: str = "It is sunny in Kanpur.",
        failure: str = "",
        decide_after: float = 0.0,
        answer_after: float = 0.0,
        cannot_decide: str = "",
        has_facts: bool = False,
        further: str = "",
    ):
        self.has_facts = has_facts
        self._further = further  # what a second search in the same turn is for, if there is one
        self.earlier: list[list] = []  # the call so far, as each lookup was given it
        self.facts_asked: list[tuple[str, list]] = []  # (question, earlier turns) of each
        self._search, self._answer, self._failure = search, answer, failure
        self._cannot_decide = cannot_decide
        self._decide_after, self._answer_after = decide_after, answer_after
        self.decided: list[tuple[list, dict]] = []  # (earlier turns, the turn) of each decision
        self.asked: list[tuple[str, str]] = []  # (question, query) of each lookup
        self.gave_up_deciding = False
        self.closed = False

    async def decide(self, earlier, turn):
        self.decided.append((earlier, turn))
        try:
            await asyncio.sleep(self._decide_after)
        except asyncio.CancelledError:
            self.gave_up_deciding = True
            raise
        if self._cannot_decide:
            raise LookupFailed(self._cannot_decide)
        return self._search

    async def answer(self, *, question, query, earlier=(), searched=None, again=None):
        self.asked.append((question, query))
        self.earlier.append(list(earlier))
        await asyncio.sleep(self._answer_after)
        if self._failure:
            raise LookupFailed(self._failure)
        if searched:
            await searched(query, PAGES)
        if self._further and again:
            # The first results called for one more search.
            await asyncio.gather(asyncio.sleep(self._answer_after), again(self._further))
            await searched(self._further, [])
        return self._answer

    async def answer_from_facts(self, question, earlier):
        self.facts_asked.append((question, earlier))
        await asyncio.sleep(self._answer_after)
        if self._failure:
            raise LookupFailed(self._failure)
        return self._answer

    async def aclose(self):
        self.closed = True


class Script:
    """A stand-in for app.script.ScriptKeeper: each time it is asked, Oscar is one step further on."""

    def __init__(self, *, after: float = 0.0, lost: str = ""):
        self.place = 0
        self.talks: list[list[dict]] = []  # the call so far, each time the place was asked for
        self.gave_up = 0  # how often it was stopped before it had answered
        self.closed = False
        self._after, self._lost = after, lost

    def note(self) -> str:
        return f"You are on step {self.place + 1}."

    async def find_place(self, talk):
        self.talks.append(talk)
        try:
            await asyncio.sleep(self._after)
        except asyncio.CancelledError:
            self.gave_up += 1
            raise
        if self._lost:
            raise ScriptLost(self._lost)
        self.place += 1
        return self.place

    async def aclose(self):
        self.closed = True


def written(text: str = HEARD):
    """A transcription endpoint that hears `text`."""
    return lambda request: httpx.Response(200, json={"text": text, "usage": {"seconds": 1.0}})


# Driving the service


def audio_frames(pcm: bytes, rate: int) -> list[InputAudioRawFrame]:
    size = rate // 50 * 2  # 20 ms
    return [
        InputAudioRawFrame(audio=pcm[i : i + size], sample_rate=rate, num_channels=1)
        for i in range(0, len(pcm), size)
    ]


def caller_turn(seconds: float = 1.0, *, rate: int = 16000) -> list:
    """The frames of one turn, as turn-taking delivers them.

    The caller is quiet, then speaks for `seconds`. Their first 0.2 seconds
    arrive before the turn is known to have started, as on a real call.
    """
    voice = tone(seconds, rate)
    early = int(0.2 * rate) * 2
    return [
        *audio_frames(b"\x00\x00" * rate, rate),
        *audio_frames(voice[:early], rate),
        UserStartedSpeakingFrame(),
        *audio_frames(voice[early:], rate),
        UserStoppedSpeakingFrame(),
    ]


async def run_voice(
    frames,
    *,
    chat=None,
    transcription=None,
    context: LLMContext | None = None,
    reminder: str = "",
    lookup: Lookup | None = None,
    script: Script | None = None,
    script_reminder: str = "",
    line_instructions: str = "",
    service: type[OpenRouterVoiceService] = OpenRouterVoiceService,
    in_rate: int = 16000,
    out_rate: int = 24000,
    recordings: Recordings | None = None,
    warm_up: bool = False,
    meter=None,
):
    chat = chat or spoken()
    transcription = transcription or written()
    requests: list[httpx.Request] = []

    def openrouter(request: httpx.Request):
        requests.append(request)
        return (chat if request.url.path.endswith("/chat/completions") else transcription)(request)

    service = service(
        api_key="sk-or-test",
        voice="cedar",
        instructions=INSTRUCTIONS,
        reminder=reminder,
        lookup=lookup,
        script=script,
        script_reminder=script_reminder,
        line_instructions=line_instructions,
        context=context or LLMContext(),
        base_url=BASE_URL,
        client=httpx.AsyncClient(transport=httpx.MockTransport(openrouter)),
        recordings=recordings,
        warm_up=warm_up,
        meter=meter,
    )
    searches: list[tuple[str, str]] = []  # (what was searched for, what went wrong)
    pages: list[list[Source]] = []  # the pages each of them found

    @service.event_handler("on_web_search")
    async def on_web_search(_, query, problem, sources):
        searches.append((query, problem))
        pages.append(sources)

    facts_failures: list[str] = []  # why an answer could not be had from the fact sheet

    @service.event_handler("on_facts_failed")
    async def on_facts_failed(_, problem):
        facts_failures.append(problem)

    script_losses: list[str] = []  # why Oscar's place in its script could not be worked out

    @service.event_handler("on_script_lost")
    async def on_script_lost(_, problem):
        script_losses.append(problem)

    down, up = await run_test(
        service,
        frames_to_send=frames,
        pipeline_params=PipelineParams(
            audio_in_sample_rate=in_rate, audio_out_sample_rate=out_rate
        ),
    )

    def sent_to(path: str) -> list[dict]:
        return [json.loads(r.content) for r in requests if r.url.path.endswith(path)]

    def of(kind) -> list:
        return [frame for frame in down if isinstance(frame, kind)]

    return SimpleNamespace(
        down=down,
        up=up,
        of=of,
        requests=requests,
        chat=sent_to("/chat/completions"),
        transcriptions=sent_to("/audio/transcriptions"),
        audio=of(TTSAudioRawFrame),
        text="".join(frame.text for frame in of(TTSTextFrame)),
        heard=[frame.text for frame in up if isinstance(frame, TranscriptionFrame)],
        errors=[frame.error for frame in up if isinstance(frame, ErrorFrame)],
        searches=searches,
        pages=pages,
        facts_failures=facts_failures,
        script_losses=script_losses,
        searching=[frame.active for frame in up if isinstance(frame, SearchingFrame)],
    )


def clip(message: dict) -> tuple[int, float, np.ndarray]:
    """(sample rate, seconds, samples) of the caller audio in a chat message."""
    (part,) = message["content"]
    assert part["type"] == "input_audio" and part["input_audio"]["format"] == "wav"
    with wave.open(io.BytesIO(base64.b64decode(part["input_audio"]["data"]))) as wav:
        assert (wav.getnchannels(), wav.getsampwidth()) == (1, 2)
        samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
        return wav.getframerate(), wav.getnframes() / wav.getframerate(), samples


def seconds_of_voice(samples: np.ndarray, rate: int) -> float:
    """How much of a clip is the caller's tone rather than silence."""
    windows = samples[: len(samples) // 160 * 160].reshape(-1, 160)
    return float((np.abs(windows).max(axis=1) > 4000).sum() * 160 / rate)


def voice_sent(body: dict) -> float:
    """Seconds of the caller's voice in the audio a chat request carries."""
    rate, _, samples = clip(body["messages"][-1])
    return seconds_of_voice(samples, rate)


def samples_in(frames) -> int:
    return sum(len(frame.audio) for frame in frames) // 2


# Answering the caller


@pytest.mark.parametrize("in_rate", [16000, 8000])  # browser, phone
async def test_the_callers_turn_goes_to_the_model_as_audio(in_rate):
    call = await run_voice([*caller_turn(1.0, rate=in_rate), SleepFrame(0.5)], in_rate=in_rate)
    assert call.errors == []

    request = next(r for r in call.requests if r.url.path.endswith("/chat/completions"))
    assert request.method == "POST"
    assert str(request.url) == f"{BASE_URL}/chat/completions"
    assert request.headers["authorization"] == "Bearer sk-or-test"

    (body,) = call.chat
    assert set(body) == {"model", "stream", "modalities", "audio", "messages", "usage"}
    assert (body["model"], body["stream"]) == (VOICE_MODEL, True)
    assert body["modalities"] == ["text", "audio"]
    assert body["audio"] == {"voice": "cedar", "format": "pcm16"}
    system, turn = body["messages"]
    assert system == {"role": "system", "content": INSTRUCTIONS}
    assert turn["role"] == "user"

    # A real WAV file at the call's own sample rate, holding everything the
    # caller said: also the start of it, from before the turn was detected.
    rate, seconds, samples = clip(turn)
    assert rate == in_rate
    assert seconds == pytest.approx(1.4, abs=0.05)  # 0.4 s of quiet lead-in, then the speech
    assert seconds_of_voice(samples, rate) == pytest.approx(1.0, abs=0.03)


async def test_the_reply_is_spoken_and_written_down():
    call = await run_voice([*caller_turn(), SleepFrame(0.5)])
    assert call.errors == []

    # The audio comes out exactly as the model sent it (the call is at 24 kHz too).
    assert all(f.sample_rate == 24000 and f.num_channels == 1 for f in call.audio)
    assert b"".join(f.audio for f in call.audio) == tone(0.06 * len(REPLY))
    assert call.text == REPLY

    # One reply: opened, spoken, closed, in that order.
    kinds = (LLMFullResponseStartFrame, TTSStartedFrame, TTSStoppedFrame, LLMFullResponseEndFrame)
    marks = [type(frame) for frame in call.down if isinstance(frame, kinds)]
    assert marks == list(kinds)
    order = [f for f in call.down if isinstance(f, (*kinds, TTSAudioRawFrame, TTSTextFrame))]
    assert isinstance(order[1], TTSStartedFrame) and isinstance(order[2], TTSAudioRawFrame)
    assert isinstance(order[-2], TTSStoppedFrame)


async def test_reply_words_follow_the_audio_they_belong_to():
    # The model's text runs ahead of its audio (here all of it comes first).
    # Passing it on at once would record words as said before they were played.
    call = await run_voice([*caller_turn(), SleepFrame(0.5)])
    played = 0.0
    written_chars = 0
    for frame in call.down:
        if isinstance(frame, TTSAudioRawFrame):
            played += len(frame.audio) / 2 / frame.sample_rate
        elif isinstance(frame, TTSTextFrame) and played < 0.06 * len(REPLY) - 0.01:
            written_chars += len(frame.text)
            assert written_chars <= played * SPOKEN_CHARS_PER_SECOND
    assert written_chars > len(REPLY) / 2  # most of it was passed on along the way
    assert call.text == REPLY  # and all of it by the end


async def test_the_reply_is_resampled_for_phone_calls():
    # The model speaks at 24 kHz; a phone line is 8 kHz. Same sound, same length.
    call = await run_voice(
        [*caller_turn(rate=8000), SleepFrame(0.5)], in_rate=8000, out_rate=8000
    )
    assert call.errors == []
    assert all(frame.sample_rate == 8000 for frame in call.audio)
    assert samples_in(call.audio) == pytest.approx(REPLY_SAMPLES / 3, rel=0.02)
    loudest = max(np.abs(np.frombuffer(frame.audio, dtype="<i2")).max() for frame in call.audio)
    assert loudest > 4000  # still the tone, not silence or noise


async def test_audio_arriving_in_odd_sized_pieces_is_put_back_together():
    # 333 bytes at a time splits samples in half.
    call = await run_voice([*caller_turn(), SleepFrame(0.5)], chat=spoken(piece_bytes=333))
    assert call.errors == []
    assert all(len(frame.audio) % 2 == 0 for frame in call.audio)
    assert b"".join(frame.audio for frame in call.audio) == tone(0.06 * len(REPLY))


async def test_the_callers_words_are_written_down_for_the_transcript():
    call = await run_voice(
        [*caller_turn(), SleepFrame(0.5)], transcription=written(f"  {HEARD} ")
    )
    assert call.heard == [HEARD]
    (body,) = call.transcriptions
    assert set(body) == {"model", "language", "input_audio", "usage"}
    assert body["model"] == TRANSCRIPT_MODEL
    # Not English alone: a caller's Hindi used to be written as nothing, and
    # what is not written down is not remembered on the next turn.
    assert body["language"] == TRANSCRIPT_LANGUAGE == "multi"
    assert body["input_audio"]["format"] == "wav"
    with wave.open(io.BytesIO(base64.b64decode(body["input_audio"]["data"]))) as wav:
        assert wav.getframerate() == 16000 and wav.getnframes() / 16000 > 1.0


@pytest.mark.parametrize(
    "transcription",
    [
        written("   "),  # a cough or background noise
        lambda request: httpx.Response(500, text="upstream exploded"),
        lambda request: httpx.Response(200, text="<html>gateway</html>"),
        lambda request: httpx.Response(200, json=["unexpected"]),
    ],
)
async def test_a_missing_transcript_does_not_get_in_the_way_of_the_reply(transcription):
    # The model hears the caller's audio itself; the transcript is only for show.
    call = await run_voice([*caller_turn(), SleepFrame(0.5)], transcription=transcription)
    assert (call.heard, call.errors) == ([], [])
    assert call.text == REPLY and samples_in(call.audio) == REPLY_SAMPLES


async def test_a_blip_too_short_to_be_speech_is_not_sent():
    blip = [
        UserStartedSpeakingFrame(),
        *audio_frames(tone(0.04, 16000), 16000),
        UserStoppedSpeakingFrame(),
        SleepFrame(0.3),
    ]
    call = await run_voice(blip)
    assert (call.requests, call.audio, call.errors) == ([], [], [])


async def test_the_model_is_announced_as_speech_to_speech():
    # This is what makes turn-taking end a turn without waiting for a transcript.
    call = await run_voice([SleepFrame(0.1)])
    (metadata,) = call.of(LLMServiceMetadataFrame)
    assert metadata.is_realtime_service is True


# The greeting


async def test_the_greeting_is_asked_for_word_for_word():
    call = await run_voice(
        [GreetingFrame("Hi, this is Oscar."), SleepFrame(0.5)], chat=spoken("Hi, this is Oscar.")
    )
    assert call.errors == []
    (body,) = call.chat
    assert body["messages"] == [
        {"role": "system", "content": INSTRUCTIONS},
        {"role": "user", "content": greeting_request("Hi, this is Oscar.")},
    ]
    assert call.text == "Hi, this is Oscar."
    assert samples_in(call.audio) == int(0.06 * len("Hi, this is Oscar.") * 24000)
    assert call.of(GreetingFrame) == []  # an instruction to the service, not passed on
    assert call.transcriptions == []


GREETING = "Hi, this is Oscar."


def refusing(request):
    """A chat endpoint that should not be asked anything."""
    return httpx.Response(500, json={"error": {"message": "not expected"}})


async def test_the_greeting_is_recorded_once_and_played_from_then_on(tmp_path):
    recordings = Recordings(tmp_path)
    first = await run_voice(
        [GreetingFrame(GREETING), SleepFrame(0.5)], chat=spoken(GREETING), recordings=recordings
    )
    assert len(first.chat) == 1
    assert len(list(tmp_path.glob("greeting-*.pcm"))) == 1

    # The next call plays it at once, without asking the model.
    again = await run_voice(
        [GreetingFrame(GREETING), SleepFrame(0.5)], chat=refusing, recordings=recordings
    )
    assert again.chat == [] and again.errors == []
    assert again.text == GREETING
    assert samples_in(again.audio) == samples_in(first.audio) == int(0.06 * len(GREETING) * 24000)
    assert len(again.of(LLMFullResponseStartFrame)) == len(again.of(LLMFullResponseEndFrame)) == 1
    assert len(again.of(TTSStartedFrame)) == len(again.of(TTSStoppedFrame)) == 1


async def test_a_greeting_said_otherwise_is_not_kept(tmp_path):
    call = await run_voice(
        [GreetingFrame(GREETING), SleepFrame(0.5)],
        chat=spoken("Hello there, Oscar speaking."),
        recordings=Recordings(tmp_path),
    )
    assert call.text == "Hello there, Oscar speaking."  # said as it came
    assert list(tmp_path.iterdir()) == []


async def test_a_greeting_cut_off_is_not_kept(tmp_path):
    call = await run_voice(
        [GreetingFrame(GREETING), SleepFrame(0.15), InterruptionFrame(), SleepFrame(0.5)],
        chat=slowly(GREETING, gap=0.1),
        recordings=Recordings(tmp_path),
    )
    assert call.audio  # some of it was said
    assert list(tmp_path.iterdir()) == []


def failing_part_way(text: str):
    """A chat endpoint whose answer stops with the provider's error before its last audio."""
    chunks = list(answer_stream(text))
    body = "".join(chunks[:-3]) + PROVIDER_ERROR
    return lambda request: httpx.Response(
        200, content=body.encode(), headers={"content-type": "text/event-stream"}
    )


async def test_a_greeting_that_failed_part_way_is_not_kept(tmp_path):
    call = await run_voice(
        [GreetingFrame(GREETING), SleepFrame(0.5)],
        chat=failing_part_way(GREETING),
        recordings=Recordings(tmp_path),
    )
    assert call.audio and call.errors
    assert list(tmp_path.iterdir()) == []


async def test_a_new_greeting_is_recorded_in_place_of_the_old(tmp_path):
    recordings = Recordings(tmp_path)
    await run_voice([GreetingFrame(GREETING), SleepFrame(0.5)], chat=spoken(GREETING), recordings=recordings)
    (old,) = tmp_path.glob("greeting-*.pcm")
    changed = "Hello, Oscar here. How can I help?"
    call = await run_voice(
        [GreetingFrame(changed), SleepFrame(0.5)], chat=spoken(changed), recordings=recordings
    )
    assert len(call.chat) == 1  # a recording of other words is no use
    (new,) = tmp_path.glob("greeting-*.pcm")
    assert new != old


def test_a_recording_depends_on_everything_that_shapes_how_it_sounds(tmp_path):
    def key(**changes):
        options = {"voice": "cedar", "reminder": "(Indian accent.)", "instructions": INSTRUCTIONS, **changes}
        service = OpenRouterVoiceService(
            api_key="sk-or-test",
            voice=options["voice"],
            instructions=options["instructions"],
            reminder=options["reminder"],
            context=LLMContext(),
            recordings=Recordings(tmp_path),
        )
        return service._recording_key(options["instructions"], GREETING)

    keys = {key(), key(voice="marin"), key(reminder="(British accent.)"), key(instructions="Be chatty.")}
    assert len(keys) == 4
    assert key() == key()


async def test_a_recorded_hold_line_is_not_asked_for_again(tmp_path):
    recordings = Recordings(tmp_path)
    first = await run_voice(
        [*caller_turn(), SleepFrame(0.8)], chat=saying_lines(), lookup=Lookup(SEARCH, answer=FOUND),
        recordings=recordings,
    )
    assert len(list(tmp_path.glob("hold-line-*.pcm"))) == 1
    assert any(body["messages"][-1]["content"] == say_request(HOLD_LINE) for body in first.chat)

    again = await run_voice(
        [*caller_turn(), SleepFrame(0.8)], chat=saying_lines(), lookup=Lookup(SEARCH, answer=FOUND),
        recordings=recordings,
    )
    assert not any(body["messages"][-1]["content"] == say_request(HOLD_LINE) for body in again.chat)
    assert again.text == f"{HOLD_LINE} {FOUND}"
    assert samples_in(again.audio) == samples_in(first.audio)


async def test_warming_up_opens_a_connection_as_the_call_starts():
    call = await run_voice([SleepFrame(0.2)], warm_up=True)
    assert [(r.method, r.url.path) for r in call.requests] == [("GET", "/api/v1/models/count")]


async def test_no_warm_up_unless_asked():
    call = await run_voice([SleepFrame(0.2)])
    assert call.requests == []


def test_the_timing_of_a_reply_is_summed_up_in_one_line(monkeypatch):
    clock = iter([10.0, 11.04, 11.19, 11.19, 12.0])
    monkeypatch.setattr(speech.time, "monotonic", lambda: next(clock))
    timing = TurnTiming("Turn 3")
    timing.mark("answer")
    timing.mark("decided")
    assert timing.summary() is None  # not heard yet
    timing.mark("heard")
    timing.mark("heard")  # only the first time counts
    assert timing.summary() == "Turn 3: heard after 1.19s (answer began 1.04s, lookup decided 1.19s)"


def test_greeting_request_quotes_the_greeting():
    assert greeting_request("Hello!") == (
        "[The call has just connected. Greet the caller by saying exactly this "
        'and nothing else: "Hello!"]'
    )


# What the model is told about the conversation so far


async def test_earlier_turns_go_along_as_text():
    context = LLMContext(
        messages=[
            {"role": "assistant", "content": "Hi, this is Oscar."},
            {"role": "user", "content": "Are you open today?"},
            {"role": "assistant", "content": "Yes, until six."},
            {"role": "user", "content": "   "},  # nothing was heard
            {"role": "system", "content": "A note that is not part of the talk."},
            {"role": "user", "content": [{"type": "text", "text": "not plain text"}]},
        ]
    )
    call = await run_voice([*caller_turn(), SleepFrame(0.5)], context=context)
    system, *history, turn = call.chat[0]["messages"]
    assert system == {"role": "system", "content": INSTRUCTIONS}
    assert history == [
        {"role": "assistant", "content": "Hi, this is Oscar."},
        {"role": "user", "content": "Are you open today?"},
        {"role": "assistant", "content": "Yes, until six."},
    ]
    assert turn["role"] == "user" and clip(turn)[1] > 1.0


async def test_a_reminder_is_the_last_thing_read_before_the_turn_to_answer():
    # How Oscar should sound (its accent) is repeated here on every request:
    # after the conversation so far, before the caller's turn.
    context = LLMContext(
        messages=[
            {"role": "assistant", "content": "Hi, this is Oscar."},
            {"role": "user", "content": "Are you open today?"},
            {"role": "assistant", "content": "Yes, until six."},
        ]
    )
    call = await run_voice(
        [*caller_turn(), SleepFrame(0.5)], context=context, reminder="(Keep your accent.)"
    )
    system, *history, reminder, turn = call.chat[0]["messages"]
    assert system == {"role": "system", "content": INSTRUCTIONS}
    assert [message["role"] for message in history] == ["assistant", "user", "assistant"]
    assert reminder == {"role": "system", "content": "(Keep your accent.)"}
    assert turn["role"] == "user" and clip(turn)[1] > 1.0


async def test_a_reminder_comes_before_the_greeting_too():
    call = await run_voice(
        [GreetingFrame("Hi, this is Oscar."), SleepFrame(0.5)],
        chat=spoken("Hi, this is Oscar."),
        reminder="(Keep your accent.)",
    )
    assert call.chat[0]["messages"] == [
        {"role": "system", "content": INSTRUCTIONS},
        {"role": "system", "content": "(Keep your accent.)"},
        {"role": "user", "content": greeting_request("Hi, this is Oscar.")},
    ]
    assert call.text == "Hi, this is Oscar."


# Looking things up on the web

SEARCH = SearchRequest(query="today's weather in Kanpur India", question="What's the weather in Kanpur?")
FOUND = "It is sunny in Kanpur."
LINES = "Your name is Oscar."  # what the model is told when handed a line to say


def seconds_said(*lines: str) -> float:
    return sum(spoken_seconds(line) for line in lines)


async def test_a_turn_that_needs_no_search_is_answered_as_usual():
    context = LLMContext(messages=[{"role": "assistant", "content": "Hi, this is Oscar."}])
    lookup = Lookup(search=None)
    call = await run_voice(
        [*caller_turn(), SleepFrame(0.6)], chat=saying_lines(), lookup=lookup, context=context
    )
    assert call.errors == []
    assert call.text == REPLY and samples_in(call.audio) == REPLY_SAMPLES
    assert (call.searches, call.searching, lookup.asked) == ([], [], [])

    # The decision was asked for once, with the talk so far as text (and without
    # the speech model's own instructions) and the caller's turn as audio.
    ((earlier, turn),) = lookup.decided
    assert earlier == [{"role": "assistant", "content": "Hi, this is Oscar."}]
    assert clip(turn)[1] == pytest.approx(1.4, abs=0.05)
    # The request that produces Oscar's voice is the same as without web search.
    _, reply_request = call.chat
    assert set(reply_request) == {"model", "stream", "modalities", "audio", "messages", "usage"}
    assert reply_request["messages"][-1] == turn


async def test_a_turn_that_needs_a_search_is_answered_from_the_web():
    context = LLMContext(messages=[{"role": "assistant", "content": "Hi, this is Oscar."}])
    lookup = Lookup(SEARCH, answer=FOUND)
    call = await run_voice(
        [*caller_turn(), SleepFrame(0.8)],
        chat=saying_lines(),
        lookup=lookup,
        context=context,
        reminder="(Keep your accent.)",
        line_instructions=LINES,
    )
    assert call.errors == []
    # One reply: the "one moment" line, then what was found. The model's own
    # reply to the turn was dropped, never played.
    assert call.text == f"{HOLD_LINE} {FOUND}"
    assert REPLY not in call.text
    assert samples_in(call.audio) == pytest.approx(seconds_said(HOLD_LINE, FOUND) * 24000, abs=4)
    assert len(call.of(LLMFullResponseStartFrame)) == len(call.of(LLMFullResponseEndFrame)) == 1
    assert len(call.of(TTSStartedFrame)) == len(call.of(TTSStoppedFrame)) == 2  # two stretches of speech

    assert lookup.asked == [(SEARCH.question, SEARCH.query)]
    assert call.searches == [(SEARCH.query, "")]
    assert call.pages == [PAGES]  # what it found is handed on, for the notes after the call
    assert call.searching == [True, False]  # the microphone gate is told

    recording, _, answer = call.chat
    sound = {"role": "system", "content": "(Keep your accent.)"}
    # The "one moment" line was recorded when the call began, to play at once.
    assert recording["messages"] == [
        {"role": "system", "content": LINES},
        sound,
        {"role": "user", "content": say_request(HOLD_LINE)},
    ]
    # The model was not given the search results: only the answer, to say word
    # for word, after the turn so far as text.
    assert answer["messages"] == [
        {"role": "system", "content": LINES},
        {"role": "assistant", "content": "Hi, this is Oscar."},
        {"role": "user", "content": SEARCH.question},
        {"role": "assistant", "content": HOLD_LINE},
        sound,
        {"role": "user", "content": say_request(FOUND)},
    ]


async def test_nothing_of_the_reply_plays_before_the_search_decision_is_known():
    # The model's reply is ready at once; the decision takes 0.4 seconds.
    lookup = Lookup(SEARCH, answer=FOUND, decide_after=0.4)
    call = await run_voice([*caller_turn(), SleepFrame(1.2)], chat=saying_lines(), lookup=lookup)
    assert call.text == f"{HOLD_LINE} {FOUND}"


async def test_a_decision_that_comes_too_late_is_not_waited_for(monkeypatch):
    monkeypatch.setattr(speech, "SEARCH_DECISION_PATIENCE_SECONDS", 0.2)
    lookup = Lookup(SEARCH, decide_after=5.0)
    call = await run_voice([*caller_turn(), SleepFrame(0.8)], chat=saying_lines(), lookup=lookup)
    assert call.errors == []
    assert call.text == REPLY  # the model's own reply, a little late
    assert (lookup.asked, call.searches, call.searching) == ([], [], [])
    assert lookup.gave_up_deciding


async def test_when_the_decision_cannot_be_had_the_turn_is_answered_without_a_search():
    logged: list[tuple[str, str]] = []
    watching = logger.add(lambda m: logged.append((m.record["level"].name, m.record["message"])), level="WARNING")
    try:
        lookup = Lookup(SEARCH, cannot_decide="Could not decide: OpenRouter returned an error (500).")
        call = await run_voice([*caller_turn(), SleepFrame(0.6)], chat=saying_lines(), lookup=lookup)
    finally:
        logger.remove(watching)
    assert call.text == REPLY
    assert (call.errors, call.searches, lookup.asked) == ([], [], [])
    # Said once in the log, as what it is, not as a crash.
    assert [level for level, _ in logged] == ["WARNING"]
    assert logged[0][1].endswith("Could not decide: OpenRouter returned an error (500).")


async def test_a_search_that_fails_is_apologised_for_and_reported():
    lookup = Lookup(SEARCH, failure="Firecrawl rejected the API key (check FIRECRAWL_API_KEY).")
    call = await run_voice([*caller_turn(), SleepFrame(0.8)], chat=saying_lines(), lookup=lookup)
    assert call.text == f"{HOLD_LINE} {SEARCH_FAILED_LINE}"
    assert call.searches == [(SEARCH.query, "Firecrawl rejected the API key (check FIRECRAWL_API_KEY).")]
    assert call.pages == [[]]
    assert call.errors == []  # the call itself is fine
    assert call.searching == [True, False]


async def test_a_lookup_that_breaks_still_gets_the_caller_an_answer():
    class Broken(Lookup):
        async def answer(self, *, question, query, **more):
            raise KeyError("data")

    call = await run_voice([*caller_turn(), SleepFrame(0.8)], chat=saying_lines(), lookup=Broken(SEARCH))
    assert call.text == f"{HOLD_LINE} {SEARCH_FAILED_LINE}"
    ((query, problem),) = call.searches
    assert query == SEARCH.query and problem == "The lookup broke unexpectedly (KeyError('data'))."


async def test_a_search_is_made_in_the_light_of_the_call_so_far():
    # "Who owns it?" only means something with what was said before.
    said = [
        {"role": "assistant", "content": "Hi, this is Oscar."},
        {"role": "user", "content": "Find the listing at 7747 Via Grande."},
        {"role": "assistant", "content": f"{HOLD_LINE} It is listed at 565,000 dollars."},
    ]
    lookup = Lookup(SEARCH)
    await run_voice(
        [*caller_turn(), SleepFrame(0.8)], chat=saying_lines(), lookup=lookup, context=LLMContext(messages=said)
    )
    assert lookup.earlier == [said]  # as text, without the speech model's own instructions


async def test_a_second_search_in_the_same_turn_is_bridged_with_a_line():
    # The first results named what to search for, not the answer.
    lookup = Lookup(SEARCH, answer=FOUND, further="Crown Sydney owner", answer_after=0.05)
    call = await run_voice(
        [*caller_turn(), SleepFrame(1.2)],
        chat=saying_lines(),
        lookup=lookup,
        reminder="(Keep your accent.)",
        line_instructions=LINES,
    )
    assert call.errors == []
    # One reply in three stretches: hold on, hold on a little longer, the answer.
    assert call.text == f"{HOLD_LINE} {STILL_LOOKING_LINE} {FOUND}"
    assert len(call.of(LLMFullResponseStartFrame)) == len(call.of(LLMFullResponseEndFrame)) == 1
    assert len(call.of(TTSStartedFrame)) == len(call.of(TTSStoppedFrame)) == 3
    # Both searches are noted, in the order they were made.
    assert call.searches == [(SEARCH.query, ""), ("Crown Sydney owner", "")]
    assert call.pages == [PAGES, []]
    assert call.searching == [True, False]  # the caller is not listened to until it is all said

    # After the recording of the "one moment" line and the model's own reply, which was dropped.
    _, _, still, answer = call.chat
    sound = {"role": "system", "content": "(Keep your accent.)"}
    assert still["messages"] == [
        {"role": "system", "content": LINES},
        {"role": "user", "content": SEARCH.question},
        sound,
        {"role": "user", "content": say_request(STILL_LOOKING_LINE)},
    ]
    # The answer is asked for after everything the caller has been told meanwhile.
    assert answer["messages"] == [
        {"role": "system", "content": LINES},
        {"role": "user", "content": SEARCH.question},
        {"role": "assistant", "content": f"{HOLD_LINE} {STILL_LOOKING_LINE}"},
        sound,
        {"role": "user", "content": say_request(FOUND)},
    ]


async def test_the_second_line_waits_for_the_first_to_be_said():
    # The "one moment" line had not been recorded yet, so it is asked for when
    # needed and takes a moment. The first search was quicker than that.
    order: list[str] = []

    def chat(request):
        body = json.loads(request.content)
        line = requested_line(body)
        in_the_turn = any(m["content"] == SEARCH.question for m in body["messages"])
        if line == STILL_LOOKING_LINE:
            order.append("still looking: asked for")
        if line != HOLD_LINE or not in_the_turn:
            return saying_lines()(request)

        async def said_slowly():
            order.append("one moment: asked for")
            await asyncio.sleep(0.3)
            for chunk in answer_stream(HOLD_LINE):
                if "[DONE]" in chunk:  # nothing is read after this
                    order.append("one moment: said")
                yield chunk.encode()

        return httpx.Response(200, content=said_slowly(), headers={"content-type": "text/event-stream"})

    class NotRecorded(OpenRouterVoiceService):
        async def _record_hold_line(self):
            pass

    lookup = Lookup(SEARCH, answer=FOUND, further="Crown Sydney owner")
    call = await run_voice(
        [*caller_turn(), SleepFrame(1.5)], chat=chat, lookup=lookup, service=NotRecorded
    )
    assert call.errors == []
    assert call.text == f"{HOLD_LINE} {STILL_LOOKING_LINE} {FOUND}"
    # One line at a time: two requests at once would speak over each other.
    assert order == ["one moment: asked for", "one moment: said", "still looking: asked for"]


async def test_one_search_needs_no_second_line():
    call = await run_voice(
        [*caller_turn(), SleepFrame(0.8)], chat=saying_lines(), lookup=Lookup(SEARCH, answer=FOUND)
    )
    assert STILL_LOOKING_LINE not in call.text
    assert call.searches == [(SEARCH.query, "")]


async def test_a_reading_that_fails_after_the_search_worked_reports_both():
    class Unreadable(Lookup):
        async def answer(self, *, question, query, searched=None, **more):
            await searched(query, PAGES)
            raise LookupFailed("The search worked, but its results could not be read.")

    call = await run_voice([*caller_turn(), SleepFrame(0.8)], chat=saying_lines(), lookup=Unreadable(SEARCH))
    assert call.text == f"{HOLD_LINE} {SEARCH_FAILED_LINE}"
    assert call.searches == [
        (SEARCH.query, ""),
        (SEARCH.query, "The search worked, but its results could not be read."),
    ]


async def test_a_model_that_will_not_say_the_hold_line_is_not_recorded():
    # Seen on real calls: told never to say it is looking something up, the
    # model answered the request for this line with a refusal.
    refusal = "I'm sorry, but I can't say that."
    lookup = Lookup(SEARCH, answer=FOUND)
    call = await run_voice(
        [*caller_turn(), SleepFrame(0.8)], chat=saying_lines(hold_line=refusal), lookup=lookup
    )
    asked_for_the_line = [
        body for body in call.chat if body["messages"][-1]["content"] == say_request(HOLD_LINE)
    ]
    # Asked for twice as the call began, and once more when it was needed.
    assert len(asked_for_the_line) == 3
    # The transcript has what was really said, not what was asked for.
    assert call.text == f"{refusal} {FOUND}"
    # Asked for on the spot, the line comes after the caller's question.
    assert asked_for_the_line[-1]["messages"][1:] == [
        {"role": "user", "content": SEARCH.question},
        {"role": "user", "content": say_request(HOLD_LINE)},
    ]


async def test_a_hold_line_that_could_not_be_recorded_is_asked_for_when_needed():
    attempts = []

    def chat(request):
        body = json.loads(request.content)
        if requested_line(body) == HOLD_LINE:
            attempts.append(body)
            if len(attempts) == 1:
                return httpx.Response(503, json={"error": {"message": "overloaded"}})
        return saying_lines()(request)

    call = await run_voice([*caller_turn(), SleepFrame(0.8)], chat=chat, lookup=Lookup(SEARCH, answer=FOUND))
    assert len(attempts) == 2  # once as the call began (it failed), once when needed
    assert call.text == f"{HOLD_LINE} {FOUND}"
    assert call.errors == []  # a failed recording is no failure of the call


async def test_talking_over_a_search_stops_it():
    lookup = Lookup(SEARCH, answer=FOUND, answer_after=5.0)
    call = await run_voice(
        [*caller_turn(), SleepFrame(0.4), InterruptionFrame(), SleepFrame(0.4)],
        chat=saying_lines(),
        lookup=lookup,
    )
    assert lookup.asked == [(SEARCH.question, SEARCH.query)]  # it had begun
    assert call.text == HOLD_LINE  # and never got to an answer
    assert call.searches == []
    assert call.searching == [True, False]  # the gate is opened again
    assert len(call.of(LLMFullResponseEndFrame)) == 1


async def test_the_greeting_is_never_looked_up():
    lookup = Lookup(SEARCH)
    call = await run_voice(
        [GreetingFrame("Hi, this is Oscar."), SleepFrame(0.5)], chat=saying_lines(), lookup=lookup
    )
    assert call.text == "Hi, this is Oscar."
    assert lookup.decided == [] and lookup.closed


async def test_without_web_search_nothing_is_recorded_or_decided():
    call = await run_voice([*caller_turn(), SleepFrame(0.5)])
    assert len(call.chat) == 1 and call.text == REPLY


async def test_lines_are_said_under_the_usual_instructions_unless_told_otherwise():
    call = await run_voice([*caller_turn(), SleepFrame(0.8)], chat=saying_lines(), lookup=Lookup(SEARCH))
    recording, _, answer = call.chat
    assert recording["messages"][0] == answer["messages"][0] == {"role": "system", "content": INSTRUCTIONS}


# The owner's fact sheet

FACTS = FactsRequest(question="What time do you close on Saturday?")
KNOWN = "We close at four pm on Saturdays."


def lines_asked_for(call) -> list[str]:
    return [requested_line(body) for body in call.chat if requested_line(body)]


async def test_a_turn_about_the_owner_is_answered_from_the_fact_sheet():
    context = LLMContext(messages=[{"role": "assistant", "content": "Hi, this is Oscar."}])
    lookup = Lookup(FACTS, answer=KNOWN, has_facts=True)
    call = await run_voice(
        [*caller_turn(), SleepFrame(0.8)],
        chat=saying_lines(),
        lookup=lookup,
        context=context,
        reminder="(Keep your accent.)",
        line_instructions=LINES,
    )
    assert call.errors == []
    # One reply: the "one moment" line, then the answer. The model's own reply
    # (it has not seen the fact sheet) was dropped, never played.
    assert call.text == f"{HOLD_LINE} {KNOWN}"
    assert REPLY not in call.text
    assert samples_in(call.audio) == pytest.approx(seconds_said(HOLD_LINE, KNOWN) * 24000, abs=4)
    assert len(call.of(LLMFullResponseStartFrame)) == len(call.of(LLMFullResponseEndFrame)) == 1

    # The answer was written from the question and the talk so far, as text.
    assert lookup.facts_asked == [(FACTS.question, [{"role": "assistant", "content": "Hi, this is Oscar."}])]
    assert (lookup.asked, call.searches, call.facts_failures) == ([], [], [])  # no web search
    assert call.searching == [True, False]  # the microphone gate is told

    # The model was not given the fact sheet: only the answer, to say word for
    # word, after the turn so far as text.
    sound = {"role": "system", "content": "(Keep your accent.)"}
    assert call.chat[-1]["messages"] == [
        {"role": "system", "content": LINES},
        {"role": "assistant", "content": "Hi, this is Oscar."},
        {"role": "user", "content": FACTS.question},
        {"role": "assistant", "content": HOLD_LINE},
        sound,
        {"role": "user", "content": say_request(KNOWN)},
    ]
    # The "one moment" line was recorded once, as the call began, and played from that.
    assert lines_asked_for(call) == [HOLD_LINE, KNOWN]


async def test_a_fact_sheet_that_cannot_be_read_is_apologised_for_and_reported():
    problem = "The fact sheet could not be read: OpenRouter is out of credits."
    lookup = Lookup(FACTS, failure=problem, has_facts=True)
    call = await run_voice([*caller_turn(), SleepFrame(0.8)], chat=saying_lines(), lookup=lookup)
    assert call.text == f"{HOLD_LINE} {FACTS_FAILED_LINE}"
    assert call.facts_failures == [problem]
    assert (call.errors, call.searches) == ([], [])  # the call itself is fine, and it was no search
    assert call.searching == [True, False]


async def test_a_fact_lookup_that_breaks_still_gets_the_caller_an_answer():
    class Broken(Lookup):
        async def answer_from_facts(self, question, earlier):
            raise KeyError("choices")

    call = await run_voice(
        [*caller_turn(), SleepFrame(0.8)], chat=saying_lines(), lookup=Broken(FACTS, has_facts=True)
    )
    assert call.text == f"{HOLD_LINE} {FACTS_FAILED_LINE}"
    (problem,) = call.facts_failures
    assert problem.startswith("The lookup broke unexpectedly")


async def test_with_a_fact_sheet_the_decision_is_waited_for_longer(monkeypatch):
    # Played without the decision, the model's own reply can be "I don't have
    # that information" about something the fact sheet says.
    monkeypatch.setattr(speech, "SEARCH_DECISION_PATIENCE_SECONDS", 0.1)
    monkeypatch.setattr(speech, "FACTS_DECISION_PATIENCE_SECONDS", 2.0)
    with_sheet = Lookup(FACTS, answer=KNOWN, decide_after=0.5, has_facts=True)
    call = await run_voice([*caller_turn(), SleepFrame(1.4)], chat=saying_lines(), lookup=with_sheet)
    assert call.text == f"{HOLD_LINE} {KNOWN}"
    # Without one, a decision as late as that is not waited for.
    without = Lookup(SEARCH, decide_after=0.5)
    call = await run_voice([*caller_turn(), SleepFrame(1.4)], chat=saying_lines(), lookup=without)
    assert call.text == REPLY and without.asked == []


async def test_a_lookup_decided_first_does_not_wait_for_the_models_own_reply():
    # On a real call the model's own reply stalled for seven seconds after the
    # decision was known, and nothing was said until it came.
    usual = saying_lines()

    async def chat(request):
        if requested_line(json.loads(request.content)) is None:  # the model's own reply to the turn
            await asyncio.sleep(3.0)
        return usual(request)

    lookup = Lookup(FACTS, answer=KNOWN, has_facts=True)
    call = await run_voice([*caller_turn(), SleepFrame(1.0)], chat=chat, lookup=lookup)
    assert call.text == f"{HOLD_LINE} {KNOWN}"
    assert call.errors == []

    searching = Lookup(SEARCH, answer=FOUND)
    call = await run_voice([*caller_turn(), SleepFrame(1.0)], chat=chat, lookup=searching)
    assert call.text == f"{HOLD_LINE} {FOUND}"


async def test_a_slow_reply_that_needs_no_lookup_is_still_played():
    usual = saying_lines()

    async def chat(request):
        if requested_line(json.loads(request.content)) is None:
            await asyncio.sleep(0.4)
        return usual(request)

    lookup = Lookup(None, has_facts=True)
    call = await run_voice([*caller_turn(), SleepFrame(1.2)], chat=chat, lookup=lookup)
    assert call.text == REPLY and samples_in(call.audio) == REPLY_SAMPLES
    assert (lookup.facts_asked, lookup.asked) == ([], [])


async def test_a_decision_that_comes_once_the_reply_is_playing_leaves_it_alone(monkeypatch):
    monkeypatch.setattr(speech, "SEARCH_DECISION_PATIENCE_SECONDS", 0.1)
    lookup = Lookup(SEARCH, answer=FOUND, decide_after=0.5)
    arriving = slowly(gap=0.06)  # the reply is still arriving when the decision comes

    def chat(request):
        said_as_asked = requested_line(json.loads(request.content)) is not None
        return (saying_lines() if said_as_asked else arriving)(request)

    call = await run_voice([*caller_turn(), SleepFrame(2.5)], chat=chat, lookup=lookup)
    assert call.text == REPLY and samples_in(call.audio) == REPLY_SAMPLES
    assert (lookup.asked, call.searching) == ([], [])


async def test_talking_over_an_answer_from_the_fact_sheet_stops_it():
    lookup = Lookup(FACTS, answer=KNOWN, answer_after=5.0, has_facts=True)
    call = await run_voice(
        [*caller_turn(), SleepFrame(0.4), InterruptionFrame(), SleepFrame(0.4)],
        chat=saying_lines(),
        lookup=lookup,
    )
    assert len(lookup.facts_asked) == 1  # it had begun
    assert call.text == HOLD_LINE  # and never got to an answer
    assert call.searching == [True, False]  # the gate is opened again
    assert len(call.of(LLMFullResponseEndFrame)) == 1


@pytest.mark.parametrize(
    ("said", "same"),
    [
        ("One moment, let me look that up.", True),
        ("one moment — let me look that up", True),
        ("One moment, let me look that up for you.", False),
        ("I'm sorry, but I can't say that.", False),
        ("", False),
    ],
)
def test_checking_that_a_line_was_said_as_asked(said, same):
    assert same_words(said, HOLD_LINE) is same


def test_say_request_quotes_the_line():
    assert say_request("It is sunny.") == (
        '[Say exactly this to the caller, and nothing else: "It is sunny."]'
    )


# The caller carries on talking, or talks over Oscar


async def test_a_caller_who_carries_on_talking_is_heard_in_full():
    # "Hey ..." (Oscar starts working on an answer) "... are you there?"
    # The second request holds both parts, and the first answer is never spoken.
    context = LLMContext(messages=[{"role": "assistant", "content": "Hi, this is Oscar."}])
    frames = [
        *caller_turn(0.5),
        SleepFrame(0.2),
        InterruptionFrame(),  # the caller starts speaking again
        *caller_turn(1.0),
        SleepFrame(0.6),
    ]
    answered = 0

    def chat(request):
        nonlocal answered
        answered += 1
        return slowly(first_after=5.0)(request) if answered == 1 else spoken()(request)

    call = await run_voice(frames, chat=chat, context=context)
    assert call.errors == []
    first, second = call.chat
    assert voice_sent(first) == pytest.approx(0.5, abs=0.03)
    assert voice_sent(second) == pytest.approx(1.5, abs=0.05)
    # Both were asked with the same history; the second simply heard more.
    assert first["messages"][:-1] == second["messages"][:-1]
    # Only the second answer was spoken.
    assert samples_in(call.audio) == REPLY_SAMPLES
    assert len(call.of(LLMFullResponseStartFrame)) == 1


async def test_what_was_said_meanwhile_is_not_also_sent_as_text():
    # While the caller carried on, their first words were written into the
    # history. The model already gets those words as audio, so the history it
    # is sent stops where that audio begins.
    context = LLMContext(messages=[{"role": "assistant", "content": "Hi, this is Oscar."}])

    def chat(request):
        if len(context.get_messages()) == 1:
            context.add_message({"role": "user", "content": "Hey."})
            return slowly(first_after=5.0)(request)
        return spoken()(request)

    frames = [
        *caller_turn(0.5),
        SleepFrame(0.2),
        InterruptionFrame(),
        *caller_turn(1.0),
        SleepFrame(0.6),
    ]
    call = await run_voice(frames, chat=chat, context=context)
    _, second = call.chat
    assert [m["content"] for m in second["messages"][:-1]] == [INSTRUCTIONS, "Hi, this is Oscar."]
    assert voice_sent(second) == pytest.approx(1.5, abs=0.05)


async def test_once_oscar_has_answered_a_turn_it_is_not_sent_again():
    frames = [
        *caller_turn(0.5),
        SleepFrame(0.4),
        BotStartedSpeakingFrame(),
        BotStoppedSpeakingFrame(),  # Oscar said its answer to the end
        *caller_turn(1.0),
        SleepFrame(0.5),
    ]
    call = await run_voice(frames)
    _, second = call.chat
    assert voice_sent(second) == pytest.approx(1.0, abs=0.03)


@pytest.mark.parametrize(
    ("listened_for", "kept"),
    [
        (0.05, True),  # cut in at once: the caller was still mid-thought
        (0.5, False),  # listened first: the caller is reacting to the answer
    ],
)
async def test_cutting_in_early_keeps_the_callers_earlier_words(monkeypatch, listened_for, kept):
    monkeypatch.setattr(speech, "ANSWERED_AFTER_SECONDS", 0.3)
    frames = [
        *caller_turn(0.5),
        SleepFrame(0.3),
        BotStartedSpeakingFrame(),
        SleepFrame(listened_for),
        InterruptionFrame(),
        BotStoppedSpeakingFrame(),  # the audio output confirming it went quiet
        *caller_turn(1.0),
        SleepFrame(0.5),
    ]
    call = await run_voice(frames)
    assert voice_sent(call.chat[-1]) == pytest.approx(1.5 if kept else 1.0, abs=0.05)


async def test_talking_over_oscar_stops_the_reply():
    frames = [*caller_turn(), SleepFrame(0.45), InterruptionFrame(), SleepFrame(0.6)]
    call = await run_voice(frames, chat=slowly(gap=0.1))
    assert call.errors == []
    # About four of the reply's nineteen pieces had arrived; nothing came after.
    assert 0 < samples_in(call.audio) < REPLY_SAMPLES / 2
    assert len(call.of(LLMFullResponseStartFrame)) == 1
    assert len(call.of(LLMFullResponseEndFrame)) <= 1
    assert len(call.of(TTSStoppedFrame)) <= 1
    assert len(call.text) < len(REPLY)  # the words that never played are not recorded


async def test_very_long_talking_is_cut_to_the_newest_part(monkeypatch):
    monkeypatch.setattr(speech, "MAX_TURN_SECONDS", 2)
    call = await run_voice([*caller_turn(3.0), SleepFrame(0.5)])
    rate, seconds, samples = clip(call.chat[0]["messages"][-1])
    assert seconds == pytest.approx(2.0, abs=0.01)
    assert seconds_of_voice(samples, rate) == pytest.approx(2.0, abs=0.03)


# When OpenRouter fails


# A refusal no second try can get past is asked for once. The provider's own
# trouble (5xx) is asked for twice, and explained once.
@pytest.mark.parametrize(
    ("status", "body", "expected", "asked"),
    [
        (401, {"json": {"error": {"message": "No auth credentials found"}}},
         "Oscar could not answer: OpenRouter rejected the API key (check OPENROUTER_API_KEY). "
         "No auth credentials found", 1),
        (402, {"json": {"error": {"message": "Insufficient credits"}}},
         "Oscar could not answer: The OpenRouter account is out of credits. Insufficient credits", 1),
        (404, {"json": {"error": {"message": "No such model"}}},
         "Oscar could not answer: OpenRouter no longer offers the speech model Oscar uses. "
         "No such model", 1),
        (429, {"json": {"error": {"message": "Slow down"}}},
         "Oscar could not answer: OpenRouter is rate limiting requests. Try again in a minute. "
         "Slow down", 1),
        (499, {"text": "client closed request"}, "Oscar could not answer: OpenRouter returned an error (499).", 1),
        (500, {"text": "upstream exploded"}, "Oscar could not answer: OpenRouter returned an error (500).", 2),
        (503, {"json": {"error": {"message": "Provider returned error"}}},
         "Oscar could not answer: OpenRouter returned an error (503). Provider returned error", 2),
    ],
)
async def test_a_refused_request_is_explained_and_plays_nothing(status, body, expected, asked):
    call = await run_voice(
        [*caller_turn(), SleepFrame(0.5)], chat=lambda request: httpx.Response(status, **body)
    )
    assert call.audio == []
    assert call.errors == [expected]
    assert call.of(LLMFullResponseStartFrame) == []
    assert len(call.chat) == asked and ANSWER_ATTEMPTS == 2


async def test_a_network_failure_is_explained():
    def chat(request):
        raise httpx.ConnectError("no route to host")

    call = await run_voice([*caller_turn(), SleepFrame(0.5)], chat=chat)
    assert call.audio == []
    assert call.errors == ["Oscar could not answer: OpenRouter was unreachable (no route to host)."]
    assert len(call.chat) == 2  # asked for once more, and no more than that


# An answer that fails before any of it was said is asked for once more

PROVIDER_ERROR = 'data: {"error": {"message": "Provider returned error", "code": 502}}\n\n'


def failing_first(failure, then=None):
    """A chat endpoint whose first answer fails and whose later ones are spoken.

    `failure` is a function of the request giving the response (or raising).
    """
    then = then or spoken()
    asked = []

    def respond(request):
        asked.append(request)
        return failure(request) if len(asked) == 1 else then(request)

    return respond


def cut_off(*chunks: str, error: Exception):
    """An answer whose connection breaks after `chunks` have arrived."""

    async def pieces():
        for chunk in chunks:
            yield chunk.encode()
        raise error

    return httpx.Response(200, content=pieces(), headers={"content-type": "text/event-stream"})


def unreachable(request):
    raise httpx.ConnectError("no route to host")


@pytest.mark.parametrize(
    "failure",
    [
        # What the real service sent on 2026-11-13, when a search's answer was never said.
        lambda r: httpx.Response(503, json={"error": {"message": "Provider returned error"}}),
        lambda r: httpx.Response(500, text="upstream exploded"),
        unreachable,
        # The provider gives up as the answer starts.
        lambda r: httpx.Response(200, content=PROVIDER_ERROR.encode()),
        # Words of the answer came, none of its speech.
        lambda r: httpx.Response(200, content=(sse({"audio": {"transcript": "We open"}}) + PROVIDER_ERROR).encode()),
        lambda r: cut_off(sse({"audio": {"transcript": "We open"}}), error=httpx.ReadError("connection reset")),
    ],
)
async def test_an_answer_that_fails_before_any_of_it_was_said_is_asked_for_once_more(failure):
    logged: list[str] = []
    watching = logger.add(lambda m: logged.append(m.record["message"]), level="WARNING")
    try:
        call = await run_voice([*caller_turn(), SleepFrame(0.5)], chat=failing_first(failure))
    finally:
        logger.remove(watching)
    # The caller hears the answer, once, and the call shows no problem.
    assert call.errors == []
    assert call.text == REPLY
    assert samples_in(call.audio) == REPLY_SAMPLES
    assert len(call.of(LLMFullResponseStartFrame)) == len(call.of(LLMFullResponseEndFrame)) == 1
    assert len(call.of(TTSStartedFrame)) == len(call.of(TTSStoppedFrame)) == 1
    # The same request, made twice.
    first, second = call.chat
    assert first == second
    (warning,) = logged
    assert warning.endswith("Asking once more.")


async def test_an_answer_that_fails_twice_is_explained_once():
    call = await run_voice(
        [*caller_turn(), SleepFrame(0.5)],
        chat=lambda r: httpx.Response(200, content=(sse({"audio": {"transcript": "We open"}}) + PROVIDER_ERROR).encode()),
    )
    assert len(call.chat) == 2
    assert call.errors == ["Oscar could not answer: Provider returned error"]
    # Nothing was said, so nothing is written down as said.
    assert (call.audio, call.text) == ([], "")
    # The reply that was begun is closed off, so the call carries on.
    assert len(call.of(LLMFullResponseStartFrame)) == len(call.of(LLMFullResponseEndFrame)) == 1


async def test_a_searchs_answer_that_is_refused_is_asked_for_once_more():
    # The real failure: the line carrying what a search found got a 503.
    refused = lambda r: httpx.Response(503, json={"error": {"message": "Provider returned error"}})
    lines = saying_lines()
    asked_to_say_it = []

    def chat(request):
        if requested_line(json.loads(request.content)) == FOUND:
            asked_to_say_it.append(json.loads(request.content))
            if len(asked_to_say_it) == 1:
                return refused(request)
        return lines(request)

    call = await run_voice([*caller_turn(), SleepFrame(1.0)], chat=chat, lookup=Lookup(SEARCH, answer=FOUND))
    assert call.text == f"{HOLD_LINE} {FOUND}"
    assert call.errors == []
    first, second = asked_to_say_it
    assert first == second
    assert call.searches == [(SEARCH.query, "")] and call.searching == [True, False]


async def test_a_greeting_that_is_refused_is_asked_for_once_more():
    call = await run_voice(
        [GreetingFrame("Hi, this is Oscar."), SleepFrame(0.5)],
        chat=failing_first(lambda r: httpx.Response(502, text="bad gateway"), saying_lines()),
    )
    assert (call.text, call.errors, len(call.chat)) == ("Hi, this is Oscar.", [], 2)


async def test_a_reply_asked_for_again_still_gives_way_to_a_lookup():
    # The model's own reply is refused while the decision to search is still on its way.
    refused = lambda r: httpx.Response(503, json={"error": {"message": "Provider returned error"}})
    lines = saying_lines()
    replies = []

    def chat(request):
        body = json.loads(request.content)
        if requested_line(body) is None:
            replies.append(body)
            if len(replies) == 1:
                return refused(request)
        return lines(request)

    lookup = Lookup(SEARCH, answer=FOUND, decide_after=0.3)
    call = await run_voice([*caller_turn(), SleepFrame(1.2)], chat=chat, lookup=lookup)
    assert call.text == f"{HOLD_LINE} {FOUND}" and REPLY not in call.text
    assert call.errors == []
    assert len(replies) == 2  # asked for again, then dropped for what was looked up


async def test_a_reply_asked_for_again_is_played_when_no_lookup_is_needed():
    lookup = Lookup(None, decide_after=0.3)
    refused = lambda r: httpx.Response(503, json={"error": {"message": "Provider returned error"}})
    lines = saying_lines()
    replies = []

    def chat(request):
        body = json.loads(request.content)
        if requested_line(body) is None:
            replies.append(body)
            if len(replies) == 1:
                return refused(request)
        return lines(request)

    call = await run_voice([*caller_turn(), SleepFrame(1.0)], chat=chat, lookup=lookup)
    assert (call.text, call.errors, len(replies)) == (REPLY, [], 2)
    assert lookup.asked == []


async def test_an_answer_cut_off_after_it_began_is_not_asked_for_again():
    half = tone(0.5)
    cut = cut_off(
        sse({"audio": {"transcript": "We open"}}),
        sse({"audio": {"data": base64.b64encode(half).decode()}}),
        error=httpx.ReadError("connection reset"),
    )
    call = await run_voice([*caller_turn(), SleepFrame(0.5)], chat=lambda request: cut)
    # The caller heard part of it: saying it over again would not help them.
    assert len(call.chat) == 1
    assert call.errors == ["Oscar could not answer: OpenRouter was unreachable (connection reset)."]
    assert b"".join(frame.audio for frame in call.audio) == half
    assert call.text == "We open"
    assert len(call.of(TTSStoppedFrame)) == 1 and len(call.of(LLMFullResponseEndFrame)) == 1


async def test_an_error_in_the_middle_of_an_answer_is_explained_and_closes_the_reply():
    half = tone(0.5)
    body = (
        sse({"audio": {"transcript": "We open"}})
        + sse({"audio": {"data": base64.b64encode(half).decode()}})
        + 'data: {"error": {"message": "Provider returned error", "code": 502}}\n\n'
        + sse({"audio": {"transcript": " never said"}})
    )
    call = await run_voice(
        [*caller_turn(), SleepFrame(0.5)],
        chat=lambda request: httpx.Response(200, content=body.encode()),
    )
    assert call.errors == ["Oscar could not answer: Provider returned error"]
    assert b"".join(frame.audio for frame in call.audio) == half
    assert call.text == "We open"
    # What was said is closed off properly, so the call carries on.
    assert len(call.of(TTSStoppedFrame)) == 1 and len(call.of(LLMFullResponseEndFrame)) == 1
    assert len(call.chat) == 1  # part of it was heard: it is not asked for again


async def test_lines_that_are_not_answers_are_skipped():
    piece = tone(0.2)
    body = (
        ": OPENROUTER PROCESSING\n\n"
        + "data: not json\n\n"
        + 'data: ["unexpected"]\n\n'
        + sse({"role": "assistant", "content": ""})
        + sse({"audio": {"data": "not base64!"}})
        + sse({"audio": {"data": base64.b64encode(piece).decode(), "transcript": "Yes."}})
        + sse({}, finish_reason="stop")
        + "data: [DONE]\n\n"
        + sse({"audio": {"transcript": " after the end"}})
    )
    call = await run_voice(
        [*caller_turn(), SleepFrame(0.5)],
        chat=lambda request: httpx.Response(200, content=body.encode()),
    )
    assert call.errors == []
    assert b"".join(frame.audio for frame in call.audio) == piece
    assert call.text == "Yes."


# Not hearing its own voice


async def gate(frames) -> list[bool]:
    """Whether each piece of caller audio came through the gate with its sound."""
    down, _ = await run_test(EchoGate(), frames_to_send=frames)
    return [any(frame.audio) for frame in down if isinstance(frame, InputAudioRawFrame)]


def sound() -> InputAudioRawFrame:
    return InputAudioRawFrame(audio=tone(0.02, 16000), sample_rate=16000, num_channels=1)


async def test_the_caller_is_not_heard_while_oscar_speaks_nor_just_after():
    heard = await gate(
        [
            sound(),  # before Oscar speaks
            BotStartedSpeakingFrame(),
            sound(),  # Oscar's own voice, coming back through the microphone
            SleepFrame(0.4),
            sound(),
            BotStoppedSpeakingFrame(),
            sound(),  # the last of it, played a little late by the caller's device
            SleepFrame(0.2),
            sound(),
            SleepFrame(0.2),
            sound(),  # the caller, answering
        ]
    )
    assert heard == [True, False, False, False, False, True]


async def test_silenced_audio_keeps_its_length_and_format():
    frames = [BotStartedSpeakingFrame(), sound()]
    down, _ = await run_test(EchoGate(), frames_to_send=frames)
    (quiet,) = [frame for frame in down if isinstance(frame, InputAudioRawFrame)]
    assert quiet.audio == bytes(640)  # 20 ms of silence
    assert (quiet.sample_rate, quiet.num_channels) == (16000, 1)


async def test_a_reply_cut_off_at_once_leaves_no_deaf_moment():
    # Oscar had barely begun, so there is nothing of its voice left to come back.
    heard = await gate(
        [BotStartedSpeakingFrame(), BotStoppedSpeakingFrame(), SleepFrame(0.05), sound()]
    )
    assert heard == [True]


# The small pieces


async def test_the_caller_is_not_heard_while_oscar_searches_the_web():
    # The line is silent then, and "hello?" would count as a new turn and
    # cancel the search.
    voice = tone(0.1, 16000)
    down, _ = await run_test(
        EchoGate(),
        frames_to_send=[
            *audio_frames(voice, 16000),
            SearchingFrame(active=True),
            *audio_frames(voice, 16000),
            SearchingFrame(active=False),
            *audio_frames(voice, 16000),
        ],
    )
    heard = [any(frame.audio) for frame in down if isinstance(frame, InputAudioRawFrame)]
    assert heard == [True] * 5 + [False] * 5 + [True] * 5


@pytest.mark.parametrize("rate", [8000, 16000])
def test_wav_file(rate):
    pcm = tone(0.25, rate)
    with wave.open(io.BytesIO(wav_file(pcm, rate))) as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (rate, 1, 2)
        assert wav.readframes(wav.getnframes()) == pcm


# Following a script


def note_before(body: dict) -> dict | None:
    """The note the model reads last before the turn it answers, if the request has one."""
    last = body["messages"][-2]
    return last if last["role"] == "system" and last["content"] != INSTRUCTIONS else None


async def test_before_a_callers_turn_the_model_is_told_its_step_after_how_it_sounds():
    call = await run_voice(
        [*caller_turn(), SleepFrame(0.5)],
        script=Script(),
        reminder="(You are speaking in your Indian accent.)",
        script_reminder="Keep your Indian accent.",
    )
    system, note, turn = call.chat[0]["messages"]
    assert system == {"role": "system", "content": INSTRUCTIONS}  # the script is not in here
    # One note: how Oscar sounds, its step, and the accent again as the last words.
    assert note == {
        "role": "system",
        "content": "(You are speaking in your Indian accent.) (You are on step 1. Keep your Indian accent.)",
    }
    assert turn["role"] == "user"


async def test_without_a_script_the_accent_is_not_named_twice():
    call = await run_voice(
        [*caller_turn(), SleepFrame(0.5)],
        reminder="(You are speaking in your Indian accent.)",
        script_reminder="Keep your Indian accent.",
    )
    assert note_before(call.chat[0])["content"] == "(You are speaking in your Indian accent.)"


async def test_a_voice_left_as_it_is_still_gets_its_step():
    call = await run_voice([*caller_turn(), SleepFrame(0.5)], script=Script())
    assert note_before(call.chat[0]) == {"role": "system", "content": "(You are on step 1.)"}


async def test_a_finished_script_leaves_only_the_reminder():
    script = Script()
    script.note = lambda: ""
    call = await run_voice([*caller_turn(), SleepFrame(0.5)], script=script, reminder="(Keep your accent.)")
    assert note_before(call.chat[0]) == {"role": "system", "content": "(Keep your accent.)"}


async def test_the_greeting_is_not_a_step_of_the_script():
    script = Script()
    call = await run_voice(
        [GreetingFrame("Hi, this is Oscar."), SleepFrame(0.5)],
        chat=saying_lines(),
        script=script,
        reminder="(Keep your accent.)",
    )
    assert call.text == "Hi, this is Oscar."
    assert note_before(call.chat[0]) == {"role": "system", "content": "(Keep your accent.)"}
    assert script.talks == []  # and the place is not asked for: the caller has said nothing yet
    assert script.closed


async def test_a_line_oscar_is_handed_to_say_is_not_given_the_script():
    script = Script()
    call = await run_voice(
        [*caller_turn(), SleepFrame(1.0)],
        chat=saying_lines(),
        lookup=Lookup(SEARCH),
        script=script,
        reminder="(Keep your accent.)",
    )
    assert call.text == f"{HOLD_LINE} It is sunny in Kanpur."
    lines = [body for body in call.chat if requested_line(body)]
    assert lines and all(note_before(body)["content"] == "(Keep your accent.)" for body in lines)
    # What Oscar looked up and said still counts as part of the call.
    assert script.talks == [
        [
            {"role": "user", "content": HEARD},
            {"role": "assistant", "content": f"{HOLD_LINE} It is sunny in Kanpur."},
        ]
    ]


async def test_after_each_reply_the_place_in_the_script_is_worked_out_from_the_whole_call():
    script = Script()
    context = LLMContext(
        messages=[
            {"role": "assistant", "content": "Hi, this is Oscar."},
            {"role": "user", "content": "It's Daniel."},
            {"role": "assistant", "content": "Hi Daniel, what do you need?"},
        ]
    )
    call = await run_voice([*caller_turn(), SleepFrame(0.5)], context=context, script=script)
    assert call.errors == [] and call.script_losses == []
    # Everything said so far, as text: the earlier turns, what the caller just said, Oscar's reply.
    assert script.talks == [
        [
            {"role": "assistant", "content": "Hi, this is Oscar."},
            {"role": "user", "content": "It's Daniel."},
            {"role": "assistant", "content": "Hi Daniel, what do you need?"},
            {"role": "user", "content": HEARD},
            {"role": "assistant", "content": REPLY},
        ]
    ]
    assert script.place == 1


async def test_the_place_is_worked_out_without_the_callers_words_when_they_were_not_made_out():
    script = Script()
    call = await run_voice(
        [*caller_turn(), SleepFrame(0.5)], transcription=written(""), script=script
    )
    assert call.heard == []
    assert script.talks == [[{"role": "assistant", "content": REPLY}]]


async def test_the_place_is_worked_out_from_everything_the_caller_said_before_the_answer():
    # The caller paused mid-thought, so two turns are answered as one.
    script = Script()
    parts = iter(["It's Daniel.", "I'd like a haircut."])
    frames = [*caller_turn(0.5), SleepFrame(0.1), *caller_turn(0.5), SleepFrame(1.5)]
    call = await run_voice(
        frames,
        chat=slowly(first_after=0.5, gap=0.01),
        transcription=lambda request: httpx.Response(200, json={"text": next(parts)}),
        script=script,
    )
    assert call.text == REPLY  # one answer
    assert script.talks == [
        [
            {"role": "user", "content": "It's Daniel. I'd like a haircut."},
            {"role": "assistant", "content": REPLY},
        ]
    ]


async def test_words_oscar_has_answered_are_not_counted_into_the_next_turn():
    script = Script()
    parts = iter(["It's Daniel.", "I'd like a haircut."])
    frames = [
        *caller_turn(0.5),
        SleepFrame(0.3),
        BotStartedSpeakingFrame(),
        SleepFrame(0.1),
        BotStoppedSpeakingFrame(),  # Oscar said its reply to the end
        *caller_turn(0.5),
        SleepFrame(0.5),
    ]
    await run_voice(
        frames,
        transcription=lambda request: httpx.Response(200, json={"text": next(parts)}),
        script=script,
    )
    assert [talk[0] for talk in script.talks] == [
        {"role": "user", "content": "It's Daniel."},
        {"role": "user", "content": "I'd like a haircut."},
    ]


async def test_the_next_turn_is_told_the_step_the_script_has_reached():
    script = Script()
    frames = [*caller_turn(), SleepFrame(0.5), *caller_turn(), SleepFrame(0.5)]
    call = await run_voice(frames, script=script)
    first, second = call.chat
    assert note_before(first)["content"] == "(You are on step 1.)"
    assert note_before(second)["content"] == "(You are on step 2.)"
    assert script.place == 2
    # Each time, the reply just given is the last thing in the call so far (and is there once).
    assert [talk[-1] for talk in script.talks] == [{"role": "assistant", "content": REPLY}] * 2


async def test_a_caller_quicker_than_the_keeper_is_made_to_wait_for_the_place(monkeypatch):
    # The place takes 0.4 s to work out, and the caller's next turn ends 0.2 s
    # after Oscar's reply arrived. Told the step it had already done, Oscar
    # would ask the same thing twice.
    script = Script(after=0.4)
    frames = [*caller_turn(), SleepFrame(0.2), *caller_turn(0.3), SleepFrame(1.0)]
    call = await run_voice(frames, script=script)
    first, second = call.chat
    assert note_before(second)["content"] == "(You are on step 2.)"
    assert script.gave_up == 0


async def test_a_place_that_takes_too_long_is_not_waited_for(monkeypatch):
    monkeypatch.setattr(speech, "SCRIPT_PLACE_PATIENCE_SECONDS", 0.1)
    script = Script(after=5.0)
    frames = [*caller_turn(), SleepFrame(0.2), *caller_turn(0.3), SleepFrame(0.5)]
    call = await run_voice(frames, script=script)
    first, second = call.chat
    # The second turn is answered from the last place known.
    assert note_before(second)["content"] == "(You are on step 1.)"
    assert call.text.count(REPLY) == 2
    # The slow answer knew of one exchange fewer, so it is dropped for a new one.
    assert len(script.talks) == 2 and script.gave_up == 2  # the first dropped, the second cut off by the hang-up


async def test_losing_the_place_is_reported_and_oscar_carries_on_from_where_it_was():
    script = Script(lost="The OpenRouter account is out of credits.")
    frames = [*caller_turn(), SleepFrame(0.5), *caller_turn(), SleepFrame(0.5)]
    call = await run_voice(frames, script=script)
    assert call.script_losses == ["The OpenRouter account is out of credits."] * 2
    assert call.errors == []  # the call itself is fine
    assert [note_before(body)["content"] for body in call.chat] == ["(You are on step 1.)"] * 2
    assert call.text.count(REPLY) == 2


async def test_a_reply_cut_off_before_it_started_does_not_move_the_script():
    # The caller spoke again before anything was said: there is no exchange to judge.
    script = Script()
    frames = [
        *caller_turn(0.5),
        SleepFrame(0.1),
        InterruptionFrame(),
        SleepFrame(0.3),
    ]
    call = await run_voice(frames, chat=slowly(first_after=1.0), script=script)
    assert call.text == ""
    assert script.talks == []
