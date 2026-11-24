"""The part of a phone call after Oscar puts the caller through to a person.

Oscar's own audio stream ends when Twilio takes the call over (app/phone.py,
put_call_through), so Oscar cannot record that part itself. When calls are
recorded, Twilio records it instead, from when the person answers, with the
caller on one channel and the person on the other. Twilio says when the
recording is ready at /twilio/put-through-recording, and Oscar then:

1. fetches it, and deletes Twilio's copy (Oscar keeps its own, as it does the
   rest of the call);
2. adds it to the end of the call's recording after a moment's silence, so the
   whole call plays as one in Call history;
3. finds where each of the two spoke (Silero voice detection, a channel at a
   time), writes each stretch down with the transcriber calls use, and adds the
   lines to the transcript with their times in the recording;
4. writes the call's summary again, now that it covers the whole conversation
   (it is not sent to the webhook a second time).

Twilio charges for recording that part. A recording Oscar could not fetch is
left on Twilio, where the owner can still find it (Monitor > Call recordings).
"""

import asyncio
import base64
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import av
import httpx
import numpy as np
from loguru import logger

from app import numbers
from app.calls import CallManager
from app.config import Config
from app.costs import USAGE, CostMeter
from app.openrouter import auth_headers, describe_failure, new_client
from app.recording import encode, recording_path, recordings_folder
from app.speech import TRANSCRIPT_LANGUAGE, TRANSCRIPT_MODEL, wav_file

# Silence between Oscar's part of the recording and the part put through.
GAP_SECONDS = 1.0
# The most of Twilio's recording Oscar fetches: about four hours of it.
RECORDING_MOST_BYTES = 450 * 1024 * 1024
FETCH_TIMEOUT = httpx.Timeout(120.0, connect=10.0)
# Twilio has a recording ready a moment after it says so, now and then: asked again after this.
FETCH_AGAIN_AFTER_SECONDS = 5.0
# Oscar's part of the recording is still being encoded when a short put-through ends.
WAIT_FOR_RECORDING_SECONDS = 120
# The summary written as Oscar's part ended may still be being written.
WAIT_FOR_SUMMARY_SECONDS = 180

# Finding speech: a 32 ms window is speech at this confidence, and stays so down to the
# lower one; a stretch ends after this much quiet. Each stretch keeps a little either side.
SPEECH_STARTS = 0.5
SPEECH_GOES_ON = 0.35
SPEECH_ENDS_AFTER_SECONDS = 0.8
SPEECH_PAD_SECONDS = 0.2
SHORTEST_SPEECH_SECONDS = 0.3
# A longer stretch is written down in parts this long.
LONGEST_STRETCH_SECONDS = 30.0
# One speaker's lines this close together read as one line.
SAME_LINE_GAP_SECONDS = 1.5
TRANSCRIBING_AT_ONCE = 4

ROLES = ("user", "person")  # the caller's channel, then the person's


class PutThroughFailed(Exception):
    """The part put through could not be kept. The message says why, for the transcript."""


def _twilio_client(cfg: Config):
    return numbers.twilio_client(cfg)


def recording_url_ok(cfg: Config, url: str, recording_sid: str) -> bool:
    """Whether `url` is this account's recording on Twilio's API, the only place Oscar
    sends its Twilio keys to fetch one."""
    parts = urlsplit(url)
    host_ok = parts.hostname == "api.twilio.com" or bool(
        parts.hostname and re.fullmatch(r"api\.[a-z0-9-]+\.twilio\.com", parts.hostname)
    )
    path = f"/2010-04-01/Accounts/{cfg.twilio_account_sid}/Recordings/{recording_sid}"
    return (
        parts.scheme == "https"
        and host_ok
        and parts.port is None
        and parts.username is None
        and not parts.query
        and re.fullmatch(r"RE[0-9a-f]{32}", recording_sid or "") is not None
        and parts.path.removesuffix(".wav").removesuffix(".json") == path
    )


async def fetch_recording(
    cfg: Config, url: str, target: Path, client: httpx.AsyncClient | None = None
) -> None:
    """Download Twilio's recording, both channels, as a WAV, to `target`."""
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=FETCH_TIMEOUT)
    source = url.removesuffix(".wav").removesuffix(".json") + ".wav"
    try:
        for attempt in (1, 2):
            # httpx drops the keys if Twilio sends the download elsewhere (another host).
            async with client.stream(
                "GET", source, auth=(cfg.twilio_account_sid, cfg.twilio_auth_token), follow_redirects=True
            ) as response:
                if response.status_code == 200:
                    size = 0
                    with target.open("wb") as out:
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > RECORDING_MOST_BYTES:
                                raise PutThroughFailed("Twilio's recording of it was too long to keep.")
                            out.write(chunk)
                    return
                await response.aread()
                problem = f"Twilio answered {response.status_code} when asked for the recording."
                if attempt == 2 or response.status_code not in (404, 429) and response.status_code < 500:
                    raise PutThroughFailed(problem)
            await asyncio.sleep(FETCH_AGAIN_AFTER_SECONDS)
    except httpx.HTTPError as exc:
        raise PutThroughFailed(f"Twilio's recording could not be fetched ({type(exc).__name__}).") from exc
    finally:
        if own_client:
            await client.aclose()


def read_audio(path: Path, rate: int, channels: int) -> np.ndarray:
    """A recording's audio as 16-bit samples at `rate`, shaped (channels, samples)."""
    layout = "stereo" if channels == 2 else "mono"
    pieces: list[np.ndarray] = []
    with av.open(str(path)) as source:
        stream = source.streams.audio[0]
        resampler = av.AudioResampler(format="s16p", layout=layout, rate=rate)
        for frame in source.decode(stream):
            pieces.extend(out.to_ndarray() for out in resampler.resample(frame))
        pieces.extend(out.to_ndarray() for out in resampler.resample(None))
    if not pieces:
        return np.zeros((channels, 0), dtype=np.int16)
    return np.concatenate(pieces, axis=1).astype(np.int16)


def channel_count(path: Path) -> int:
    with av.open(str(path)) as source:
        return source.streams.audio[0].codec_context.channels


def recording_rate(path: Path) -> int | None:
    if not path.exists():
        return None
    with av.open(str(path)) as source:
        return source.streams.audio[0].codec_context.sample_rate


def speech_stretches(samples: np.ndarray, rate: int) -> list[tuple[int, int]]:
    """Where someone speaks in one channel: (first sample, end sample) of each stretch.
    Long stretches are split, so each can be written down on its own."""
    from pipecat.audio.vad.silero import SileroVADAnalyzer

    if samples.size == 0:
        return []
    vad_rate = rate if rate in (8000, 16000) else 16000
    audio = samples
    if vad_rate != rate:
        audio = _resample(samples, rate, vad_rate)
    detector = SileroVADAnalyzer(sample_rate=vad_rate)
    detector.set_sample_rate(vad_rate)
    window = detector.num_frames_required()
    quiet_windows_to_end = max(1, round(SPEECH_ENDS_AFTER_SECONDS * vad_rate / window))
    found: list[tuple[int, int]] = []
    start = None
    last_speech = 0
    quiet = 0
    for index in range(len(audio) // window):
        scored = detector.voice_confidence(audio[index * window : (index + 1) * window].tobytes())
        confidence = float(np.ravel(scored)[0])
        if start is None:
            if confidence >= SPEECH_STARTS:
                start, last_speech, quiet = index, index, 0
        elif confidence >= SPEECH_GOES_ON:
            last_speech, quiet = index, 0
        else:
            quiet += 1
            if quiet >= quiet_windows_to_end:
                found.append((start, last_speech + 1))
                start = None
    if start is not None:
        found.append((start, last_speech + 1))

    scale = rate / vad_rate
    pad = int(SPEECH_PAD_SECONDS * rate)
    stretches: list[tuple[int, int]] = []
    for first, end in found:
        begin = max(0, int(first * window * scale) - pad)
        finish = min(len(samples), int(end * window * scale) + pad)
        if finish - begin < SHORTEST_SPEECH_SECONDS * rate:
            continue
        longest = int(LONGEST_STRETCH_SECONDS * rate)
        while finish - begin > longest:
            stretches.append((begin, begin + longest))
            begin += longest
        stretches.append((begin, finish))
    return stretches


def _resample(samples: np.ndarray, rate: int, to_rate: int) -> np.ndarray:
    frame = av.AudioFrame.from_ndarray(samples.reshape(1, -1).astype(np.int16), format="s16", layout="mono")
    frame.sample_rate = rate
    resampler = av.AudioResampler(format="s16", layout="mono", rate=to_rate)
    pieces = [out.to_ndarray().reshape(-1) for out in resampler.resample(frame)]
    pieces += [out.to_ndarray().reshape(-1) for out in resampler.resample(None)]
    return np.concatenate(pieces).astype(np.int16) if pieces else np.zeros(0, dtype=np.int16)


async def write_down(
    client: httpx.AsyncClient, cfg: Config, meter: CostMeter, pcm: bytes, rate: int
) -> str | None:
    """What was said in a stretch of audio, or None when nothing was made out."""
    payload = {
        "model": TRANSCRIPT_MODEL,
        "language": TRANSCRIPT_LANGUAGE,
        "usage": USAGE,
        "input_audio": {"data": base64.b64encode(wav_file(pcm, rate)).decode(), "format": "wav"},
    }
    response = await client.post(
        f"{cfg.openrouter_base_url}/audio/transcriptions", json=payload, headers=auth_headers(cfg.openrouter_api_key)
    )
    if response.status_code != 200:
        raise PutThroughFailed(f"It could not be written down: {describe_failure(response)}")
    try:
        body = response.json()
    except ValueError:
        return None
    meter.charge(body)
    text = body.get("text") if isinstance(body, dict) else None
    return text.strip() if isinstance(text, str) and text.strip() else None


def lines_from(heard: list[tuple[float, float, str, str]]) -> list[tuple[float, str, str]]:
    """(start, end, role, words) of each stretch written down, as transcript lines in the
    order they were said: (start, role, words). One speaker's stretches close together
    make one line."""
    lines: list[list] = []
    last_end: dict[str, float] = {}
    for start, end, role, words in sorted(heard):
        previous = lines[-1] if lines else None
        if previous and previous[1] == role and start - last_end.get(role, -1e9) <= SAME_LINE_GAP_SECONDS:
            previous[2] = f"{previous[2]} {words}"
        else:
            lines.append([start, role, words])
        last_end[role] = end
    return [(start, role, words) for start, role, words in lines]


def join_recordings(target: Path, scratch: Path, added: np.ndarray, rate: int) -> tuple[float, float]:
    """Add `added` (mono samples at `rate`) to the end of the call's recording at `target`,
    after a moment's silence; with no recording yet, it becomes the recording. Returns
    (where the added audio begins, the recording's new length), in seconds."""
    before = read_audio(target, rate, 1)[0] if target.exists() else np.zeros(0, dtype=np.int16)
    gap = np.zeros(int(GAP_SECONDS * rate) if before.size else 0, dtype=np.int16)
    scratch.write_bytes(np.concatenate([before, gap, added]).astype(np.int16).tobytes())
    try:
        length = encode(scratch, target, rate)
    finally:
        scratch.unlink(missing_ok=True)
    return (before.size + gap.size) / rate, length


def mixed(channels: np.ndarray) -> np.ndarray:
    """Both sides of the conversation in one track, as the rest of the recording is."""
    total = channels.astype(np.int32).sum(axis=0)
    return np.clip(total, -32768, 32767).astype(np.int16)


async def _wait_for(condition, seconds: float, every: float = 1.0) -> bool:
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(every)
    return True


def _encoding(folder: Path, call_id: str) -> bool:
    """Whether Oscar's part of the call's recording is still being written or encoded."""
    return any(folder.glob(f".{call_id}.[0-9]*.pcm")) or (folder / f".{call_id}.m4a.partial").exists()


async def keep_put_through(
    cfg: Config,
    calls: CallManager,
    call_id: str,
    recording_sid: str,
    recording_url: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> None:
    """Keep Twilio's recording of the part of the call put through: its audio at the end
    of the call's recording, and what was said in the transcript."""
    folder = recordings_folder(cfg.db_path)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    download = folder / f".{call_id}.put-through.wav.partial"
    scratch = folder / f".{call_id}.put-through.pcm.partial"
    fetched = False
    try:
        if not recording_url_ok(cfg, recording_url, recording_sid):
            raise PutThroughFailed("Twilio gave an address for the recording that Oscar does not fetch from.")
        await fetch_recording(cfg, recording_url, download, client)
        fetched = True
        await _forget_on_twilio(cfg, recording_sid)

        # Oscar's part is encoded a moment after the call is handed over.
        await _wait_for(lambda: not _encoding(folder, call_id), WAIT_FOR_RECORDING_SECONDS)
        if calls.store.get_call(call_id) is None:
            return  # deleted meanwhile: nothing of it is kept
        target = recording_path(cfg.db_path, call_id)
        rate = await asyncio.to_thread(recording_rate, target) or 8000
        channels = await asyncio.to_thread(channel_count, download)
        audio = await asyncio.to_thread(read_audio, download, rate, 2 if channels >= 2 else 1)
        offset, length = await asyncio.to_thread(join_recordings, target, scratch, mixed(audio), rate)
        if calls.store.get_call(call_id) is None:
            target.unlink(missing_ok=True)
            return
        calls.update(call_id, recording_seconds=round(length, 1))

        if channels < 2:
            calls.add_message(
                call_id,
                "note",
                "The recording of the call after it was put through came as one track, so who said "
                "what could not be told apart. Listen to it in the recording.",
                at_seconds=offset,
            )
            return
        lines = await _transcribe(cfg, calls, call_id, audio, rate)
        if calls.store.get_call(call_id) is None:
            return  # deleted while it was being written down
        if not lines:
            calls.add_message(call_id, "note", "Nothing was made out after the call was put through.", at_seconds=offset)
        for start, role, words in lines:
            calls.add_message(call_id, role, words, at_seconds=round(offset + start, 2))
        await _summarise_again(cfg, calls, call_id)
    except PutThroughFailed as exc:
        logger.warning(f"Call {call_id}: the part put through could not be kept: {exc}")
        where = "" if fetched else " Twilio still has its recording of it (Monitor > Call recordings)."
        if calls.store.get_call(call_id):
            calls.add_message(call_id, "note", f"The part after the call was put through could not be kept. {exc}{where}")
    except Exception:
        logger.exception(f"Call {call_id}: the part put through could not be kept")
        if calls.store.get_call(call_id):
            where = "" if fetched else " Twilio still has its recording of it (Monitor > Call recordings)."
            calls.add_message(call_id, "note", f"The part after the call was put through could not be kept.{where}")
    finally:
        download.unlink(missing_ok=True)
        scratch.unlink(missing_ok=True)


async def _forget_on_twilio(cfg: Config, recording_sid: str) -> None:
    """Delete Twilio's copy: Oscar keeps the call's recording itself."""
    try:
        await asyncio.to_thread(_twilio_client(cfg).recordings(recording_sid).delete)
    except Exception as exc:
        logger.warning(f"Twilio's copy of recording {recording_sid} could not be deleted: {exc}")


async def forget_on_twilio(cfg: Config, recording_sid: str) -> None:
    """Delete a recording Oscar does not keep (its call was deleted) from Twilio."""
    if re.fullmatch(r"RE[0-9a-f]{32}", recording_sid or ""):
        await _forget_on_twilio(cfg, recording_sid)


async def _transcribe(
    cfg: Config, calls: CallManager, call_id: str, audio: np.ndarray, rate: int
) -> list[tuple[float, str, str]]:
    """Write down what each of the two said, a stretch at a time."""
    stretches = []
    for role, channel in zip(ROLES, audio, strict=False):
        for begin, end in await asyncio.to_thread(speech_stretches, channel, rate):
            stretches.append((role, begin, end, channel[begin:end].tobytes()))
    meter = CostMeter(
        lambda dollars: calls.add_cost(call_id, dollars),
        api_key=cfg.openrouter_api_key,
        base_url=cfg.openrouter_base_url,
        spawn=calls.spawn,
    )
    limit = asyncio.Semaphore(TRANSCRIBING_AT_ONCE)
    heard: list[tuple[float, float, str, str]] = []
    problems: list[str] = []
    async with new_client(timeout=FETCH_TIMEOUT) as client:

        async def one(role: str, begin: int, end: int, pcm: bytes) -> None:
            async with limit:
                try:
                    words = await write_down(client, cfg, meter, pcm, rate)
                except (PutThroughFailed, httpx.HTTPError) as exc:
                    problems.append(str(exc) or type(exc).__name__)
                    return
            if words:
                heard.append((begin / rate, end / rate, role, words))

        await asyncio.gather(*(one(*stretch) for stretch in stretches))
    if problems and not heard:
        raise PutThroughFailed(problems[0])
    if problems:
        calls.add_message(
            call_id,
            "note",
            f"Some of the call after it was put through could not be written down ({len(problems)} of "
            f"{len(stretches)} parts). It is all in the recording.",
        )
    return lines_from(heard)


async def _summarise_again(cfg: Config, calls: CallManager, call_id: str) -> None:
    """Write the summary again with the whole conversation (once the first is written)."""
    from app.summary import start_summary  # app.summary is imported by app.phone, which imports this

    def written() -> bool:
        call = calls.store.get_call(call_id)
        return call is None or call["summary_status"] != "writing"

    await _wait_for(written, WAIT_FOR_SUMMARY_SECONDS, every=2.0)
    call = calls.store.get_call(call_id)
    if call is None or call["summary_status"] == "writing":
        return
    start_summary(cfg, calls, call, calls.store.get_agent(), notify=False)
