"""Oscar's ears and voice on calls: one speech-to-speech model, through OpenRouter.

The caller's whole turn is sent as audio to a model that answers in audio
(openai/gpt-audio), so each reply takes one round trip:

    POST /chat/completions   the caller's audio in; speech and its text out, streamed

Measured against the real service on 2026-11-11: speech starts about 1.1 seconds
after the request. The earlier design (transcribe, then a text model, then
text-to-speech) took three round trips and about 4 seconds.

The model hears the audio itself, so no transcript is needed to answer. One is
still made, in the background, so the dashboard and later turns have the
caller's words as text. That text is what Oscar remembers of the caller:

    POST /audio/transcriptions   a clip in, text out (deepgram/nova-3, several languages)

When web search is set up, a second request decides, alongside the first,
whether the turn needs looking up (app/search.py). If it does, the model's own
reply is dropped: the caller hears a "one moment" line (recorded when the call
began, so it starts at once), and then the model is asked to say the answer
that was found, word for word.

Lines said the same way on every call (the greeting, the "one moment" line)
are recorded once and kept (`Recordings`): asked for on each call, the
greeting took about 1.3 seconds to start.
"""

import asyncio
import base64
import hashlib
import io
import json
import re
import unicodedata
import time
import wave
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from loguru import logger
from pipecat.audio.utils import create_stream_resampler
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    CancelFrame,
    DataFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMServiceMetadataFrame,
    StartFrame,
    SystemFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor, FrameProcessorSetup
from pipecat.services.ai_service import AIService
from pipecat.services.settings import ServiceSettings
from pipecat.utils.text.base_text_aggregator import TextType
from pipecat.utils.time import time_now_iso8601

from app.costs import USAGE, CostMeter
from app.openrouter import (
    DEFAULT_BASE_URL,
    auth_headers,
    describe_failure,
    error_detail,
    new_client,
    warm_up,
)
from app.script import ScriptKeeper, ScriptLost
from app.search import FactsRequest, LookupFailed, SearchRequest, Source, WebLookup

# The fastest speech-to-speech model OpenRouter offers. "openai/gpt-audio-mini"
# costs far less per turn but starts speaking about 0.7 seconds later.
VOICE_MODEL = "openai/gpt-audio"
# The model's raw audio ("pcm16"): 16-bit mono at 24 kHz.
VOICE_SAMPLE_RATE = 24000

# Writes down the caller's words. It is off the path to an answer, but later
# turns reach the model as this text (`_history`), so it is also all Oscar
# remembers of what the caller said.
TRANSCRIPT_MODEL = "deepgram/nova-3"
# Left to itself the transcriber expects English. It wrote a caller's Hindi as
# nothing at all, or as a stray word ("Appointment"), and on real calls Oscar
# then could not say what name and time the caller had given two turns earlier
# (2026-11-12). "multi" takes several languages, mixed in one sentence too:
# English and Hindi were checked here, and Deepgram lists eight more. It wrote
# the same English as before on 13 clips of 13, and nothing for 10 sounds that
# were not speech. (openai/gpt-4o-transcribe made up words for 5 such sounds of 5.)
TRANSCRIPT_LANGUAGE = "multi"

# Audio kept from just before the caller is detected speaking, so the first
# syllable is not lost (detection needs about 0.2 seconds of speech to be sure).
PREROLL_SECONDS = 0.6
# The most caller audio sent in one request (the newest is kept).
MAX_TURN_SECONDS = 60
# A caller who speaks again before Oscar has been talking this long was most
# likely still mid-thought, not reacting to the answer. What they said before
# is then sent again with what they say next, so the model hears all of it.
ANSWERED_AFTER_SECONDS = 1.0
# The model sends an answer several times faster than it plays, and its text
# runs ahead of its audio. Text is passed on at this pace behind the audio (real
# answers measured about 19 characters a second), so that when the caller cuts
# Oscar off, only words that were actually played are kept as said.
SPOKEN_CHARS_PER_SECOND = 15
# How long after Oscar stops talking its voice can still be coming back through
# the caller's microphone (their device plays it a little late).
ECHO_TAIL_SECONDS = 0.3
# After a reply, how long to wait for the caller's words to be transcribed, so
# the transcript shows them before Oscar's answer.
TRANSCRIPT_WAIT_SECONDS = 3.0

REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# What the caller hears while a search runs. Asking the model for it only once
# a search is needed left 4 seconds of silence on real calls, so it is recorded
# in the agent's voice ahead of time (as the first call begins, then kept: see
# `Recordings`) and played the moment it is needed.
HOLD_LINE = "One moment, let me look that up."
SEARCH_FAILED_LINE = "Sorry, I couldn't look that up just now."
# Said when the first results call for one more search (app/search.py), which
# makes the wait several seconds longer.
STILL_LOOKING_LINE = "Still looking, one more moment."
# The decision to search is usually known before the model's own reply is ready
# to play (13 of 15 real tries; 0.15 seconds late at worst). When it is not,
# the reply waits this long for it, then plays without a search.
SEARCH_DECISION_PATIENCE_SECONDS = 1.0
# With a fact sheet, a reply played without the decision can be "I don't have
# that information" about something the sheet says, so it waits longer. Real
# decisions took 0.8 to 2.1 seconds; the model's own reply is ready after
# about 1.1.
FACTS_DECISION_PATIENCE_SECONDS = 2.0
FACTS_FAILED_LINE = "Sorry, I couldn't check that just now."
# Oscar's place in its script is worked out after each reply, while the caller
# listens to it (app/script.py). A caller who answers a short question at once
# can be quicker than that. Their turn then waits this long for it: told a step
# it has already done, Oscar would ask the same thing twice.
SCRIPT_PLACE_PATIENCE_SECONDS = 1.5

_MODEL_MISSING = "OpenRouter no longer offers the speech model Oscar uses."


# How many times an answer is asked for. A request that fails before any of
# its answer has been said is made once more: on a real call on 2026-11-13 the
# provider refused the request for a search's answer (503, "Provider returned
# error"), and the caller heard "One moment, let me look that up." and then
# nothing.
ANSWER_ATTEMPTS = 2


class _AnswerFailed(Exception):
    """The provider reported an error in the middle of a streamed answer."""


class _NotAnswered(Exception):
    """A request for an answer failed before any of it was said. The message is the reason."""

    def __init__(self, reason: str, *, may_pass: bool = True):
        super().__init__(reason)
        # Whether asking again could work (it cannot with a wrong key or no credit).
        self.may_pass = may_pass


def greeting_request(greeting: str) -> str:
    """What the model is told so it opens the call with the agent's own words."""
    return (
        "[The call has just connected. Greet the caller by saying exactly this "
        f'and nothing else: "{greeting}"]'
    )


def say_request(text: str) -> str:
    """What the model is told so it speaks a prepared line, such as a looked-up answer."""
    return f'[Say exactly this to the caller, and nothing else: "{text}"]'


def same_words(said: str, asked: str) -> bool:
    """Whether two lines have the same words, whatever their punctuation and capitals.

    Words in any script count ("नमस्ते"), and a line with no words matches nothing,
    so a greeting is never kept when what was said cannot be checked.
    """

    def words(text: str) -> list[str]:
        text = text.casefold().replace("'", "").replace("’", "").replace("-", " ")
        # A vowel sign is part of its word: "नमस्ते" is one word, not several.
        kept = (c if c.isalnum() or unicodedata.category(c).startswith("M") else " " for c in text)
        return "".join(kept).split()

    return bool(words(asked)) and words(said) == words(asked)


def wav_file(pcm: bytes, sample_rate: int) -> bytes:
    """16-bit mono audio wrapped as a WAV file."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buffer.getvalue()


@dataclass
class GreetingFrame(DataFrame):
    """Asks the voice service to open the call by speaking the agent's greeting."""

    text: str


@dataclass
class FarewellFrame(DataFrame):
    """Asks the voice service to close the call with this line (the call's time is
    up). Nothing the caller says after it is answered, and "on_farewell_said" fires
    once it has been heard, so the call can be hung up."""

    text: str


@dataclass
class SearchingFrame(SystemFrame):
    """Sent up the pipeline when Oscar starts and stops looking something up mid-reply."""

    active: bool


class EchoGate(FrameProcessor):
    """Keeps Oscar from hearing its own voice through the caller's loudspeaker.

    Sits before turn-taking and replaces the caller's audio with silence while
    Oscar is speaking, and for a moment after. Without it, a device that lets
    Oscar's voice leak into the microphone makes Oscar cut itself off and then
    answer its own words (seen on a real call from a laptop without
    headphones). The price is that the caller cannot interrupt Oscar.

    It stays deaf through a web search in the middle of a reply too. The line
    is silent for a few seconds then, and a caller filling the silence
    ("hello?") would otherwise count as a new turn and cancel the search.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._speaking_since: float | None = None
        self._searching = False
        self._deaf_until = 0.0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        now = time.monotonic()
        if isinstance(frame, BotStartedSpeakingFrame):
            self._speaking_since = now
        elif isinstance(frame, BotStoppedSpeakingFrame):
            if self._speaking_since is not None:
                # No more of Oscar's voice can come back than was played.
                self._deaf_until = now + min(ECHO_TAIL_SECONDS, now - self._speaking_since)
                self._speaking_since = None
        elif isinstance(frame, SearchingFrame):
            self._searching = frame.active
        elif isinstance(frame, InputAudioRawFrame):
            if self._speaking_since is not None or self._searching or now < self._deaf_until:
                frame.audio = bytes(len(frame.audio))

        await self.push_frame(frame, direction)


class Recordings:
    """Lines Oscar says the same way on every call, kept as the model said them.

    Each is a file of the model's raw audio, named after everything that shapes
    how it sounds: the words, the voice, the model and what it was told. When
    any of those change, the line is recorded again on the next call (and the
    old recording is removed).
    """

    def __init__(self, folder: Path):
        self.folder = folder

    @staticmethod
    def key(*shaped_by: str) -> str:
        return hashlib.sha256("\0".join(shaped_by).encode()).hexdigest()[:24]

    def _path(self, line: str, key: str) -> Path:
        return self.folder / f"{line}-{key}.pcm"

    def get(self, line: str, key: str) -> bytes | None:
        try:
            audio = self._path(line, key).read_bytes()
        except OSError:
            return None
        return audio if len(audio) >= 2 else None

    def put(self, line: str, key: str, audio: bytes) -> None:
        path = self._path(line, key)
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            partial = path.with_suffix(".partial")
            partial.write_bytes(audio)
            partial.replace(path)  # never a half-written recording
            for older in self.folder.glob(f"{line}-*.pcm"):
                if older != path:
                    older.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(f"Could not keep the recording of the {line}: {exc}")


@dataclass
class _Capture:
    """The audio and words of one answer, as the model sends them, to keep as a recording."""

    audio: bytearray = field(default_factory=bytearray)
    words: str = ""
    complete: bool = False  # the answer arrived in full


class TurnTiming:
    """How long one reply took to be heard, step by step, for the log.

    Times are seconds after the caller's turn was judged over (or the greeting
    was asked for). The caller stopped speaking a little before that: the voice
    detector waits for 0.2 seconds of silence first. A turn with web search on
    reads, for example:

        Turn 3: heard after 1.19s (answer began 1.04s, lookup decided 1.19s)

    The answer cannot be played before the lookup is decided, so a decision
    later than the answer's start is time the caller waited for it.
    """

    def __init__(self, what: str):
        self.what = what
        self._start = time.monotonic()
        self.marks: dict[str, float] = {}

    def mark(self, step: str) -> None:
        """Note when `step` first happened (later times of the same step are ignored)."""
        self.marks.setdefault(step, round(time.monotonic() - self._start, 2))

    def summary(self) -> str | None:
        """One line for the log, or None when nothing was heard."""
        heard = self.marks.get("heard")
        if heard is None:
            return None
        steps = [
            f"{label} {self.marks[step]:.2f}s"
            for step, label in (("answer", "answer began"), ("decided", "lookup decided"))
            if step in self.marks
        ]
        return f"{self.what}: heard after {heard:.2f}s" + (f" ({', '.join(steps)})" if steps else "")


class OpenRouterVoiceService(AIService):
    """Hears each turn of the caller and answers in speech.

    Sits between the turn-taking logic and the audio output. Pipecat's user
    aggregator decides when the caller has started and finished a turn; this
    service collects the audio in between, asks the model, and plays the reply.
    """

    def __init__(
        self,
        *,
        api_key: str,
        voice: str,
        instructions: str,
        context: LLMContext,
        reminder: str = "",
        line_instructions: str = "",
        lookup: WebLookup | None = None,
        script: ScriptKeeper | None = None,
        script_reminder: str = "",
        base_url: str = DEFAULT_BASE_URL,
        model: str = VOICE_MODEL,
        client: httpx.AsyncClient | None = None,
        recordings: Recordings | None = None,
        warm_up: bool = False,
        meter: CostMeter | None = None,
        **kwargs,
    ):
        super().__init__(settings=ServiceSettings(model=model), **kwargs)
        self._voice = voice
        self._instructions = instructions
        # Read by the model last, just before the turn it is about to answer.
        # How it should sound (an accent) holds on every reply only when it is
        # repeated here; given once in the instructions, it fades now and then.
        self._reminder = reminder
        # What the model is told when it is handed a line to say during a
        # search. Its usual instructions will not do: told (rightly, for its
        # own replies) never to say it is looking something up, it refused to
        # say the "one moment" line on four real calls out of four.
        self._line_instructions = line_instructions or instructions
        # Web search, when it is set up.
        self._lookup = lookup
        # The "one moment" line as (audio at the model's rate, its words), once recorded.
        self._hold_line: tuple[bytes, str] | None = None
        self._hold_line_task: asyncio.Task | None = None
        # The steps Oscar works through on the call, when the owner wrote a script.
        self._script = script
        # How Oscar sounds, in a few words, to close the note about its step.
        self._script_reminder = script_reminder
        self._place_task: asyncio.Task | None = None
        self._register_event_handler("on_web_search")
        self._register_event_handler("on_facts_failed")
        self._register_event_handler("on_script_lost")
        self._register_event_handler("on_farewell_said")
        # Saying goodbye (FarewellFrame): no more replies.
        self._closing = False
        self._context = context
        self._chat_url = f"{base_url}/chat/completions"
        self._transcript_url = f"{base_url}/audio/transcriptions"
        self._headers = auth_headers(api_key)
        self._base_url = base_url
        self._client = client or new_client(timeout=REQUEST_TIMEOUT)
        # Transcripts get connections of their own: sent at the same moment as
        # the request for an answer, one could otherwise take the open
        # connection and leave the answer to set up a new one.
        self._transcript_client = client or new_client(timeout=REQUEST_TIMEOUT)
        # The greeting and the "one moment" line, kept between calls.
        self._recordings = recordings
        self._capture: _Capture | None = None
        # Opens the connections the first turn will use as the call starts.
        self._warm_up = warm_up
        # Counts what the call's requests cost (app/costs.py).
        self._meter = meter
        self._resampler = create_stream_resampler()
        self._in_rate = 16000
        self._out_rate = VOICE_SAMPLE_RATE

        self._preroll = bytearray()
        # The caller's audio for the turn in progress; None between turns.
        self._turn_audio: bytearray | None = None
        # Audio of the turns Oscar has not answered yet. Usually that is one
        # turn. When the caller carries on talking before Oscar answers, their
        # earlier turns stay here and go out again with the next one, so the
        # model hears "Hey ... are you there?" as one thing.
        self._unanswered = bytearray()
        # The words of those turns, each being written down by the transcriber.
        self._unanswered_words: list[asyncio.Task] = []
        # How many messages the conversation had when that audio began: the
        # model gets the history up to there as text, and the rest as audio.
        self._history_length = 0
        # When Oscar's current stretch of speech started playing to the caller.
        self._speaking_since: float | None = None

        self._response_task: asyncio.Task | None = None
        self._responding = False  # the reply's start has been announced downstream
        self._speaking = False  # the reply's audio has started
        self._wrote_text = False  # words of the reply have been passed on
        self._starting_part = True  # the next words open a new part of the reply
        self._reply_text = ""  # the words of the reply so far
        self._transcript_tasks: set[asyncio.Task] = set()
        # How long the reply in progress is taking, step by step (for the log).
        self._timing: TurnTiming | None = None
        self._turns_heard = 0  # for the log: "Turn 3: ..."

    def can_generate_metrics(self) -> bool:
        return True

    def service_metadata_frame(self) -> LLMServiceMetadataFrame:
        # Tells the aggregators a speech-to-speech model is in charge: turns end
        # on silence alone, and the caller's transcript may arrive late.
        return LLMServiceMetadataFrame(service_name=self.name, is_realtime_service=True)

    async def setup(self, setup: FrameProcessorSetup):
        await super().setup(setup)
        self._in_rate = setup.audio_in_sample_rate
        self._out_rate = setup.audio_out_sample_rate

    async def start(self, frame: StartFrame):
        await super().start(frame)
        if self._warm_up:
            self.create_task(warm_up(self._client, self._base_url))
            if self._lookup is not None:
                self.create_task(self._lookup.warm_up())
        if self._lookup is not None:
            recorded = self._recorded("hold-line", self._line_instructions, HOLD_LINE)
            if recorded:
                self._hold_line = (recorded, HOLD_LINE)
            else:
                self._hold_line_task = self.create_task(self._record_hold_line())

    async def stop(self, frame: EndFrame):
        await super().stop(frame)
        await self._shut_down()

    async def cancel(self, frame: CancelFrame):
        await super().cancel(frame)
        await self._shut_down()

    async def cleanup(self):
        await super().cleanup()
        await self._client.aclose()
        await self._transcript_client.aclose()
        if self._lookup:
            await self._lookup.aclose()
        if self._script:
            await self._script.aclose()

    async def _shut_down(self) -> None:
        await self._abort_response()
        for task in (self._hold_line_task, self._place_task):
            if task and not task.done():
                await self.cancel_task(task)
        self._hold_line_task = self._place_task = None
        for task in list(self._transcript_tasks):
            await self.cancel_task(task)

    # Frames

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, InputAudioRawFrame):
            self._hear(frame.audio)
        elif isinstance(frame, UserStartedSpeakingFrame):
            self._turn_started()
        elif isinstance(frame, UserStoppedSpeakingFrame):
            await self._turn_stopped()
        elif isinstance(frame, BotStartedSpeakingFrame):
            self._speaking_since = time.monotonic()
        elif isinstance(frame, BotStoppedSpeakingFrame):
            if self._speaking_since is not None:  # said to the end, not cut off
                self._speaking_since = None
                self._unanswered.clear()
            if self._closing:
                await self._call_event_handler("on_farewell_said")
        elif isinstance(frame, InterruptionFrame):
            # The caller spoke over Oscar (or over a reply still on its way).
            since, self._speaking_since = self._speaking_since, None
            if since is not None and time.monotonic() - since >= ANSWERED_AFTER_SECONDS:
                self._unanswered.clear()
            await self._abort_response()
        elif isinstance(frame, GreetingFrame):
            await self._speak_greeting(frame.text)
            return
        elif isinstance(frame, FarewellFrame):
            await self._speak_farewell(frame.text)
            return

        await self.push_frame(frame, direction)

    def _hear(self, audio: bytes) -> None:
        if self._turn_audio is not None:
            self._turn_audio += audio
            return
        self._preroll += audio
        keep = int(self._in_rate * PREROLL_SECONDS) * 2
        if len(self._preroll) > keep:
            del self._preroll[: len(self._preroll) - keep]

    def _turn_started(self) -> None:
        if self._turn_audio is None:
            self._turn_audio = bytearray(self._preroll)
            self._preroll.clear()

    async def _turn_stopped(self) -> None:
        audio = bytes(self._turn_audio or b"")
        self._turn_audio = None
        if self._closing:
            return  # goodbye has been said: nothing more is answered
        if len(audio) < self._in_rate // 5:  # under a tenth of a second: nothing to hear
            return

        transcript = self._transcribe_in_background(audio)

        if not self._unanswered:
            self._history_length = len(self._context.get_messages())
            self._unanswered_words = []
        self._unanswered += audio
        self._unanswered_words.append(transcript)
        limit = MAX_TURN_SECONDS * self._in_rate * 2
        if len(self._unanswered) > limit:
            del self._unanswered[: len(self._unanswered) - limit]
        clip = wav_file(bytes(self._unanswered), self._in_rate)

        caller_turn = {
            "role": "user",
            "content": [
                {
                    "type": "input_audio",
                    "input_audio": {"data": base64.b64encode(clip).decode(), "format": "wav"},
                }
            ],
        }
        await self._start_response(
            self._history(), caller_turn, heard=list(self._unanswered_words)
        )

    # Replies

    async def _speak_greeting(self, greeting: str) -> None:
        await self._abort_response()
        self._response_task = self.create_task(self._greet(greeting))

    async def _greet(self, greeting: str) -> None:
        """Open the call with the greeting: as recorded on an earlier call, or asked for now (and kept)."""
        # Kept by what shapes how it sounds (who Oscar is, its accent, the owner's
        # instructions), not by the date or the note about searching that the
        # full instructions also carry: those would record it again every day.
        recorded = self._recorded("greeting", self._line_instructions, greeting)
        if recorded:
            self._timing = timing = TurnTiming("Greeting (recorded)")
            await self._play_recorded(recorded, greeting)
            await self._end_reply(timing)
            return
        self._capture = _Capture() if self._recordings else None
        instructions = {"role": "system", "content": self._instructions}
        try:
            await self._respond(
                [instructions], {"role": "user", "content": greeting_request(greeting)}, None
            )
        finally:
            # Cut off (the caller spoke over it, or hung up), it is not kept,
            # and no later answer is collected.
            capture, self._capture = self._capture, None
        if capture and capture.complete and capture.audio and same_words(capture.words, greeting):
            self._keep("greeting", bytes(capture.audio), self._line_instructions, greeting)

    async def _speak_farewell(self, line: str) -> None:
        self._closing = True
        await self._abort_response()
        self._response_task = self.create_task(self._say_farewell(line))

    async def _say_farewell(self, line: str) -> None:
        """Close the call with `line`: as recorded on an earlier call, or asked for now (and kept)."""
        recorded = self._recorded("farewell", self._line_instructions, line)
        if recorded:
            self._timing = timing = TurnTiming("Farewell (recorded)")
            await self._play_recorded(recorded, line)
            await self._end_reply(timing)
            return
        self._capture = _Capture() if self._recordings else None
        try:
            self._timing = timing = TurnTiming("Farewell")
            await self._say_line([{"role": "system", "content": self._line_instructions}], line)
            await self._end_reply(timing)
        finally:
            capture, self._capture = self._capture, None
        if capture and capture.complete and capture.audio and same_words(capture.words, line):
            self._keep("farewell", bytes(capture.audio), self._line_instructions, line)

    def _recording_key(self, *shaped_by: str) -> str:
        """What a recorded line depends on: its words and how the model was asked to say them."""
        return Recordings.key(self._settings.model, self._voice, self._reminder, *shaped_by)

    def _recorded(self, line: str, *shaped_by: str) -> bytes | None:
        if not self._recordings:
            return None
        return self._recordings.get(line, self._recording_key(*shaped_by))

    def _keep(self, line: str, audio: bytes, *shaped_by: str) -> None:
        if self._recordings:
            self._recordings.put(line, self._recording_key(*shaped_by), audio[: len(audio) & ~1])

    def _history(self) -> list[dict]:
        """The agent's instructions and the conversation so far, as text."""
        messages: list[dict] = [{"role": "system", "content": self._instructions}]
        for message in self._context.get_messages()[: self._history_length]:
            if not isinstance(message, dict):
                continue
            role, content = message.get("role"), message.get("content")
            if role in ("user", "assistant") and isinstance(content, str) and content.strip():
                messages.append({"role": role, "content": content})
        return messages

    def _reminder_message(self) -> list[dict]:
        return [{"role": "system", "content": self._reminder}] if self._reminder else []

    def _note_before_the_caller(self) -> list[dict]:
        """What the model reads last before a caller's turn: how it sounds, then its place in the script."""
        step = self._script.note() if self._script else ""
        if step:
            step = f"({' '.join(part for part in (step, self._script_reminder) if part)})"
        note = " ".join(part for part in (self._reminder, step) if part)
        return [{"role": "system", "content": note}] if note else []

    async def _start_response(
        self,
        history: list[dict],
        turn: dict,
        *,
        heard: list[asyncio.Task] | None = None,
    ) -> None:
        """Answer `turn`. For a turn of the caller's, `heard` is its words being written down."""
        await self._abort_response()
        self._response_task = self.create_task(self._respond(history, turn, heard))

    async def _abort_response(self) -> None:
        """Stop a reply that is on its way or being spoken, and close it downstream."""
        task, self._response_task = self._response_task, None
        if task and not task.done():
            await self.cancel_task(task)
        if self._responding:
            await self.push_frame(LLMFullResponseEndFrame())
            if self._speaking:
                await self.push_frame(TTSStoppedFrame())
        self._responding = self._speaking = self._wrote_text = False
        await self._resampler.reset()

    async def _respond(
        self,
        history: list[dict],
        turn: dict,
        heard: list[asyncio.Task] | None,
    ) -> None:
        """Answer one turn: the model's own reply, or what was looked up for it."""
        from_caller = heard is not None  # not the greeting
        self._reply_text = ""
        self._timing = timing = TurnTiming("Turn" if from_caller else "Greeting")
        note = self._reminder_message()
        if from_caller:
            await self._settle_place()
            note = self._note_before_the_caller()
        deciding: asyncio.Task | None = None
        if from_caller and self._lookup is not None:
            # Alongside the reply: does this turn need the web or the fact sheet?
            # (The model's instructions come first in `history`; the decision
            # has its own.)
            deciding = self.create_task(self._decide(history[1:], turn))
        try:
            lookup = await self._reply_or_lookup([*history, *note, turn], deciding)
            if isinstance(lookup, FactsRequest):
                await self._answer_from_the_facts(history, lookup)
            elif lookup is not None:
                await self._answer_from_the_web(history, lookup)
        finally:
            if deciding is not None and not deciding.done():
                await self.cancel_task(deciding)

        if from_caller and self._script is not None:
            # The reply is still playing: time to work out where the script has got to.
            await self._keep_place(history[1:], heard)
        if heard and self._responding:
            # The reply is already playing; this only orders the transcript, so
            # the caller's words are written down before Oscar's answer.
            await asyncio.wait({heard[-1]}, timeout=TRANSCRIPT_WAIT_SECONDS)
        await self._end_reply(timing)

    async def _end_reply(self, timing: TurnTiming) -> None:
        """Close the reply downstream, and log how long it took to be heard."""
        if self._responding:
            await self.push_frame(LLMFullResponseEndFrame())
        self._responding = self._speaking = self._wrote_text = False
        if timing.what == "Turn" and "heard" in timing.marks:
            self._turns_heard += 1
            timing.what = f"Turn {self._turns_heard}"
        summary = timing.summary()
        if summary:
            logger.info(f"{self}: {summary}")

    async def _settle_place(self) -> None:
        """Give a place in the script that is still being worked out a moment to arrive."""
        task = self._place_task
        if task and not task.done():
            await asyncio.wait({task}, timeout=SCRIPT_PLACE_PATIENCE_SECONDS)

    async def _keep_place(self, earlier: list[dict], heard: list[asyncio.Task]) -> None:
        """Start working out which step of the script Oscar is on, now that it has replied."""
        task, self._place_task = self._place_task, None
        if task and not task.done():
            await self.cancel_task(task)  # it knew of one exchange fewer
        self._place_task = self.create_task(self._find_place(earlier, heard, self._reply_text))

    async def _find_place(self, earlier: list[dict], heard: list[asyncio.Task], reply: str) -> None:
        talk = list(earlier)
        # What the caller said in the turn just answered, as far as it was made out in time.
        written, _ = await asyncio.wait(heard, timeout=TRANSCRIPT_WAIT_SECONDS)
        words = [t.result() for t in heard if t in written and not t.cancelled() and not t.exception()]
        if any(words):
            talk.append({"role": "user", "content": " ".join(filter(None, words))})
        if reply.strip():
            talk.append({"role": "assistant", "content": reply.strip()})
        try:
            place = await self._script.find_place(talk)
        except ScriptLost as exc:
            logger.warning(f"{self}: lost its place in the script: {exc}")
            await self._call_event_handler("on_script_lost", str(exc))
            return
        logger.debug(f"{self}: now on step {place + 1} of the script")

    async def _reply_or_lookup(
        self, messages: list[dict], deciding: asyncio.Task | None
    ) -> SearchRequest | FactsRequest | None:
        """Play the model's own reply to the turn, or return the lookup the turn needs instead.

        A lookup decided while the reply is still on its way does not wait for
        it. On a real call the reply stalled for seven seconds after the
        decision was known, and the caller heard nothing all that time.
        """
        if deciding is None:
            return await self._ask(messages)
        asking = self.create_task(self._ask(messages, deciding))
        try:
            await asyncio.wait({asking, deciding}, return_when=asyncio.FIRST_COMPLETED)
            if not asking.done() and not self._responding:
                # Nothing of the reply has been played. Is it needed at all?
                lookup = None if deciding.cancelled() else deciding.result()
                if lookup is not None:
                    await self.cancel_task(asking)
                    return lookup
            return await asking
        finally:
            if not asking.done():
                await self.cancel_task(asking)

    async def _decide(self, earlier: list[dict], turn: dict) -> SearchRequest | FactsRequest | None:
        """Whether the turn needs the web or the fact sheet. Without a clear yes, neither."""
        try:
            lookup = await self._lookup.decide(earlier, turn)
            if self._timing:
                self._timing.mark("decided")
        except LookupFailed as exc:
            logger.warning(f"{self}: {exc}")
            return None
        logger.debug(f"{self}: lookup for this turn: {lookup}")
        return lookup

    async def _answer_from_the_facts(self, history: list[dict], asked: FactsRequest) -> None:
        """Say what the fact sheet answers: a text model writes it, the speech model says it.

        Without the "one moment" line the answer began 4.4 seconds after the
        caller stopped on a real call, against about 2 for an ordinary reply.
        The line fills that wait without adding to it: it is over about when
        the answer is ready to be said. A shorter "One moment." was tried and
        dropped: recorded ten times, it sounded American twice (the longer
        line never did).
        """
        earlier = history[1:]
        so_far = [
            {"role": "system", "content": self._line_instructions},
            *earlier,
            {"role": "user", "content": asked.question},
        ]
        await self.push_frame(SearchingFrame(active=True), FrameDirection.UPSTREAM)
        try:
            answer, _ = await asyncio.gather(
                self._read_the_facts(asked, earlier), self._hold_the_line(so_far)
            )
            await self._say_line([*so_far, {"role": "assistant", "content": HOLD_LINE}], answer)
        finally:
            await self.push_frame(SearchingFrame(active=False), FrameDirection.UPSTREAM)

    async def _answer_from_the_web(self, history: list[dict], search: SearchRequest) -> None:
        """Look up what the caller asked and say what was found, as one reply."""
        asked = [
            {"role": "system", "content": self._line_instructions},
            *history[1:],
            {"role": "user", "content": search.question},
        ]
        said = [HOLD_LINE]  # what Oscar has told the caller while it looks
        held = asyncio.Event()

        async def hold() -> None:
            try:
                await self._hold_the_line(asked)
            finally:
                held.set()

        async def still_looking(query: str) -> None:
            await held.wait()  # one line at a time
            await self._say_line(asked, STILL_LOOKING_LINE)
            said.append(STILL_LOOKING_LINE)

        await self.push_frame(SearchingFrame(active=True), FrameDirection.UPSTREAM)
        try:
            # The caller hears the "one moment" line while the search runs.
            answer, _ = await asyncio.gather(
                self._look_up(search, history[1:], still_looking), hold()
            )
            # The model is not handed the results (it loses the agent's accent
            # reading them: see app/search.py). It gets the turn so far as text
            # and the finished answer to say.
            await self._say_line([*asked, {"role": "assistant", "content": " ".join(said)}], answer)
        finally:
            await self.push_frame(SearchingFrame(active=False), FrameDirection.UPSTREAM)

    async def _read_the_facts(self, asked: FactsRequest, earlier: list[dict]) -> str:
        """What to tell the caller: the fact sheet's answer, or that it could not be checked."""
        problem = ""
        try:
            answer = await self._lookup.answer_from_facts(asked.question, earlier)
        except LookupFailed as exc:
            answer, problem = FACTS_FAILED_LINE, str(exc)
        except Exception as exc:  # the caller still gets an answer, if only an apology
            logger.exception(f"{self}: answering from the fact sheet broke")
            answer, problem = FACTS_FAILED_LINE, f"The lookup broke unexpectedly ({exc!r})."
        if problem:
            await self._call_event_handler("on_facts_failed", problem)
        return answer

    async def _hold_the_line(self, asked: list[dict]) -> None:
        if self._hold_line is None:
            await self._say_line(asked, HOLD_LINE)  # not recorded (yet): ask for it now
            return
        await self._play_recorded(*self._hold_line)

    async def _play_recorded(self, speech: bytes, words: str) -> None:
        """Play a recorded line (audio at the model's rate) and pass on its words."""
        if not self._responding:
            self._responding = True
            await self.push_frame(LLMFullResponseStartFrame())
        self._starting_part = True
        piece = VOICE_SAMPLE_RATE // 2  # a quarter of a second, as the model sends it
        # The words go out in step with the audio, as a live reply's do (_play):
        # cut off part-way, the words heard so far are kept, and the rest are
        # dropped with the audio that was never played.
        unsaid = deque(re.findall(r"\s*\S+", words))
        written, sent_seconds = 0, 0.0
        for start in range(0, len(speech), piece):
            chunk = speech[start : start + piece]
            await self._say(chunk)
            sent_seconds += len(chunk) / 2 / VOICE_SAMPLE_RATE
            while unsaid and written + len(unsaid[0]) <= sent_seconds * SPOKEN_CHARS_PER_SECOND:
                written += len(unsaid[0])
                await self._write(unsaid.popleft())
        for text in unsaid:
            await self._write(text)
        await self._finish_speaking()

    async def _record_hold_line(self) -> None:
        """Have the model say the "one moment" line now, to play whenever a lookup starts."""
        messages = [
            {"role": "system", "content": self._line_instructions},
            *self._reminder_message(),
            {"role": "user", "content": say_request(HOLD_LINE)},
        ]
        for _ in range(2):  # the model can say something else: then ask once more
            speech, said = b"", ""
            try:
                async with self._client.stream(
                    "POST", self._chat_url, json=self._request(messages), headers=self._headers
                ) as response:
                    if response.status_code != 200:
                        await response.aread()
                        raise _AnswerFailed(describe_failure(response, not_found=_MODEL_MISSING))
                    receipt = {"paid": False}
                    try:
                        async for piece, text in self._pieces(response, receipt):
                            speech += piece
                            said += text
                    finally:
                        self._settle(response, receipt)
            except (httpx.HTTPError, _AnswerFailed) as exc:
                # Not worth failing a call over: the line is asked for when needed instead.
                logger.warning(f"{self}: could not record the hold line: {exc!r}")
                return
            speech = speech[: len(speech) & ~1]
            if speech and same_words(said, HOLD_LINE):
                self._hold_line = (speech, HOLD_LINE)
                self._keep("hold-line", speech, self._line_instructions, HOLD_LINE)
                return
            logger.warning(f"{self}: asked for the hold line, the model said {said!r}")

    async def _look_up(self, search: SearchRequest, earlier: list[dict], still_looking) -> str:
        """What to tell the caller: the answer found, or that the search did not work.

        `earlier` is the call so far, which the answer is written in the light
        of. `still_looking` is run if the lookup goes on to a further search.
        """

        async def searched(query: str, sources: list[Source]) -> None:
            await self._call_event_handler("on_web_search", query, "", sources)

        try:
            return await self._lookup.answer(
                question=search.question,
                query=search.query,
                earlier=earlier,
                searched=searched,
                again=still_looking,
            )
        except LookupFailed as exc:
            problem = str(exc)
        except Exception as exc:  # the caller still gets an answer, if only an apology
            logger.exception(f"{self}: the web lookup broke")
            problem = f"The lookup broke unexpectedly ({exc!r})."
        await self._call_event_handler("on_web_search", search.query, problem, [])
        return SEARCH_FAILED_LINE

    async def _say_line(self, so_far: list[dict], line: str) -> None:
        turn = {"role": "user", "content": say_request(line)}
        await self._ask([*so_far, *self._reminder_message(), turn])

    async def _ask(
        self, messages: list[dict], deciding: asyncio.Task | None = None
    ) -> SearchRequest | FactsRequest | None:
        """Send one request to the model and play what it says back.

        With `deciding` (whether the turn needs a lookup), nothing is played
        until that is known. If one is needed, the model's reply is dropped
        and the lookup is returned instead.

        A request that fails with nothing said yet is made once more, unless
        the failure is one that asking again cannot get past.
        """
        search = None
        for attempt in range(1, ANSWER_ATTEMPTS + 1):
            try:
                search = await self._ask_once(messages, deciding)
            except _NotAnswered as failure:
                if failure.may_pass and attempt < ANSWER_ATTEMPTS:
                    logger.warning(f"{self}: no answer ({failure}) Asking once more.")
                    continue
                await self.push_error(f"Oscar could not answer: {failure}")
            break
        # Whatever was said has to be closed off, failure or not.
        await self._finish_speaking()
        return search

    async def _ask_once(
        self, messages: list[dict], deciding: asyncio.Task | None
    ) -> SearchRequest | FactsRequest | None:
        """One request to the model, played back. Raises _NotAnswered if it fails with nothing said."""
        try:
            await self.start_ttfb_metrics()
            async with self._client.stream(
                "POST", self._chat_url, json=self._request(messages), headers=self._headers
            ) as response:
                if response.status_code != 200:
                    await response.aread()
                    raise _NotAnswered(
                        describe_failure(response, not_found=_MODEL_MISSING),
                        # A wrong key, an empty account or a missing model stays that way.
                        may_pass=response.status_code >= 500,
                    )
                receipt = {"paid": False}
                try:
                    return await self._play(response, deciding, receipt)
                finally:
                    self._settle(response, receipt)
        except httpx.HTTPError as exc:
            # (One that broke off an answer part-way is dealt with in _play.)
            raise _NotAnswered(f"OpenRouter was unreachable ({exc}).") from None

    def _request(self, messages: list[dict]) -> dict:
        return {
            "model": self._settings.model,
            "stream": True,
            "modalities": ["text", "audio"],
            "audio": {"voice": self._voice, "format": "pcm16"},
            "messages": messages,
            "usage": USAGE,
        }

    def _settle(self, response: httpx.Response, receipt: dict) -> None:
        """Count a streamed answer that stopped before saying what it cost (it was cut off)."""
        if self._meter and not receipt["paid"]:
            self._meter.charge_later(response.headers.get("x-generation-id", ""))

    async def _finish_speaking(self) -> None:
        """Close off a stretch of speech (the reply may go on after a pause)."""
        if self._speaking:
            tail = await self._resampler.flush()
            if tail:
                await self.push_frame(TTSAudioRawFrame(tail, self._out_rate, 1))
            await self.push_frame(TTSStoppedFrame())
            self._speaking = False

    async def _decision(self, deciding: asyncio.Task) -> SearchRequest | FactsRequest | None:
        """The lookup decision, waited for briefly. None means: go on without a lookup."""
        patience = SEARCH_DECISION_PATIENCE_SECONDS
        if getattr(self._lookup, "has_facts", False):
            patience = FACTS_DECISION_PATIENCE_SECONDS
        try:
            return await asyncio.wait_for(asyncio.shield(deciding), patience)
        except asyncio.TimeoutError:
            logger.warning("The decision whether to look something up came too late: answering without.")
            return None

    async def _pieces(
        self, response: httpx.Response, receipt: dict | None = None
    ) -> AsyncIterator[tuple[bytes, str]]:
        """The (speech, its text) pieces of a streamed answer, as they arrive.

        What the answer cost comes in its last chunk: it is counted, and
        `receipt["paid"]` set. Raises _AnswerFailed when the provider reports an
        error part-way.
        """
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue  # keep-alive comments
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                continue
            if not isinstance(chunk, dict):
                continue
            if chunk.get("usage") and receipt is not None:
                if self._meter:
                    self._meter.charge(chunk)
                receipt["paid"] = True
            if chunk.get("error"):
                raise _AnswerFailed(error_detail(chunk) or "the model's provider failed.")
            for choice in chunk.get("choices") or []:
                delta = (choice.get("delta") or {}).get("audio") or {}
                speech, text = delta.get("data"), delta.get("transcript")
                try:
                    speech = base64.b64decode(speech) if speech else b""
                except ValueError:
                    speech = b""  # unreadable audio: its words still count
                if speech or text:
                    yield speech, text or ""

    async def _play(
        self, response: httpx.Response, deciding: asyncio.Task | None, receipt: dict | None = None
    ) -> SearchRequest | FactsRequest | None:
        """Turn the model's streamed answer into speech and text frames.

        Raises _NotAnswered when the answer fails before any of it was said.
        One that fails part-way is reported, and what was said of it stands.
        """
        pending = b""  # an odd leftover byte, until its other half arrives
        words: deque[str] = deque()  # text held back until its audio has been sent
        written = 0  # characters of text passed on so far
        sent_seconds = 0.0  # audio passed on so far
        self._starting_part = True
        capture = self._capture
        if capture:
            capture.audio.clear()
            capture.words, capture.complete = "", False
        try:
            async for speech, text in self._pieces(response, receipt):
                if capture:
                    capture.audio += speech
                    capture.words += text
                if self._timing:
                    self._timing.mark("answer")
                if deciding is not None:
                    # The reply is ready to play. Does the turn need a lookup instead?
                    search, deciding = await self._decision(deciding), None
                    if search is not None:
                        return search
                if not self._responding:
                    self._responding = True
                    await self.push_frame(LLMFullResponseStartFrame())
                if text:
                    words.append(text)
                pending += speech
                usable = len(pending) & ~1
                if usable:
                    await self._say(pending[:usable])
                    sent_seconds += usable / 2 / VOICE_SAMPLE_RATE
                    pending = pending[usable:]
                while words and written + len(words[0]) <= sent_seconds * SPOKEN_CHARS_PER_SECOND:
                    written += len(words[0])
                    await self._write(words.popleft())
        except (_AnswerFailed, httpx.HTTPError) as exc:
            reason = (
                str(exc)
                if isinstance(exc, _AnswerFailed)
                else f"OpenRouter was unreachable ({exc})."
            )
            if not sent_seconds:
                # Its words may have come, but none of its speech: nothing was said.
                raise _NotAnswered(reason) from None
            await self.push_error(f"Oscar could not answer: {reason}")
        else:
            if capture:
                capture.complete = True
        # All the audio has been sent, so the rest of the text follows it.
        for text in words:
            await self._write(text)
        return None

    async def _write(self, text: str) -> None:
        """Pass on words of the reply. They count as said once the audio before them has played."""
        if self._starting_part and self._wrote_text and not text.startswith(" "):
            text = " " + text  # a later part of the same reply
        self._starting_part = False
        self._wrote_text = True
        self._reply_text += text
        frame = TTSTextFrame(text, text_type=TextType.SENTENCE)
        frame.includes_inter_frame_spaces = True
        await self.push_frame(frame)

    async def _say(self, audio: bytes) -> None:
        if not self._speaking:
            self._speaking = True
            if self._timing:
                self._timing.mark("heard")
            await self.stop_ttfb_metrics()
            await self.push_frame(TTSStartedFrame())
        audio = await self._resampler.resample(audio, VOICE_SAMPLE_RATE, self._out_rate)
        if audio:
            await self.push_frame(TTSAudioRawFrame(audio, self._out_rate, 1))

    # The caller's words, for the transcript

    def _transcribe_in_background(self, audio: bytes) -> asyncio.Task:
        task = self.create_task(self._transcribe(audio))
        self._transcript_tasks.add(task)
        task.add_done_callback(self._transcript_tasks.discard)
        return task

    async def _transcribe(self, audio: bytes) -> str | None:
        """Write down what the caller said. Returns it too, or None when nothing was made out."""
        payload = {
            "model": TRANSCRIPT_MODEL,
            "language": TRANSCRIPT_LANGUAGE,
            "usage": USAGE,
            "input_audio": {
                "data": base64.b64encode(wav_file(audio, self._in_rate)).decode(),
                "format": "wav",
            },
        }
        try:
            response = await self._transcript_client.post(
                self._transcript_url, json=payload, headers=self._headers
            )
        except httpx.HTTPError as exc:
            logger.warning(f"{self}: could not transcribe the caller: {exc}")
            return None
        if response.status_code != 200:
            logger.warning(f"{self}: could not transcribe the caller: {describe_failure(response)}")
            return None
        try:
            body = response.json()
            text = body.get("text")
        except (ValueError, AttributeError):
            body, text = None, None
        if self._meter:
            self._meter.charge(body)
        if not isinstance(text, str) or not text.strip():
            return None
        await self.push_frame(
            TranscriptionFrame(text.strip(), "", time_now_iso8601()), FrameDirection.UPSTREAM
        )
        return text.strip()
