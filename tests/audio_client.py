"""A scripted caller for browser calls: plays a recorded clip and listens to the agent."""

import asyncio
import fractions
import time
import wave
from pathlib import Path

import numpy as np
from aiortc import MediaStreamTrack, RTCPeerConnection
from av import AudioFrame

CALLER_CLIP = Path(__file__).parent / "fixtures" / "caller_16k.wav"
# "Can you search the internet for today's weather in Kanpur, India?" (the
# first thing Oscar's owner asked it to look up). Only real calls use it.
WEATHER_CLIP = Path(__file__).parent / "fixtures" / "caller_weather_24k.wav"
LOUD = 1000  # peak sample value that counts as "someone is speaking"


def read_wav(path: Path) -> tuple[bytes, int]:
    with wave.open(str(path)) as wav:
        assert wav.getnchannels() == 1 and wav.getsampwidth() == 2
        return wav.readframes(wav.getnframes()), wav.getframerate()


class ScriptedMicrophone(MediaStreamTrack):
    """Plays silence, then the caller's clip, then silence, in real time."""

    kind = "audio"

    def __init__(self, pcm: bytes, rate: int, lead_silence: float):
        super().__init__()
        self._pcm = b"\x00\x00" * int(rate * lead_silence) + pcm
        self._rate = rate
        self._samples = rate // 50  # 20 ms frames
        self._position = 0
        self._pts = 0
        self._start: float | None = None
        self.clip_ends_at: float | None = None  # monotonic time the clip finishes

    async def recv(self) -> AudioFrame:
        if self._start is None:
            self._start = time.monotonic()
            self.clip_ends_at = self._start + len(self._pcm) / 2 / self._rate
        else:
            self._pts += self._samples
            delay = self._start + self._pts / self._rate - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
        size = self._samples * 2
        chunk = self._pcm[self._position : self._position + size].ljust(size, b"\x00")
        self._position += size
        frame = AudioFrame(format="s16", layout="mono", samples=self._samples)
        frame.planes[0].update(chunk)
        frame.pts = self._pts
        frame.sample_rate = self._rate
        frame.time_base = fractions.Fraction(1, self._rate)
        return frame


class Ear:
    """Counts audio frames from the agent that contain sound."""

    def __init__(self):
        self.loud_frames = 0
        self.loud_times: list[float] = []  # time.monotonic() of each frame with sound
        self.task: asyncio.Task | None = None

    def listen(self, track) -> None:
        self.task = asyncio.ensure_future(self._consume(track))

    async def _consume(self, track) -> None:
        try:
            while True:
                frame = await track.recv()
                if np.abs(frame.to_ndarray()).max() > LOUD:
                    self.loud_frames += 1
                    self.loud_times.append(time.monotonic())
        except Exception:
            pass  # the track ended


def make_caller(
    lead_silence: float, clip: Path = CALLER_CLIP
) -> tuple[RTCPeerConnection, ScriptedMicrophone, Ear]:
    """A peer connection set up the way the dashboard sets one up."""
    pcm, rate = read_wav(clip)
    microphone = ScriptedMicrophone(pcm, rate, lead_silence=lead_silence)
    peer = RTCPeerConnection()
    peer.addTransceiver(microphone, direction="sendrecv")
    peer.addTransceiver("video", direction="sendrecv")
    ear = Ear()
    peer.on("track", lambda track: ear.listen(track) if track.kind == "audio" else None)
    return peer, microphone, ear
