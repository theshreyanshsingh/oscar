"""Call recordings (app/recording.py). A whole call's recording is tested in test_call_flow.py."""

import os
import time

import av
import numpy as np
import pytest

from app.recording import encode, finish_leftovers, prune


def tone_pcm(seconds: float, rate: int) -> bytes:
    t = np.arange(int(seconds * rate)) / rate
    return (np.sin(2 * np.pi * 300 * t) * 8000).astype(np.int16).tobytes()


@pytest.mark.parametrize("rate", [8000, 16000])
def test_a_recording_is_small_and_plays_for_as_long_as_the_call(tmp_path, rate):
    raw = tmp_path / "call.pcm"
    raw.write_bytes(tone_pcm(30, rate))
    target = tmp_path / "call.m4a"
    assert encode(raw, target, rate) == pytest.approx(30)
    with av.open(str(target)) as played:
        assert float(played.duration / av.time_base) == pytest.approx(30, abs=0.1)
        assert played.streams.audio[0].codec_context.name == "aac"
    assert target.stat().st_size < 160_000  # about a quarter of a megabyte a minute
    assert oct(target.stat().st_mode & 0o777) == "0o600"


def test_a_recording_left_by_a_stopped_server_is_finished_later(tmp_path):
    call_id = "a" * 32
    other = "b" * 32
    (tmp_path / f".{call_id}.8000.pcm").write_bytes(tone_pcm(3, 8000))
    (tmp_path / f".{other}.8000.pcm").write_bytes(tone_pcm(3, 8000))
    (tmp_path / f".{'c' * 32}.8000.pcm").write_bytes(tone_pcm(0.2, 8000))  # too short to keep
    done = finish_leftovers(tmp_path, is_open=lambda cid: cid == other)
    assert done == {call_id: pytest.approx(3), "c" * 32: 0.0}
    assert (tmp_path / f"{call_id}.m4a").is_file()
    assert (tmp_path / f".{other}.8000.pcm").is_file()  # still being recorded
    assert not (tmp_path / f".{'c' * 32}.8000.pcm").exists()


def test_old_recordings_go(tmp_path):
    old, new = tmp_path / ("a" * 32 + ".m4a"), tmp_path / ("b" * 32 + ".m4a")
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    long_ago = time.time() - 91 * 24 * 60 * 60
    os.utime(old, (long_ago, long_ago))
    assert prune(tmp_path) == ["a" * 32]
    assert not old.exists() and new.exists()


def test_a_half_encoded_file_left_by_a_stopped_server_goes(tmp_path):
    partial = tmp_path / ".abc.m4a.partial"
    partial.write_bytes(b"x")
    two_hours_ago = time.time() - 2 * 60 * 60
    os.utime(partial, (two_hours_ago, two_hours_ago))
    fresh = tmp_path / ".def.m4a.partial"
    fresh.write_bytes(b"x")  # being encoded now
    prune(tmp_path)
    assert not partial.exists() and fresh.exists()
