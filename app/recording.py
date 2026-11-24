"""Each call's audio, both sides mixed into one track, kept to listen to in Call history.

During the call the audio goes to a raw file a couple of seconds at a time, so a
long call never sits in memory. When it ends the file is encoded as AAC in an
.m4a (about a quarter of a megabyte a minute), which every browser plays, and
the raw file goes. A raw file the server was cut off before encoding (it stopped
mid-call) is encoded the next time it looks (finish_leftovers).

Recordings live in data/call-recordings/, readable by the owner only, and go
after RECORDINGS_KEPT_DAYS, or with their call when it is deleted.
"""

import os
import re
import time
from pathlib import Path

import av
import numpy as np
from loguru import logger
from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor

from app.calls import CallClock

RECORDINGS_KEPT_DAYS = 90
# Speech in AAC at 32 kbit/s: clear on a phone line and in a browser alike.
RECORDING_BITRATE = 32000
# Audio is written out this often during a call.
WRITE_EVERY_SECONDS = 2
# A recording shorter than this (a call that never really started) is not kept.
SHORTEST_RECORDING_SECONDS = 1.0
_RAW_NAME = re.compile(r"\.(?P<call_id>[0-9a-f]{32})\.(?P<rate>\d{4,6})\.pcm")


def recordings_folder(db_path: Path) -> Path:
    return db_path.parent / "call-recordings"


def recording_path(db_path: Path, call_id: str) -> Path:
    return recordings_folder(db_path) / f"{call_id}.m4a"


def forget_recording(db_path: Path, call_id: str) -> None:
    """Delete a call's recording: the .m4a, and a raw or half-encoded one still to finish
    (an encoding under way that ends after this is deleted by whoever finishes it, when
    the call is no longer there)."""
    folder = recordings_folder(db_path)
    if not re.fullmatch(r"[0-9a-f]{32}", call_id or ""):
        return
    for path in (
        folder / f"{call_id}.m4a",
        *folder.glob(f".{call_id}.*.partial"),  # also the part put through, being kept (app/put_through.py)
        *folder.glob(f".{call_id}.*.pcm"),
    ):
        path.unlink(missing_ok=True)


def encode(raw: Path, target: Path, sample_rate: int) -> float:
    """Encode 16-bit mono PCM as AAC in an .m4a. Returns its length in seconds."""
    partial = target.with_name(f".{target.name}.partial")
    samples_written = 0
    with av.open(str(partial), "w", format="ipod") as out:
        stream = out.add_stream("aac", rate=sample_rate, layout="mono")
        stream.bit_rate = RECORDING_BITRATE
        with raw.open("rb") as source:
            while chunk := source.read(sample_rate * 2 * 10):
                samples = np.frombuffer(chunk[: len(chunk) & ~1], dtype=np.int16).reshape(1, -1)
                frame = av.AudioFrame.from_ndarray(samples, format="s16", layout="mono")
                frame.sample_rate = sample_rate
                frame.pts = samples_written
                samples_written += samples.shape[1]
                for packet in stream.encode(frame):
                    out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)
    os.chmod(partial, 0o600)  # what callers said: owner-only, as the database itself
    partial.replace(target)
    return samples_written / sample_rate


class CallRecorder:
    """Records one call: put `processor` in the pipeline after the audio output."""

    def __init__(self, folder: Path, call_id: str, sample_rate: int, clock: CallClock | None = None):
        self._folder = folder
        self._call_id = call_id
        self._rate = sample_rate
        self._raw = folder / f".{call_id}.{sample_rate}.pcm"
        self._file = None
        # Told how far the recording has got with each write, to place transcript lines in it.
        self._clock = clock
        self._written = 0
        self.processor = AudioBufferProcessor(
            sample_rate=sample_rate,
            num_channels=1,
            buffer_size=sample_rate * 2 * WRITE_EVERY_SECONDS,
            auto_start_recording=True,
        )

        @self.processor.event_handler("on_audio_data")
        async def on_audio_data(processor, audio: bytes, sample_rate: int, num_channels: int):
            self._write(audio)

    def _write(self, audio: bytes) -> None:
        if not audio:
            return
        if self._file is None:
            self._folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(self._raw, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            self._file = os.fdopen(descriptor, "ab")
        self._file.write(audio)
        self._written += len(audio)
        if self._clock is not None:
            self._clock.reached(time.time(), self._written / (self._rate * 2))

    def finish(self) -> float:
        """Encode what was recorded (run it on a worker thread). Returns the seconds kept, 0 for none."""
        if self._file is not None:
            self._file.close()
            self._file = None
        return finish_raw(self._raw, self.target, self._rate)

    @property
    def target(self) -> Path:
        return self._folder / f"{self._call_id}.m4a"

    def discard(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
        self._raw.unlink(missing_ok=True)


def finish_raw(raw: Path, target: Path, sample_rate: int) -> float:
    """Encode a raw recording and remove it. Returns the seconds kept (0 when too short to keep)."""
    try:
        size = raw.stat().st_size
    except FileNotFoundError:
        return 0.0
    try:
        if size / (sample_rate * 2) < SHORTEST_RECORDING_SECONDS:
            return 0.0
        return encode(raw, target, sample_rate)
    finally:
        raw.unlink(missing_ok=True)


def finish_leftover(folder: Path, call_id: str) -> float | None:
    """Encode one call's raw recording left by a server that stopped mid-call. Returns the
    seconds kept, or None when it left none."""
    for raw in folder.glob(f".{call_id}.*.pcm"):
        match = _RAW_NAME.fullmatch(raw.name)
        if match and match["call_id"] == call_id:
            return finish_raw(raw, folder / f"{call_id}.m4a", int(match["rate"]))
    return None


def finish_leftovers(folder: Path, is_open) -> dict[str, float]:
    """Encode raw recordings left by a server that stopped mid-call. `is_open(call_id)` says
    whether the call is still going (its recording is not left over). Returns {call_id: seconds}."""
    done: dict[str, float] = {}
    if not folder.is_dir():
        return done
    for raw in folder.glob(".*.pcm"):
        match = _RAW_NAME.fullmatch(raw.name)
        if not match:
            continue
        call_id = match["call_id"]
        if is_open(call_id):
            continue
        try:
            done[call_id] = finish_raw(raw, folder / f"{call_id}.m4a", int(match["rate"]))
        except Exception:
            logger.exception(f"Could not finish the recording of call {call_id}")
            raw.unlink(missing_ok=True)
    return done


def prune(folder: Path, *, keep_days: int = RECORDINGS_KEPT_DAYS, now: float | None = None) -> list[str]:
    """Delete recordings older than `keep_days`. Returns the ids of the calls whose recording went."""
    if not folder.is_dir():
        return []
    now = now or time.time()
    oldest = now - keep_days * 24 * 60 * 60
    # Half-encoded files left by a server stopped part-way through encoding.
    for partial in folder.glob(".*.partial"):
        try:
            if partial.stat().st_mtime < now - 60 * 60:
                partial.unlink()
        except FileNotFoundError:
            continue
    gone = []
    for path in folder.glob("*.m4a"):
        try:
            if path.stat().st_mtime < oldest:
                path.unlink()
                gone.append(path.stem)
        except FileNotFoundError:
            continue
    return gone
