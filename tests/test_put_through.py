"""Keeping the part of a call after Oscar put the caller through (app/put_through.py)."""

import base64
import io
import wave

import av
import httpx
import numpy as np
import pytest

from app import put_through
from app.calls import CallManager
from app.events import EventBus
from app.put_through import (
    keep_put_through,
    lines_from,
    read_audio,
    recording_url_ok,
    speech_stretches,
)
from app.recording import encode, recording_path, recordings_folder
from app.store import Store
from tests.audio_client import CALLER_CLIP
from tests.conftest import make_config

ACCOUNT = "AC" + "0" * 32  # make_config's
SID = "RE" + "1" * 32
URL = f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT}/Recordings/{SID}"
PERSON_SAID = "Hello, this is Jane. How can I help?"
CALLER_SAID = "Hi Jane, it's about my order."


def clip_8k() -> np.ndarray:
    """The caller clip the call tests use (real speech, 2.5 s), at Twilio's 8 kHz."""
    return read_audio(CALLER_CLIP, 8000, 1)[0]


def two_channel_wav(caller: np.ndarray, person: np.ndarray, rate: int = 8000) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(np.stack([caller, person], axis=1).astype(np.int16).tobytes())
    return out.getvalue()


def placed(seconds: float, clip: np.ndarray, length: float, volume: float = 1.0, rate: int = 8000) -> np.ndarray:
    track = np.zeros(int(length * rate), dtype=np.int16)
    start = int(seconds * rate)
    track[start : start + clip.size] = (clip * volume).astype(np.int16)
    return track


class FakeRecordings:
    def __init__(self):
        self.deleted = []

    def __call__(self, sid):
        recordings = self

        class One:
            def delete(self):
                recordings.deleted.append(sid)
                return True

        return One()


class FakeTwilio:
    def __init__(self):
        self.recordings = FakeRecordings()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    store = Store(cfg.db_path)
    calls = CallManager(store, EventBus())
    call = calls.create("phone_inbound", status="ended", from_number="+14155550100")
    calls.update(call["id"], transferred_to="+61412345678", summary_status="ready")
    twilio = FakeTwilio()
    monkeypatch.setattr(put_through, "_twilio_client", lambda cfg: twilio)
    summaries = []
    monkeypatch.setattr("app.summary.start_summary", lambda cfg, calls, call, agent, notify=False: summaries.append(notify))
    monkeypatch.setattr(put_through, "FETCH_AGAIN_AFTER_SECONDS", 0)

    # The loud side is the person put through to, the quiet one the caller (the clip twice).
    async def write_down(client, cfg, meter, pcm, rate):
        loudest = np.abs(np.frombuffer(pcm, dtype=np.int16)).max()
        return PERSON_SAID if loudest > 12000 else CALLER_SAID

    monkeypatch.setattr(put_through, "write_down", write_down)
    yield cfg, store, calls, call, twilio, summaries
    store.close()


def oscars_part(cfg, call_id: str, seconds: float = 10.0) -> None:
    folder = recordings_folder(cfg.db_path)
    folder.mkdir(exist_ok=True)
    raw = folder / "oscar.pcm"
    t = np.arange(int(seconds * 8000)) / 8000
    raw.write_bytes((np.sin(2 * np.pi * 300 * t) * 3000).astype(np.int16).tobytes())
    encode(raw, recording_path(cfg.db_path, call_id), 8000)
    raw.unlink()


def twilio_serving(body: bytes, asked: list):
    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        return httpx.Response(200, content=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def length_of(path) -> float:
    with av.open(str(path)) as played:
        return float(played.duration / av.time_base)


async def test_the_part_put_through_is_added_to_the_recording_and_written_down(setup):
    cfg, store, calls, call, twilio, summaries = setup
    oscars_part(cfg, call["id"], 10.0)
    clip = clip_8k()
    # Twilio's recording: the person answers and speaks at 0.5 s, the caller at 4 s.
    wav = two_channel_wav(caller=placed(4.0, clip, 8.0, volume=0.3), person=placed(0.5, clip, 8.0))
    asked: list[httpx.Request] = []
    async with twilio_serving(wav, asked) as client:
        await keep_put_through(cfg, calls, call["id"], SID, URL, client=client)

    # Fetched as a two-channel WAV with the account's keys, then deleted from Twilio.
    (request,) = asked
    assert str(request.url) == URL + ".wav"
    assert request.headers["authorization"] == "Basic " + base64.b64encode(
        f"{ACCOUNT}:{cfg.twilio_auth_token}".encode()
    ).decode()
    assert twilio.recordings.deleted == [SID]

    # The whole call plays as one: Oscar's part, a moment's silence, then the part put through.
    done = store.get_call(call["id"])
    assert done["recording_seconds"] == pytest.approx(19.0, abs=0.3)
    assert length_of(recording_path(cfg.db_path, call["id"])) == pytest.approx(19.0, abs=0.3)

    # What each said, in order, each line placed in the recording.
    lines = [(m["role"], m["text"], m["at_seconds"]) for m in store.get_messages(call["id"])]
    (person, caller) = lines
    assert person[:2] == ("person", PERSON_SAID)
    assert person[2] == pytest.approx(11.0 + 0.5, abs=0.5)
    assert caller[:2] == ("user", CALLER_SAID)
    assert caller[2] == pytest.approx(11.0 + 4.0, abs=0.5)

    # The summary is written again with the whole conversation, and not sent again.
    assert summaries == [False]
    # Nothing is left behind in the recordings folder.
    assert sorted(p.name for p in recordings_folder(cfg.db_path).iterdir()) == [f"{call['id']}.m4a"]


async def test_twilio_is_asked_once_more_for_a_recording_not_ready_yet(setup):
    cfg, store, calls, call, twilio, _ = setup
    oscars_part(cfg, call["id"], 3.0)
    wav = two_channel_wav(caller=placed(0.5, clip_8k(), 4.0, volume=0.3), person=np.zeros(32000, dtype=np.int16))
    answers = [httpx.Response(404), httpx.Response(200, content=wav)]
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: answers.pop(0)))
    async with client:
        await keep_put_through(cfg, calls, call["id"], SID, URL, client=client)
    assert [(m["role"], m["text"]) for m in store.get_messages(call["id"])] == [("user", CALLER_SAID)]


async def test_a_recording_twilio_will_not_hand_over_is_left_there(setup):
    cfg, store, calls, call, twilio, summaries = setup
    oscars_part(cfg, call["id"], 3.0)
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(401)))
    async with client:
        await keep_put_through(cfg, calls, call["id"], SID, URL, client=client)
    (note,) = store.get_messages(call["id"])
    assert note["role"] == "note"
    assert "Twilio answered 401" in note["text"]
    assert "Twilio still has its recording of it" in note["text"]
    assert twilio.recordings.deleted == []
    assert store.get_call(call["id"])["recording_seconds"] == 0  # Oscar's part, as it was
    assert length_of(recording_path(cfg.db_path, call["id"])) == pytest.approx(3.0, abs=0.2)
    assert summaries == []


async def test_oscar_sends_its_twilio_keys_only_to_twilio(setup):
    cfg, store, calls, call, twilio, _ = setup
    asked = []
    async with twilio_serving(b"", asked) as client:
        await keep_put_through(cfg, calls, call["id"], SID, "https://evil.example.test/rec", client=client)
    assert asked == []
    (note,) = store.get_messages(call["id"])
    assert "does not fetch from" in note["text"]


@pytest.mark.parametrize(
    "url, ok",
    [
        (URL, True),
        (URL + ".wav", True),
        (f"https://api.dublin.ie1.twilio.com/2010-04-01/Accounts/{ACCOUNT}/Recordings/{SID}", False),
        (f"https://api.ie1.twilio.com/2010-04-01/Accounts/{ACCOUNT}/Recordings/{SID}", True),
        (URL.replace("https://", "http://"), False),
        (URL.replace("api.twilio.com", "api.twilio.com.evil.test"), False),
        (URL.replace("api.twilio.com", "api.twilio.com:8443"), False),
        (URL.replace(ACCOUNT, "AC" + "9" * 32), False),  # another account's
        (URL.replace(SID, "RE" + "2" * 32), False),  # another recording
        (URL + "?x=1", False),
        (URL.replace("https://", "https://someone@"), False),
    ],
)
def test_only_this_accounts_recording_on_twilio_is_fetched(tmp_path, url, ok):
    assert recording_url_ok(make_config(tmp_path), url, SID) is ok


async def test_a_one_track_recording_is_kept_to_listen_to(setup):
    cfg, store, calls, call, twilio, summaries = setup
    oscars_part(cfg, call["id"], 3.0)
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(placed(0.5, clip_8k(), 4.0).tobytes())
    async with twilio_serving(out.getvalue(), []) as client:
        await keep_put_through(cfg, calls, call["id"], SID, URL, client=client)
    assert store.get_call(call["id"])["recording_seconds"] == pytest.approx(8.0, abs=0.3)
    (note,) = store.get_messages(call["id"])
    assert note["role"] == "note" and "one track" in note["text"]
    assert note["at_seconds"] == pytest.approx(4.0, abs=0.2)


async def test_a_call_deleted_meanwhile_keeps_nothing(setup):
    cfg, store, calls, call, twilio, _ = setup
    oscars_part(cfg, call["id"], 3.0)
    wav = two_channel_wav(caller=placed(0.5, clip_8k(), 4.0), person=np.zeros(32000, dtype=np.int16))

    def handler(request):
        store.delete_call(call["id"])  # deleted while Twilio's recording comes in
        recording_path(cfg.db_path, call["id"]).unlink()
        return httpx.Response(200, content=wav)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await keep_put_through(cfg, calls, call["id"], SID, URL, client=client)
    assert list(recordings_folder(cfg.db_path).iterdir()) == []
    assert twilio.recordings.deleted == [SID]


def test_speech_is_found_where_it_is_said():
    clip = clip_8k()
    track = np.concatenate([placed(1.0, clip, 5.0), placed(1.5, clip, 6.0)])  # at 1 s and at 6.5 s
    stretches = [(start / 8000, end / 8000) for start, end in speech_stretches(track, 8000)]
    assert len(stretches) == 2
    (first_start, first_end), (second_start, second_end) = stretches
    assert first_start == pytest.approx(1.0, abs=0.4) and first_end == pytest.approx(3.5, abs=0.5)
    assert second_start == pytest.approx(6.5, abs=0.4) and second_end == pytest.approx(9.0, abs=0.5)
    assert speech_stretches(np.zeros(40000, dtype=np.int16), 8000) == []


def test_a_long_stretch_is_written_down_in_parts(monkeypatch):
    monkeypatch.setattr(put_through, "LONGEST_STRETCH_SECONDS", 1.0)
    stretches = speech_stretches(placed(0.5, clip_8k(), 4.0), 8000)
    assert len(stretches) >= 3
    assert all(end - start <= 8000 for start, end in stretches)


def test_lines_read_in_the_order_they_were_said():
    heard = [
        (5.0, 6.0, "user", "Is it in stock?"),
        (0.5, 1.5, "person", "Hello, Jane speaking."),
        (2.0, 3.0, "person", "How can I help?"),  # Jane again, a moment later: the same line
        (3.5, 4.5, "user", "Hi Jane."),
        (9.0, 10.0, "person", "Yes, it is."),
    ]
    assert lines_from(heard) == [
        (0.5, "person", "Hello, Jane speaking. How can I help?"),
        (3.5, "user", "Hi Jane. Is it in stock?"),
        (9.0, "person", "Yes, it is."),
    ]


async def test_part_written_down_is_kept_and_the_rest_explained(setup, monkeypatch):
    cfg, store, calls, call, twilio, _ = setup
    oscars_part(cfg, call["id"], 3.0)
    clip = clip_8k()
    wav = two_channel_wav(caller=placed(4.0, clip, 8.0, volume=0.3), person=placed(0.5, clip, 8.0))

    async def write_down(client, cfg, meter, pcm, rate):
        if np.abs(np.frombuffer(pcm, dtype=np.int16)).max() > 12000:
            raise put_through.PutThroughFailed("It could not be written down: OpenRouter is busy.")
        return CALLER_SAID

    monkeypatch.setattr(put_through, "write_down", write_down)
    async with twilio_serving(wav, []) as client:
        await keep_put_through(cfg, calls, call["id"], SID, URL, client=client)
    said = [(m["role"], m["text"]) for m in store.get_messages(call["id"])]
    assert said[0][0] == "note" and "1 of 2 parts" in said[0][1]
    assert said[1] == ("user", CALLER_SAID)
