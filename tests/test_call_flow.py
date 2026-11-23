"""Whole calls, end to end, against a running server.

A recorded voice clip (tests/fixtures/caller_16k.wav) plays the caller. Nothing
in Oscar is faked: the WebRTC / Twilio media transport, Silero voice activity
detection, smart-turn end-of-turn detection, the voice service (app/speech.py),
context aggregation, transcript logging, live events and call bookkeeping all
run for real. Only OpenRouter is a stand-in (tests/fake_openrouter.py), which
Oscar talks to over HTTP exactly as it talks to the real one.
"""

import asyncio
import base64
import io
import json
import socket
import time
import wave
from types import SimpleNamespace

import av
import httpx
import numpy as np
import pytest
import uvicorn
import websockets
from aiortc import RTCSessionDescription
from pipecat.audio.utils import create_stream_resampler, pcm_to_ulaw, ulaw_to_pcm
from pipecat.serializers.twilio import TwilioFrameSerializer
from twilio.request_validator import RequestValidator

from app.agent import (
    NO_KNOWLEDGE_NOTE,
    NO_TOOLS_NOTE,
    SEARCH_NOTE,
    accent_reminder,
    line_instruction,
)
from app.main import create_app
from app.script import KEEPER_INSTRUCTIONS, KEEPER_MODEL, script_steps, step_note
from app.research import RESEARCH_INSTRUCTIONS, RESEARCH_MODEL
from app.search import READER_MODEL, ROUTER_MODEL
from app.speech import (
    FACTS_FAILED_LINE,
    HOLD_LINE,
    SEARCH_FAILED_LINE,
    TRANSCRIPT_MODEL,
    VOICE_MODEL,
    greeting_request,
    say_request,
)
from app.store import DEFAULT_AGENT
from app.summary import SUMMARY_INSTRUCTIONS, SUMMARY_MODEL, transcript_text
from tests.audio_client import CALLER_CLIP, LOUD, make_caller, read_wav
from tests.conftest import PUBLIC_URL, TWILIO_AUTH_TOKEN, make_config
from tests.fake_openrouter import NO_FOLLOW_UP, create_fake_firecrawl, create_fake_openrouter
from tests.test_phone import INBOUND, stream_parameters

# The recorded caller says "Hi there", pauses, then asks the question.
HEARD_FIRST = "Hi there."
HEARD = "What time do you open tomorrow?"
REPLY = "We open at nine in the morning."
API_KEY = "sk-or-test-key"


async def wait_until(check, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = await check()
        if result:
            return result
        await asyncio.sleep(0.2)
    raise AssertionError(f"Timed out after {timeout}s waiting for {what}")


async def serve(app) -> tuple[int, uvicorn.Server, asyncio.Task]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    await wait_until(lambda: asyncio.sleep(0, server.started), 15, "a test server to start")
    return port, server, task


async def shut_down(*servers: tuple[int, uvicorn.Server, asyncio.Task]) -> None:
    for _, server, task in servers:
        server.should_exit = True
        await asyncio.wait_for(task, 30)


@pytest.fixture
async def server(tmp_path, request):
    # The stand-in takes half a second to start answering. The real model takes
    # over a second, and that gap matters: it is when a caller who has only
    # paused carries on talking.
    openrouter = create_fake_openrouter(
        heard=HEARD,
        heard_in_brief_clips=HEARD_FIRST,
        reply=REPLY,
        api_key=API_KEY,
        answer_delay=0.5,
        # The step of a script the model keeping Oscar's place names ("2" unless a test says otherwise).
        step=getattr(request, "param", "2"),
    )
    openrouter_server = await serve(openrouter)
    cfg = make_config(
        tmp_path,
        openrouter_api_key=API_KEY,
        openrouter_base_url=f"http://127.0.0.1:{openrouter_server[0]}/api/v1",
    )
    app = create_app(cfg)
    oscar_server = await serve(app)
    port = oscar_server[0]

    yield SimpleNamespace(
        base=f"http://127.0.0.1:{port}", port=port, app=app, openrouter=openrouter
    )

    await shut_down(oscar_server, openrouter_server)


def sent(server, endpoint: str) -> list[dict]:
    """What Oscar sent to one of OpenRouter's endpoints, in order."""
    return [r["body"] for r in server.openrouter.state.requests if r["endpoint"] == endpoint]


def clip_of(message: dict) -> tuple[int, int, float]:
    """(sample rate, channels, seconds) of the caller audio in a chat message."""
    (part,) = message["content"]
    assert part["type"] == "input_audio" and part["input_audio"]["format"] == "wav"
    with wave.open(io.BytesIO(base64.b64decode(part["input_audio"]["data"]))) as clip:
        return clip.getframerate(), clip.getnchannels(), clip.getnframes() / clip.getframerate()


class EventRecorder:
    """Reads the dashboard's live event stream in the background."""

    def __init__(self, http: httpx.AsyncClient):
        self.events: list[dict] = []
        self._http = http
        self._task = asyncio.create_task(self._read())

    async def _read(self) -> None:
        try:
            async with self._http.stream("GET", "/api/events") as response:
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        self.events.append(json.loads(line[6:]))
        except httpx.HTTPError:
            pass  # the server shut down

    def call_statuses(self, call_id: str) -> list[str]:
        return [
            e["call"]["status"]
            for e in self.events
            if e["type"] == "call" and e["call"]["id"] == call_id
        ]

    def messages(self, call_id: str) -> list[tuple[str, str]]:
        return [
            (e["message"]["role"], e["message"]["text"])
            for e in self.events
            if e["type"] == "message" and e["message"]["call_id"] == call_id
        ]

    async def stop(self) -> None:
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass


def question_line(messages: list[tuple[str, str]]) -> int | None:
    """Where in a transcript the caller's question is (alone, or after "Hi there.")."""
    for index, (role, text) in enumerate(messages):
        if role == "user" and text.endswith(HEARD):
            return index
    return None


def has_full_exchange(messages: list[tuple[str, str]]) -> bool:
    return question_line(messages) is not None and ("agent", REPLY) in messages


async def start_browser_call(http: httpx.AsyncClient, peer, **options) -> str:
    await peer.setLocalDescription(await peer.createOffer())
    response = await http.post(
        "/api/calls/browser",
        json={"sdp": peer.localDescription.sdp, "type": "offer", **options},
    )
    assert response.status_code == 200, response.text
    answer = response.json()
    await peer.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]))
    return answer["call_id"]


async def test_browser_call_listens_and_speaks(server):
    peer, _, ear = make_caller(lead_silence=3.5)

    async with httpx.AsyncClient(base_url=server.base, timeout=30) as http:
        recorder = EventRecorder(http)
        await asyncio.sleep(0.3)  # let the event stream connect
        call_id = await start_browser_call(http, peer)

        async def exchange_done():
            detail = (await http.get(f"/api/calls/{call_id}")).json()
            messages = [(m["role"], m["text"]) for m in detail["messages"]]
            return detail if has_full_exchange(messages) else None

        detail = await wait_until(exchange_done, 45, "the agent to hear the caller and reply")
        assert detail["call"]["kind"] == "browser"
        assert detail["call"]["status"] == "live"

        assert detail["call"]["error"] == ""
        assert (await http.get("/api/status")).json()["agent"] == {"ready": True, "missing": []}

        # It spoke: the greeting and the reply reached the caller as sound.
        await wait_until(lambda: asyncio.sleep(0, ear.loud_frames > 50), 10, "agent audio")

        # The transcript reads in the order things were said: greeting, caller, reply.
        # The caller's two parts were answered once, as one thing.
        said = [(m["role"], m["text"]) for m in detail["messages"]]
        assert said == [
            ("agent", DEFAULT_AGENT.greeting),
            ("user", f"{HEARD_FIRST} {HEARD}"),
            ("agent", REPLY),
        ]

        # The greeting was asked for word for word, with Oscar's instructions.
        asked = sent(server, "chat")
        assert all(
            (body["model"], body["stream"], body["modalities"], body["audio"])
            == (VOICE_MODEL, True, ["text", "audio"], {"voice": DEFAULT_AGENT.voice, "format": "pcm16"})
            for body in asked
        )
        system, sound, opening = asked[0]["messages"]
        assert system["role"] == "system" and system["content"].startswith("Your name is Oscar.")
        assert DEFAULT_AGENT.system_prompt in system["content"]
        assert system["content"].endswith(NO_TOOLS_NOTE)  # it must not pretend to look things up
        assert opening == {"role": "user", "content": greeting_request(DEFAULT_AGENT.greeting)}
        # Out of the box Oscar is a man with an Indian accent. The accent is
        # asked for in the instructions and again just before every turn.
        assert asked[0]["audio"]["voice"] == "cedar"
        assert "Indian accent" in system["content"]
        assert sound == {"role": "system", "content": accent_reminder(DEFAULT_AGENT)}
        assert "Indian accent" in sound["content"]

        # It heard: the model got the caller's own voice, as one clip holding
        # everything they said (the recorded caller pauses after "Hi there"),
        # after the conversation so far as text.
        system, *history, sound_again, turn = asked[-1]["messages"]
        assert system == asked[0]["messages"][0]
        assert sound_again == sound
        assert history[0] == {"role": "assistant", "content": DEFAULT_AGENT.greeting}
        assert all(isinstance(message["content"], str) for message in history)
        assert turn["role"] == "user"
        rate, channels, seconds = clip_of(turn)
        assert (rate, channels) == (16000, 1)  # the browser call's own sample rate
        assert seconds > 2.5  # "Hi there. What time do you open tomorrow?" in full

        # The caller's words were also written down, for the transcript.
        written = sent(server, "transcriptions")
        assert written and all(set(body) == {"model", "language", "input_audio", "usage"} for body in written)
        assert all(body["model"] == TRANSCRIPT_MODEL for body in written)
        assert all(body["language"] == "multi" for body in written)  # Hindi too, not English alone

        # Ending from the dashboard stops the call.
        assert (await http.post(f"/api/calls/{call_id}/end")).json() == {"ended": True}

        async def call_ended():
            call = (await http.get(f"/api/calls/{call_id}")).json()["call"]
            return call if call["status"] == "ended" else None

        call = await wait_until(call_ended, 15, "the call to end")
        assert call["error"] == ""
        assert call["duration_seconds"] >= 5
        assert (await http.get("/api/status")).json()["active_call_ids"] == []

        # The dashboard saw it all live.
        await asyncio.sleep(0.3)
        statuses = recorder.call_statuses(call_id)
        assert statuses[0] == "connecting" and "live" in statuses and statuses[-1] == "ended"
        assert has_full_exchange(recorder.messages(call_id))
        await recorder.stop()

    await peer.close()


async def test_oscar_finishes_speaking_before_it_listens(server):
    # The default, for callers on loudspeakers. Sound that arrives while Oscar
    # is talking is not the caller's turn: it may be Oscar's own voice coming
    # back through their microphone. Here the caller talks over the greeting.
    peer, _, _ = make_caller(lead_silence=1.7)
    greeting = DEFAULT_AGENT.greeting

    async with httpx.AsyncClient(base_url=server.base, timeout=30) as http:
        call_id = await start_browser_call(http, peer)

        async def exchange_done():
            detail = (await http.get(f"/api/calls/{call_id}")).json()
            messages = [(m["role"], m["text"]) for m in detail["messages"]]
            return detail if has_full_exchange(messages) else None

        detail = await wait_until(exchange_done, 45, "the agent to answer once it has finished")
        said = [(m["role"], m["text"]) for m in detail["messages"]]
        # The greeting was said to the end, and nothing was cut off.
        assert said[0] == ("agent", greeting)
        assert not any(text.endswith("[interrupted]") for _, text in said)
        # Oscar heard only what the caller said after it had finished speaking.
        heard = [clip_of(body["messages"][-1])[2] for body in sent(server, "chat")[1:]]
        assert heard and max(heard) < 2.2
        await http.post(f"/api/calls/{call_id}/end")

    await peer.close()


async def test_caller_on_headphones_can_interrupt_the_agent(server):
    # The caller starts talking 0.5 s in, as the 2 s greeting begins to play.
    peer, _, _ = make_caller(lead_silence=0.5)
    greeting = DEFAULT_AGENT.greeting

    async with httpx.AsyncClient(base_url=server.base, timeout=30) as http:
        call_id = await start_browser_call(http, peer, interruptible=True)

        async def exchange_done():
            detail = (await http.get(f"/api/calls/{call_id}")).json()
            messages = [(m["role"], m["text"]) for m in detail["messages"]]
            return detail if has_full_exchange(messages) else None

        detail = await wait_until(exchange_done, 45, "the agent to answer after the interruption")

        # The greeting started playing but was cut off: it never counts as fully
        # said, in the transcript or in what the model is told was said. Only
        # the words that had played by then are kept, marked as interrupted.
        said = [(m["role"], m["text"]) for m in detail["messages"]]
        assert ("agent", greeting) not in said
        for role, text in said[: question_line(said)]:
            if role == "user":
                assert text == HEARD_FIRST
                continue
            assert text.endswith(" [interrupted]")
            heard_part = text.removesuffix(" [interrupted]")
            assert greeting.startswith(heard_part) and len(heard_part) < len(greeting) / 2
        for body in sent(server, "chat")[1:]:
            _, *history, turn = body["messages"]
            assert {"role": "assistant", "content": greeting} not in history
            assert turn["role"] == "user" and clip_of(turn)[2] > 1.0
        # And the agent went on to answer the caller.
        assert said[-1] == ("agent", REPLY)
        await http.post(f"/api/calls/{call_id}/end")

    await peer.close()


async def test_browser_caller_hanging_up_ends_the_call(server):
    peer, _, _ = make_caller(lead_silence=1.0)

    async with httpx.AsyncClient(base_url=server.base, timeout=30) as http:
        call_id = await start_browser_call(http, peer)

        async def status_is(wanted):
            call = (await http.get(f"/api/calls/{call_id}")).json()["call"]
            return call if call["status"] == wanted else None

        await wait_until(lambda: status_is("live"), 15, "the call to go live")
        await peer.close()  # the caller closes the tab / connection
        call = await wait_until(lambda: status_is("ended"), 45, "the server to notice the hang-up")
        assert call["error"] == ""


async def test_a_call_is_summed_up_when_it_ends(server, monkeypatch):
    peer, _, _ = make_caller(lead_silence=3.5)
    # The owner has a webhook for summaries (app/notify.py).
    delivered: list[dict] = []

    async def deliver(url, payload, client=None):
        delivered.append({"url": url, **payload})
        return ""

    monkeypatch.setattr("app.summary.send_summary", deliver)
    server.app.state.store.save_summary_webhook_url("https://hooks.example.test/oscar")

    async with httpx.AsyncClient(base_url=server.base, timeout=30) as http:
        recorder = EventRecorder(http)
        await asyncio.sleep(0.3)  # let the event stream connect
        call_id = await start_browser_call(http, peer)

        async def exchange_done():
            detail = (await http.get(f"/api/calls/{call_id}")).json()
            messages = [(m["role"], m["text"]) for m in detail["messages"]]
            return detail if has_full_exchange(messages) else None

        detail = await wait_until(exchange_done, 45, "the agent to hear the caller and reply")
        # Nothing is summed up while the call is going.
        assert detail["call"]["summary_status"] == ""
        assert sent(server, "summary") == []
        assert detail["searches"] == []

        assert (await http.post(f"/api/calls/{call_id}/end")).json() == {"ended": True}

        async def summed_up():
            detail = (await http.get(f"/api/calls/{call_id}")).json()
            return detail if detail["call"]["summary_status"] == "ready" else None

        detail = await wait_until(summed_up, 15, "the call's summary")
        call = detail["call"]
        assert call["summary"] == json.loads(NO_FOLLOW_UP)["summary"]
        assert (call["caller_name"], call["callback_number"], call["follow_up"]) == ("", "", "")
        assert (call["status"], call["error"], call["summary_error"]) == ("ended", "", "")
        # Nothing was searched for on this call, so there is nothing to write up.
        assert (call["research"], call["research_error"], sent(server, "research")) == ([], "", [])

        # One request, once the call was over: the whole transcript, as text, to a text model.
        (asked,) = sent(server, "summary")
        assert asked["model"] == SUMMARY_MODEL and "modalities" not in asked
        system, transcript = asked["messages"]
        assert system == {"role": "system", "content": SUMMARY_INSTRUCTIONS.format(name="Oscar", details="")}
        assert transcript["role"] == "user"
        assert transcript["content"] == transcript_text(
            detail["messages"], kind="browser", agent_name="Oscar"
        )
        assert f"Caller: {HEARD_FIRST} {HEARD}\nOscar: {REPLY}" in transcript["content"]

        # The dashboard saw the call end, then its summary being written, then the summary.
        await asyncio.sleep(0.3)
        seen = [
            (e["call"]["status"], e["call"]["summary_status"])
            for e in recorder.events
            if e["type"] == "call" and e["call"]["id"] == call_id
        ]
        # (Its recording being saved is one more update, at the same stage.)
        stages = [stage for i, stage in enumerate(seen) if i == 0 or stage != seen[i - 1]]
        assert stages[-3:] == [("ended", ""), ("ended", "writing"), ("ended", "ready")]
        assert recorder.events[-1]["call"]["summary"] == call["summary"]
        await recorder.stop()

        # Both sides of the call were recorded, to listen to in Call history.
        async def recorded():
            call = (await http.get(f"/api/calls/{call_id}")).json()["call"]
            return call if call["recording_seconds"] > 0 else None

        call = await wait_until(recorded, 15, "the call's recording")
        audio = await http.get(f"/api/calls/{call_id}/recording")
        assert audio.status_code == 200 and audio.headers["content-type"] == "audio/mp4"
        with av.open(io.BytesIO(audio.content)) as played:
            seconds = float(played.duration / av.time_base)
            samples = np.concatenate([frame.to_ndarray().ravel() for frame in played.decode(audio=0)])
        assert seconds == pytest.approx(call["recording_seconds"], abs=0.5) and seconds > 5
        assert np.abs(samples).max() > 0.05  # sound, not silence: the caller and Oscar

        # And the summary was sent to the owner, once.
        ((sent_summary),) = delivered
        assert sent_summary["url"] == "https://hooks.example.test/oscar"
        assert sent_summary["call"]["id"] == call_id
        assert sent_summary["call"]["summary"] == call["summary"]

    await peer.close()


async def test_browser_call_works_through_the_owners_script(server):
    script = "1. Ask who is calling.\n2. Ask what they need help with.\n3. Thank them and say goodbye."
    steps = script_steps(script)
    peer, _, _ = make_caller(lead_silence=3.5)

    async with httpx.AsyncClient(base_url=server.base, timeout=30) as http:
        told = await http.put("/api/agent", json={**DEFAULT_AGENT.model_dump(), "script": script})
        assert told.status_code == 200
        call_id = await start_browser_call(http, peer)

        async def exchange_done():
            detail = (await http.get(f"/api/calls/{call_id}")).json()
            messages = [(m["role"], m["text"]) for m in detail["messages"]]
            return detail if has_full_exchange(messages) else None

        detail = await wait_until(exchange_done, 45, "the agent to hear the caller and reply")
        asked = sent(server, "chat")

        # The script is not part of the model's instructions, and the greeting is not a step of it.
        system, sound, opening = asked[0]["messages"]
        assert "who is calling" not in system["content"]
        assert sound == {"role": "system", "content": accent_reminder(DEFAULT_AGENT)}
        assert opening == {"role": "user", "content": greeting_request(DEFAULT_AGENT.greeting)}

        # Before the caller's turn the model is told the step it is on: how it sounds, then the step.
        *_, note, turn = asked[-1]["messages"]
        assert turn["role"] == "user" and clip_of(turn)[2] > 2.5
        assert note == {
            "role": "system",
            "content": f"{accent_reminder(DEFAULT_AGENT)} ({step_note(steps, 0)} Keep your Indian accent.)",
        }

        # Once Oscar has replied, a text model is asked where the script has got to,
        # with the whole call as text.
        kept = await wait_until(
            lambda: asyncio.sleep(0, sent(server, "script")), 10, "Oscar's place in its script to be asked for"
        )
        place = kept[-1]
        assert place["model"] == KEEPER_MODEL and "modalities" not in place
        rules, call_so_far = place["messages"]
        assert rules == {"role": "system", "content": KEEPER_INSTRUCTIONS.format(name="Oscar")}
        assert call_so_far["content"] == (
            "The script:\n"
            "1. Ask who is calling.\n"
            "2. Ask what they need help with.\n"
            "3. Thank them and say goodbye.\n"
            "\n"
            "The call so far:\n"
            f"Oscar: {DEFAULT_AGENT.greeting}\n"
            f"Caller: {HEARD_FIRST} {HEARD}\n"
            f"Oscar: {REPLY}"
        )

        assert (await http.post(f"/api/calls/{call_id}/end")).json() == {"ended": True}
        call = (await http.get(f"/api/calls/{call_id}")).json()["call"]
        assert call["error"] == ""

    await peer.close()


@pytest.mark.parametrize("server", ["They seem nearly done."], indirect=True)
async def test_a_place_in_the_script_that_cannot_be_worked_out_is_explained_on_the_call(server):
    peer, _, _ = make_caller(lead_silence=3.5)

    async with httpx.AsyncClient(base_url=server.base, timeout=30) as http:
        script = "Ask who is calling.\nSay goodbye."
        await http.put("/api/agent", json={**DEFAULT_AGENT.model_dump(), "script": script})
        call_id = await start_browser_call(http, peer)

        async def explained():
            call = (await http.get(f"/api/calls/{call_id}")).json()["call"]
            return call if call["error"] else None

        call = await wait_until(explained, 45, "the lost place to be explained")
        # The call carries on from the step Oscar was last known to be on.
        assert call["status"] == "live"
        assert call["error"] == (
            "Could not keep Oscar's place in its script: "
            "The model keeping Oscar's place answered 'They seem nearly done.'."
        )
        await http.post(f"/api/calls/{call_id}/end")

    await peer.close()


async def test_phone_call_listens_and_speaks(server, monkeypatch):
    hang_ups: list[str] = []

    async def record_hang_up(serializer):
        hang_ups.append(serializer._call_sid)

    monkeypatch.setattr(TwilioFrameSerializer, "_hang_up_call", record_hang_up)

    async with httpx.AsyncClient(base_url=server.base, timeout=30) as http:
        signature = RequestValidator(TWILIO_AUTH_TOKEN).compute_signature(
            PUBLIC_URL + "/twilio/voice", INBOUND
        )
        voice = await http.post(
            "/twilio/voice", data=INBOUND, headers={"X-Twilio-Signature": signature}
        )
        assert voice.status_code == 200
        _, params = stream_parameters(voice.text)
        call_id = params["call_id"]

        pcm, rate = read_wav(CALLER_CLIP)
        audio = b"\x00\x00" * int(rate * 3.5) + pcm + b"\x00\x00" * (rate * 15)
        caller_ulaw = await pcm_to_ulaw(audio, rate, 8000, create_stream_resampler())

        stream_sid = "MZ" + "5" * 32
        start = {
            "event": "start",
            "sequenceNumber": "1",
            "streamSid": stream_sid,
            "start": {
                "streamSid": stream_sid,
                "callSid": INBOUND["CallSid"],
                "accountSid": "AC" + "0" * 32,
                "customParameters": params,
                "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
            },
        }

        agent_audio = bytearray()
        async with websockets.connect(f"ws://127.0.0.1:{server.port}/twilio/media") as ws:
            await ws.send(json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}))
            await ws.send(json.dumps(start))

            async def receive():
                try:
                    async for raw in ws:
                        message = json.loads(raw)
                        if message.get("event") == "media":
                            assert message["streamSid"] == stream_sid
                            agent_audio.extend(base64.b64decode(message["media"]["payload"]))
                except websockets.ConnectionClosed:
                    pass

            receiver = asyncio.create_task(receive())

            store = server.app.state.store
            exchanged = False
            for chunk_number, offset in enumerate(range(0, len(caller_ulaw), 160)):
                await ws.send(
                    json.dumps(
                        {
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {
                                "track": "inbound",
                                "chunk": str(chunk_number),
                                "timestamp": str(chunk_number * 20),
                                "payload": base64.b64encode(
                                    caller_ulaw[offset : offset + 160]
                                ).decode(),
                            },
                        }
                    )
                )
                await asyncio.sleep(0.02)
                if chunk_number % 25 == 0:
                    messages = [(m["role"], m["text"]) for m in store.get_messages(call_id)]
                    if has_full_exchange(messages):
                        exchanged = True
                        break
            assert exchanged, store.get_messages(call_id)

            call = store.get_call(call_id)
            assert call["status"] == "live"
            assert call["from_number"] == INBOUND["From"]

            # The agent's voice came back down the phone line (8 kHz mu-law).
            await asyncio.sleep(1.0)
            agent_pcm = await ulaw_to_pcm(bytes(agent_audio), 8000, 8000, create_stream_resampler())
            samples = np.frombuffer(agent_pcm, dtype=np.int16)
            assert len(samples) > 8000 * 2, "expected at least two seconds of agent audio"
            assert np.abs(samples).max() > LOUD

            # The model heard the caller at the phone line's own 8 kHz.
            rate, channels, seconds = clip_of(sent(server, "chat")[-1]["messages"][-1])
            assert (rate, channels) == (8000, 1) and seconds > 2.5

            # Ending from the dashboard hangs up the phone call through Twilio.
            assert (await http.post(f"/api/calls/{call_id}/end")).json() == {"ended": True}
            await asyncio.wait_for(receiver, 15)

        async def call_ended():
            call = (await http.get(f"/api/calls/{call_id}")).json()["call"]
            return call if call["status"] == "ended" else None

        call = await wait_until(call_ended, 15, "the phone call to end")
        assert call["error"] == ""
        assert hang_ups == [INBOUND["CallSid"]]


async def test_a_wrong_key_shows_up_on_the_call(tmp_path):
    # OpenRouter rejects the key: Oscar cannot answer, and the call says why.
    openrouter = create_fake_openrouter(heard=HEARD, reply=REPLY, api_key="the-right-key")
    openrouter_server = await serve(openrouter)
    cfg = make_config(
        tmp_path,
        openrouter_api_key="a-wrong-key",
        openrouter_base_url=f"http://127.0.0.1:{openrouter_server[0]}/api/v1",
    )
    oscar_server = await serve(create_app(cfg))
    peer, _, _ = make_caller(lead_silence=1.0)
    try:
        base = f"http://127.0.0.1:{oscar_server[0]}"
        async with httpx.AsyncClient(base_url=base, timeout=30) as http:
            call_id = await start_browser_call(http, peer)

            async def has_error():
                call = (await http.get(f"/api/calls/{call_id}")).json()["call"]
                return call if call["error"] else None

            call = await wait_until(has_error, 20, "the key problem to be recorded")
            assert call["error"] == (
                "Oscar could not answer: OpenRouter rejected the API key "
                "(check OPENROUTER_API_KEY). No auth credentials found"
            )
            await http.post(f"/api/calls/{call_id}/end")
    finally:
        await peer.close()
        await shut_down(oscar_server, openrouter_server)


# Looking things up on the web

SEARCH_FOR = "opening hours of Bondi Bikes tomorrow"
FOUND = "Bondi Bikes opens at nine tomorrow morning."
PAGES = [
    {
        "title": "Bondi Bikes",
        "url": "https://bondibikes.example/hours",
        "description": "# Opening hours\nOpen **9am to 5pm** every day.",
        "position": 1,
    }
]
FIRECRAWL_KEY = "fc-test-key"
# What the model writing up the call's searches answers.
RESEARCH = {"notes": [{"text": "Bondi Bikes is open 9am to 5pm every day.", "pages": [1]}]}


@pytest.fixture
async def search_server(tmp_path, request):
    """Oscar with web search set up, in front of stand-ins for OpenRouter and Firecrawl.

    The stand-in decides that the caller's question needs a search (and that
    "Hi there", said first, does not).
    """
    openrouter = create_fake_openrouter(
        heard=HEARD,
        heard_in_brief_clips=HEARD_FIRST,
        reply=REPLY,
        api_key=API_KEY,
        answer_delay=0.5,
        decision=f"SEARCH\n{SEARCH_FOR}\n{HEARD}",
        found=FOUND,
        research=json.dumps(RESEARCH),
    )
    firecrawl = create_fake_firecrawl(api_key=FIRECRAWL_KEY, web=PAGES)
    openrouter_server, firecrawl_server = await serve(openrouter), await serve(firecrawl)
    cfg = make_config(
        tmp_path,
        openrouter_api_key=API_KEY,
        openrouter_base_url=f"http://127.0.0.1:{openrouter_server[0]}/api/v1",
        firecrawl_api_key=getattr(request, "param", FIRECRAWL_KEY),
        firecrawl_base_url=f"http://127.0.0.1:{firecrawl_server[0]}/v2",
    )
    oscar_server = await serve(create_app(cfg))

    yield SimpleNamespace(
        base=f"http://127.0.0.1:{oscar_server[0]}", openrouter=openrouter, firecrawl=firecrawl
    )

    await shut_down(oscar_server, openrouter_server, firecrawl_server)


async def searched_call(http, peer) -> dict:
    """Place a browser call and wait until Oscar has answered the caller's question."""
    call_id = await start_browser_call(http, peer)

    async def answered():
        detail = (await http.get(f"/api/calls/{call_id}")).json()
        roles = [m["role"] for m in detail["messages"]]
        return detail if "note" in roles and "agent" in roles[roles.index("note") :] else None

    detail = await wait_until(answered, 45, "Oscar to search and answer")
    await http.post(f"/api/calls/{call_id}/end")
    return detail


async def test_browser_call_looks_things_up_on_the_web(search_server):
    server = search_server
    peer, _, ear = make_caller(lead_silence=3.5)
    try:
        async with httpx.AsyncClient(base_url=server.base, timeout=30) as http:
            assert (await http.get("/api/status")).json()["search"] == {"enabled": True}
            detail = await searched_call(http, peer)
        await wait_until(lambda: asyncio.sleep(0, ear.loud_frames > 50), 10, "agent audio")
    finally:
        await peer.close()

    # The transcript: the question, a note of the search, and one reply holding
    # the "one moment" line and what was found. The model's own reply to the
    # question (REPLY) was never said.
    assert [(m["role"], m["text"]) for m in detail["messages"]] == [
        ("agent", DEFAULT_AGENT.greeting),
        ("user", f"{HEARD_FIRST} {HEARD}"),
        ("note", f"Searched the web for: {SEARCH_FOR}"),
        ("agent", f"{HOLD_LINE} {FOUND}"),
    ]
    assert detail["call"]["error"] == ""

    # Firecrawl was asked once, with Oscar's key.
    assert server.firecrawl.state.requests == [
        {"query": SEARCH_FOR, "limit": 5, "sources": ["web", "news"], "timeout": 10000}
    ]

    # The decision was asked of the model that hears, with the caller's voice
    # and what the agent already knows.
    decisions = sent(server, "decision")
    assert decisions and all(body["model"] == ROUTER_MODEL for body in decisions)
    system, *earlier, turn = decisions[-1]["messages"]
    assert DEFAULT_AGENT.system_prompt in system["content"]
    assert earlier[0] == {"role": "assistant", "content": DEFAULT_AGENT.greeting}
    assert clip_of(turn)[2] > 2.5  # the whole question

    # A text model read what the search found and wrote the answer, knowing
    # what had been said on the call before the question.
    (reading,) = sent(server, "reading")
    assert reading["model"] == READER_MODEL
    assert reading["messages"][1]["content"] == (
        f"The call so far:\nOscar: {DEFAULT_AGENT.greeting}\n\n"
        f"The caller's question now: {HEARD}\n\nSearched for: {SEARCH_FOR}\n\nSearch results:\n\n"
        "1. Bondi Bikes (https://bondibikes.example/hours)\nOpening hours Open 9am to 5pm every day."
    )

    # The speech model was never offered a tool or shown the results. Its own
    # replies run on the usual instructions, with a short note in place of
    # "you cannot look anything up".
    spoken = sent(server, "chat")
    assert all("tools" not in body and "9am to 5pm" not in json.dumps(body) for body in spoken)
    handed = {
        body["messages"][-1]["content"]: body["messages"]
        for body in spoken
        if isinstance(body["messages"][-1]["content"], str)
    }
    instructions = handed[greeting_request(DEFAULT_AGENT.greeting)][0]["content"]
    assert instructions.endswith(SEARCH_NOTE) and NO_TOOLS_NOTE not in instructions
    assert "Today's date is " in instructions

    # The lines it was handed to say came under instructions of their own:
    # the "one moment" line, recorded as the call began, and the answer.
    for line in (say_request(HOLD_LINE), say_request(FOUND)):
        assert handed[line][0] == {"role": "system", "content": line_instruction(DEFAULT_AGENT)}
    assert handed[say_request(FOUND)][-4:-1] == [
        {"role": "user", "content": HEARD},
        {"role": "assistant", "content": HOLD_LINE},
        {"role": "system", "content": accent_reminder(DEFAULT_AGENT)},
    ]


async def test_a_searchs_answer_the_provider_refuses_is_asked_for_again_and_said(search_server):
    server = search_server
    # As on a real call on 2026-11-13: the request to say what was found gets a 503.
    server.openrouter.state.refuse_once.add(FOUND)
    peer, _, ear = make_caller(lead_silence=3.5)
    try:
        async with httpx.AsyncClient(base_url=server.base, timeout=30) as http:
            detail = await searched_call(http, peer)
        await wait_until(lambda: asyncio.sleep(0, ear.loud_frames > 50), 10, "agent audio")
    finally:
        await peer.close()

    # The caller gets the answer, and the call shows no problem.
    assert [(m["role"], m["text"]) for m in detail["messages"]][-2:] == [
        ("note", f"Searched the web for: {SEARCH_FOR}"),
        ("agent", f"{HOLD_LINE} {FOUND}"),
    ]
    assert detail["call"]["error"] == ""
    assert server.openrouter.state.refuse_once == set()  # it was refused
    # Asked for twice, the same way.
    first, second = [
        body for body in sent(server, "chat") if body["messages"][-1]["content"] == say_request(FOUND)
    ]
    assert first == second
    assert len(server.firecrawl.state.requests) == 1  # the search was not made again


async def test_what_a_search_found_is_kept_and_written_up_when_the_call_ends(search_server):
    server = search_server
    peer, _, _ = make_caller(lead_silence=3.5)
    page = {"title": "Bondi Bikes", "url": "https://bondibikes.example/hours", "news": False, "date": ""}
    try:
        async with httpx.AsyncClient(base_url=server.base, timeout=30) as http:
            recorder = EventRecorder(http)
            await asyncio.sleep(0.3)  # let the event stream connect
            detail = await searched_call(http, peer)
            call_id = detail["call"]["id"]

            # The search is on the call's page as soon as it is made: what was
            # searched for and the page found, by title and address.
            (search,) = detail["searches"]
            assert (search["query"], search["sources"]) == (SEARCH_FOR, [page])
            assert detail["call"]["research"] == [] and sent(server, "research") == []

            async def summed_up():
                detail = (await http.get(f"/api/calls/{call_id}")).json()
                return detail if detail["call"]["summary_status"] == "ready" else None

            detail = await wait_until(summed_up, 15, "the call's summary and research notes")
            await asyncio.sleep(0.3)
            await recorder.stop()
    finally:
        await peer.close()

    # The note the model wrote, with a link to the page it named.
    call = detail["call"]
    assert call["research"] == [
        {
            "text": "Bondi Bikes is open 9am to 5pm every day.",
            "sources": [{"title": "Bondi Bikes", "url": "https://bondibikes.example/hours"}],
        }
    ]
    assert (call["research_error"], call["summary_error"], call["error"]) == ("", "", "")
    assert detail["searches"] == [search]

    # One request, beside the summary's: the transcript, then the page as Oscar read it.
    (asked,) = sent(server, "research")
    assert asked["model"] == RESEARCH_MODEL and "modalities" not in asked
    system, matter = asked["messages"]
    assert system == {"role": "system", "content": RESEARCH_INSTRUCTIONS.format(name="Oscar")}
    assert matter["content"] == (
        transcript_text(detail["messages"], kind="browser", agent_name="Oscar")
        + "\n\nWhat Oscar's searches on the call found:\n\n"
        f'Searched for "{SEARCH_FOR}":\n\n'
        "[1] Bondi Bikes (https://bondibikes.example/hours)\nOpening hours Open 9am to 5pm every day."
    )
    assert len(sent(server, "summary")) == 1

    # The dashboard heard of the search when it was made, without the page's words.
    (heard,) = [e["search"] for e in recorder.events if e["type"] == "search"]
    assert heard == search and "excerpt" not in heard["sources"][0]
    assert recorder.events[-1]["call"]["research"] == call["research"]


@pytest.mark.parametrize("search_server", ["fc-a-wrong-key"], indirect=True)
async def test_a_search_that_cannot_run_is_explained_on_the_call(search_server):
    server = search_server
    peer, _, _ = make_caller(lead_silence=3.5)
    try:
        async with httpx.AsyncClient(base_url=server.base, timeout=30) as http:
            detail = await searched_call(http, peer)
    finally:
        await peer.close()

    problem = "Firecrawl rejected the API key (check FIRECRAWL_API_KEY). Unauthorized: Invalid token"
    said = [(m["role"], m["text"]) for m in detail["messages"]]
    # The caller is told, and the dashboard says why.
    assert said[-2:] == [
        ("note", f"Could not search the web. {problem}"),
        ("agent", f"{HOLD_LINE} {SEARCH_FAILED_LINE}"),
    ]
    assert detail["call"]["error"] == f"Web search failed: {problem}"
    assert detail["call"]["status"] == "live"  # the call itself carried on
    assert sent(server, "reading") == []  # nothing to read
    assert detail["searches"] == []  # and nothing found to keep


async def test_without_a_firecrawl_key_calls_do_not_search(server):
    peer, _, _ = make_caller(lead_silence=3.5)
    try:
        async with httpx.AsyncClient(base_url=server.base, timeout=30) as http:
            assert (await http.get("/api/status")).json()["search"] == {"enabled": False}
            call_id = await start_browser_call(http, peer)

            async def exchange_done():
                detail = (await http.get(f"/api/calls/{call_id}")).json()
                return has_full_exchange([(m["role"], m["text"]) for m in detail["messages"]])

            await wait_until(exchange_done, 45, "the agent to hear the caller and reply")
            await http.post(f"/api/calls/{call_id}/end")
    finally:
        await peer.close()
    # No decision was asked for, no "one moment" line was recorded.
    assert sent(server, "decision") == []
    assert not any(say_request(HOLD_LINE) in json.dumps(body) for body in sent(server, "chat"))


# The owner's fact sheet

FACT_SHEET = (
    "We are Bondi Bikes, 12 Beach Road.\n"
    "We open at 9 am and close at 5 pm, every day.\n"
    "Call 02 5550 0142 to book a repair."
)
# What the model reading the fact sheet writes, and what Oscar then says.
WRITTEN = "We open at nine tomorrow morning. To book a repair, call [number 1]."
KNOWN = (
    "We open at nine tomorrow morning. To book a repair, call "
    "zero two, five five five zero, zero one four two."
)


@pytest.fixture
async def facts_server(tmp_path, request):
    """Oscar with a fact sheet and no web search, in front of a stand-in for OpenRouter.

    The stand-in decides that the caller's question is one for the fact sheet
    (and that "Hi there", said first, is not), and writes `request.param` as
    the answer (WRITTEN unless a test says otherwise).
    """
    openrouter = create_fake_openrouter(
        heard=HEARD,
        heard_in_brief_clips=HEARD_FIRST,
        reply=REPLY,
        api_key=API_KEY,
        answer_delay=0.5,
        decision=f"FACTS\n{HEARD}",
        found=getattr(request, "param", WRITTEN),
    )
    openrouter_server = await serve(openrouter)
    cfg = make_config(
        tmp_path,
        openrouter_api_key=API_KEY,
        openrouter_base_url=f"http://127.0.0.1:{openrouter_server[0]}/api/v1",
    )
    oscar_server = await serve(create_app(cfg))

    yield SimpleNamespace(base=f"http://127.0.0.1:{oscar_server[0]}", openrouter=openrouter)

    await shut_down(oscar_server, openrouter_server)


async def call_about_the_owner(http, peer, last_line: str) -> dict:
    """Give Oscar the fact sheet, place a browser call, and wait for `last_line` to be said."""
    told = await http.put("/api/agent", json={**DEFAULT_AGENT.model_dump(), "knowledge": FACT_SHEET})
    assert told.status_code == 200
    call_id = await start_browser_call(http, peer)

    async def answered():
        detail = (await http.get(f"/api/calls/{call_id}")).json()
        said = [m["text"] for m in detail["messages"] if m["role"] == "agent"]
        return detail if any(text.endswith(last_line) for text in said) else None

    detail = await wait_until(answered, 45, "Oscar to answer from its fact sheet")
    await http.post(f"/api/calls/{call_id}/end")
    return detail


async def test_browser_call_answers_from_what_oscar_knows(facts_server):
    server = facts_server
    peer, _, ear = make_caller(lead_silence=3.5)
    try:
        async with httpx.AsyncClient(base_url=server.base, timeout=30) as http:
            # A fact sheet needs no Firecrawl key.
            assert (await http.get("/api/status")).json()["search"] == {"enabled": False}
            detail = await call_about_the_owner(http, peer, KNOWN)
        await wait_until(lambda: asyncio.sleep(0, ear.loud_frames > 50), 10, "agent audio")
    finally:
        await peer.close()

    # One reply: the "one moment" line, then the answer, with the phone number
    # spelt out. The model's own reply to the question (REPLY) was never said,
    # and nothing was searched for.
    assert [(m["role"], m["text"]) for m in detail["messages"]] == [
        ("agent", DEFAULT_AGENT.greeting),
        ("user", f"{HEARD_FIRST} {HEARD}"),
        ("agent", f"{HOLD_LINE} {KNOWN}"),
    ]
    assert detail["call"]["error"] == ""

    # The decision was asked of the model that hears, and shown the fact sheet.
    decisions = sent(server, "decision")
    assert decisions and all(body["model"] == ROUTER_MODEL for body in decisions)
    system, *earlier, turn = decisions[-1]["messages"]
    assert FACT_SHEET in system["content"] and "FACTS\n" in system["content"]
    assert "SEARCH" not in system["content"]  # there is no web search to choose
    assert earlier[0] == {"role": "assistant", "content": DEFAULT_AGENT.greeting}
    assert clip_of(turn)[2] > 2.5  # the whole question

    # A text model wrote the answer from the fact sheet, in which the phone
    # number is a tag: its digits are spelt out by Oscar, not by a model.
    (reading,) = sent(server, "reading")
    assert reading["model"] == READER_MODEL
    sheet, *talk = reading["messages"]
    assert "Call [number 1] to book a repair." in sheet["content"] and "5550" not in sheet["content"]
    assert talk == [
        {"role": "assistant", "content": DEFAULT_AGENT.greeting},
        {"role": "user", "content": HEARD},
    ]

    # The speech model never saw the fact sheet: it would cost the agent's accent.
    spoken = sent(server, "chat")
    assert not any("12 Beach Road" in json.dumps(body) or "5550" in json.dumps(body) for body in spoken)
    handed = {
        body["messages"][-1]["content"]: body["messages"]
        for body in spoken
        if isinstance(body["messages"][-1]["content"], str)
    }
    instructions = handed[greeting_request(DEFAULT_AGENT.greeting)][0]["content"]
    assert NO_KNOWLEDGE_NOTE in instructions and instructions.endswith(NO_TOOLS_NOTE)

    # It was handed two lines to say, under instructions of their own: the
    # "one moment" line, recorded as the call began, and the answer.
    for line in (say_request(HOLD_LINE), say_request(KNOWN)):
        assert handed[line][0] == {"role": "system", "content": line_instruction(DEFAULT_AGENT)}
    assert handed[say_request(KNOWN)][-4:-1] == [
        {"role": "user", "content": HEARD},
        {"role": "assistant", "content": HOLD_LINE},
        {"role": "system", "content": accent_reminder(DEFAULT_AGENT)},
    ]


@pytest.mark.parametrize("facts_server", ["Call us on [number 2]."], indirect=True)
async def test_an_answer_the_fact_sheet_cannot_give_is_explained_on_the_call(facts_server):
    # The model writing the answer named a number the fact sheet does not have.
    server = facts_server
    peer, _, _ = make_caller(lead_silence=3.5)
    try:
        async with httpx.AsyncClient(base_url=server.base, timeout=30) as http:
            detail = await call_about_the_owner(http, peer, FACTS_FAILED_LINE)
    finally:
        await peer.close()

    # The caller is told, a wrong number is never read out, and the dashboard says why.
    assert (detail["messages"][-1]["role"], detail["messages"][-1]["text"]) == (
        "agent",
        f"{HOLD_LINE} {FACTS_FAILED_LINE}",
    )
    assert detail["call"]["error"] == (
        "Could not answer from what Oscar knows: "
        "The answer from the fact sheet named a number that is not in it."
    )
    assert detail["call"]["status"] == "live"  # the call itself carried on


async def test_at_its_time_limit_a_call_says_goodbye_and_hangs_up(server, monkeypatch):
    from app import agent as voice_agent

    monkeypatch.setattr(voice_agent, "SECONDS_IN_A_MINUTE", 4)  # "1 minute" is 4 seconds here
    peer, _, ear = make_caller(lead_silence=60)  # a caller who says nothing
    async with httpx.AsyncClient(base_url=server.base, timeout=30) as http:
        settings = {**DEFAULT_AGENT.model_dump(), "max_call_minutes": 1}
        assert (await http.put("/api/agent", json=settings)).status_code == 200
        started = time.monotonic()
        call_id = await start_browser_call(http, peer)

        async def ended():
            detail = (await http.get(f"/api/calls/{call_id}")).json()
            return detail if detail["call"]["status"] in ("ended", "failed") else None

        detail = await wait_until(ended, 30, "the call to end at its time limit")
        took = time.monotonic() - started
        said = [(m["role"], m["text"]) for m in detail["messages"]]
        assert ("note", "The call reached its 1 minute limit.") in said
        assert ("agent", voice_agent.FAREWELL_LINE) in said
        assert said.index(("agent", voice_agent.FAREWELL_LINE)) > said.index(("note", "The call reached its 1 minute limit."))
        # Ended cleanly, once the goodbye had been heard: not as a failure, and not after the 15 s wait.
        assert (detail["call"]["status"], detail["call"]["error"]) == ("ended", "")
        assert took < 4 + voice_agent.FAREWELL_WAIT_SECONDS
        assert any(t > started + 4 for t in ear.loud_times), "the goodbye was heard"
    await peer.close()


async def test_settings_can_be_tried_on_a_call_before_they_are_saved(server):
    from app.main import TRYING_SETTINGS_NOTE

    peer, _, ear = make_caller(lead_silence=60)
    trying = {**DEFAULT_AGENT.model_dump(), "name": "Ava", "greeting": "Hello, Ava here."}
    async with httpx.AsyncClient(base_url=server.base, timeout=30) as http:
        await peer.setLocalDescription(await peer.createOffer())
        response = await http.post(
            "/api/calls/browser", json={"sdp": peer.localDescription.sdp, "type": "offer", "agent": trying}
        )
        assert response.status_code == 200, response.text
        answer = response.json()
        await peer.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]))
        call_id = answer["call_id"]

        async def greeted():
            detail = (await http.get(f"/api/calls/{call_id}")).json()
            said = [(m["role"], m["text"]) for m in detail["messages"]]
            return said if ("agent", "Hello, Ava here.") in said else None

        said = await wait_until(greeted, 20, "the tried settings' greeting")
        assert said[0] == ("note", TRYING_SETTINGS_NOTE)
        # Said by the settings being tried, which are not saved.
        (asked, *_) = sent(server, "chat")
        assert asked["messages"][0]["content"].startswith("Your name is Ava.")
        assert (await http.get("/api/agent")).json()["name"] == DEFAULT_AGENT.name
        await http.post(f"/api/calls/{call_id}/end")
    await peer.close()


# Putting a phone caller through to a person

TRANSFER_TO = "+61412345678"


@pytest.fixture
async def transfer_server(tmp_path):
    """Oscar with a number to put callers through to. The stand-in decides the caller's
    question is a request for a person (and that "Hi there", said first, is not)."""
    openrouter = create_fake_openrouter(
        heard=HEARD, heard_in_brief_clips=HEARD_FIRST, reply=REPLY, api_key=API_KEY, answer_delay=0.5, decision="TRANSFER"
    )
    openrouter_server = await serve(openrouter)
    cfg = make_config(
        tmp_path, openrouter_api_key=API_KEY, openrouter_base_url=f"http://127.0.0.1:{openrouter_server[0]}/api/v1"
    )
    app = create_app(cfg)
    app.state.store.save_agent(DEFAULT_AGENT.model_copy(update={"transfer_number": TRANSFER_TO}))
    oscar_server = await serve(app)
    yield SimpleNamespace(base=f"http://127.0.0.1:{oscar_server[0]}", port=oscar_server[0], app=app, openrouter=openrouter)
    await shut_down(oscar_server, openrouter_server)


async def test_a_phone_caller_who_asks_for_a_person_is_put_through(transfer_server, monkeypatch):
    from app import phone
    from app.speech import TRANSFER_LINE
    from tests.test_phone import FakeTwilioCalls, FakeTwilioClient

    twilio = FakeTwilioCalls()
    monkeypatch.setattr(phone, "_twilio_client", lambda cfg: FakeTwilioClient(twilio))
    hang_ups: list[str] = []

    async def record_hang_up(serializer):
        hang_ups.append(serializer._call_sid)

    monkeypatch.setattr(TwilioFrameSerializer, "_hang_up_call", record_hang_up)
    server = transfer_server
    store = server.app.state.store

    async with httpx.AsyncClient(base_url=server.base, timeout=30) as http:
        signature = RequestValidator(TWILIO_AUTH_TOKEN).compute_signature(PUBLIC_URL + "/twilio/voice", INBOUND)
        voice = await http.post("/twilio/voice", data=INBOUND, headers={"X-Twilio-Signature": signature})
        _, params = stream_parameters(voice.text)
        call_id = params["call_id"]

        pcm, rate = read_wav(CALLER_CLIP)
        audio = b"\x00\x00" * int(rate * 3.5) + pcm + b"\x00\x00" * (rate * 15)
        caller_ulaw = await pcm_to_ulaw(audio, rate, 8000, create_stream_resampler())
        stream_sid = "MZ" + "6" * 32
        start = {
            "event": "start",
            "sequenceNumber": "1",
            "streamSid": stream_sid,
            "start": {
                "streamSid": stream_sid,
                "callSid": INBOUND["CallSid"],
                "accountSid": "AC" + "0" * 32,
                "customParameters": params,
                "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
            },
        }
        async with websockets.connect(f"ws://127.0.0.1:{server.port}/twilio/media") as ws:
            await ws.send(json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}))
            await ws.send(json.dumps(start))
            # The audio stream plays the caller in real time, and Twilio would
            # play Oscar's audio as it comes: here it is read and dropped.
            reader = asyncio.create_task(ws.wait_closed())
            for chunk_number, offset in enumerate(range(0, len(caller_ulaw), 160)):
                payload = base64.b64encode(caller_ulaw[offset : offset + 160]).decode()
                await ws.send(json.dumps({"event": "media", "streamSid": stream_sid, "media": {"payload": payload}}))
                await asyncio.sleep(0.02)
                if twilio.updates:
                    break
            assert twilio.updates, store.get_messages(call_id)
            # Twilio now has the call, and ends Oscar's audio stream.
            await ws.close()
            reader.cancel()

        async def ended():
            call = store.get_call(call_id)
            return call if call["status"] == "ended" else None

        call = await wait_until(ended, 20, "Oscar's side of the call to end")

    ((sid, update),) = twilio.updates
    assert sid == INBOUND["CallSid"] and f">{TRANSFER_TO}</Dial>" in update["twiml"]
    said = [(m["role"], m["text"]) for m in store.get_messages(call_id)]
    assert ("agent", TRANSFER_LINE) in said
    assert ("note", f"Put the caller through to {TRANSFER_TO}.") in said
    assert REPLY not in [text for role, text in said if role == "agent"]  # the reply to the question was dropped
    assert (call["error"], call["transferred_to"]) == ("", TRANSFER_TO)
    assert hang_ups == []  # Oscar stopping did not hang up the call it handed over
