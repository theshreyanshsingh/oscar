"""Place one real browser call against a running Oscar server, using your real OpenRouter key.

A recorded voice asks "Hi there. What time do you open tomorrow?". The check
passes when the agent's greeting plays, the caller is heard, and a reply is
spoken back. It also prints how long the reply took to start. Costs about two
cents on OpenRouter.

    .venv/bin/python -m scripts.live_call_check            # server on http://127.0.0.1:7860
    .venv/bin/python -m scripts.live_call_check --url http://127.0.0.1:8000

With --search the voice asks for today's weather in Kanpur, India instead, and
the check also needs Oscar to have looked it up on the web. That costs about
three cents on OpenRouter and two Firecrawl credits.

    .venv/bin/python -m scripts.live_call_check --search
"""

import argparse
import asyncio
import sys
import time

import httpx
from aiortc import RTCSessionDescription

from app.config import load_config
from tests.audio_client import CALLER_CLIP, WEATHER_CLIP, make_caller


def fail(message: str) -> None:
    print(f"FAILED: {message}")
    sys.exit(1)


async def wait_for(check, timeout: float):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = await check()
        if result:
            return result
        await asyncio.sleep(0.25)
    return None


async def main(url: str, search: bool) -> None:
    cfg = load_config()
    auth = ("admin", cfg.dashboard_password) if cfg.dashboard_password else None

    async with httpx.AsyncClient(base_url=url, auth=auth, timeout=30) as http:
        try:
            status = (await http.get("/api/status")).raise_for_status().json()
        except httpx.HTTPError as exc:
            fail(f"Could not reach the server at {url} ({exc}). Start it with: .venv/bin/python -m app")
        if not status["agent"]["ready"]:
            fail(f"The server is missing {', '.join(status['agent']['missing'])}.")
        if search and not status["search"]["enabled"]:
            fail("Web search is off. Add FIRECRAWL_API_KEY to .env and restart the server.")
        agent = (await http.get("/api/agent")).json()
        print(f"Agent: {agent['name']} · voice {agent['voice']} · accent {agent['accent']}")

        # Leave the agent time to finish its greeting before the caller speaks.
        peer, microphone, ear = make_caller(
            lead_silence=7.0, clip=WEATHER_CLIP if search else CALLER_CLIP
        )
        await peer.setLocalDescription(await peer.createOffer())
        response = await http.post(
            "/api/calls/browser", json={"sdp": peer.localDescription.sdp, "type": "offer"}
        )
        if response.status_code != 200:
            fail(f"The server refused the call: {response.text}")
        answer = response.json()
        call_id = answer["call_id"]
        await peer.setRemoteDescription(
            RTCSessionDescription(sdp=answer["sdp"], type=answer["type"])
        )
        print(f"Call {call_id} started. The caller speaks after 7 seconds of silence…")

        async def get_detail():
            return (await http.get(f"/api/calls/{call_id}")).json()

        async def replied():
            detail = await get_detail()
            roles = [m["role"] for m in detail["messages"]]
            if "user" in roles and "agent" in roles[roles.index("user") :]:
                return detail
            if detail["call"]["status"] in ("ended", "failed"):
                return detail
            return None

        detail = await wait_for(replied, 60)
        await asyncio.sleep(4)  # let the reply play
        # Sound from Oscar after the caller's clip ended is its reply (the
        # greeting finished long before the caller spoke).
        clip_ended = microphone.clip_ends_at or 0.0
        reply_times = [t for t in ear.loud_times if t > clip_ended + 0.2]

        await http.post(f"/api/calls/{call_id}/end")
        await peer.close()
        final = await wait_for(lambda: _ended(http, call_id), 15)
        if final is None:
            final = (await get_detail())["call"]

        messages = (detail or await get_detail())["messages"]
        print("\nTranscript:")
        speakers = {"agent": agent["name"], "user": "Caller", "note": "(note)"}
        for message in messages:
            print(f"  {speakers[message['role']]}: {message['text']}")
        if reply_times:
            wait = reply_times[0] - clip_ended
            what = "speaking" if search else "answering"
            print(f"\nOscar started {what} {wait:.1f}s after the caller stopped speaking.")
        print(f"Call status: {final['status']}" + (f" ({final['error']})" if final["error"] else ""))

        # What Oscar recorded about the failure, if anything (a rejected key, no credits...).
        problem = f" Oscar recorded: {final['error']}" if final["error"] else ""
        roles = [m["role"] for m in messages]
        if "user" not in roles:
            fail("The agent never heard the caller." + problem)
        if "agent" not in roles[roles.index("user") :]:
            fail("The agent heard the caller but did not reply." + problem)
        if len(reply_times) < 25:
            fail("The reply was not spoken back to the caller." + problem)
        notes = [m["text"] for m in messages if m["role"] == "note"]
        if search and not any(note.startswith("Searched the web for:") for note in notes):
            fail("The agent answered without searching the web." + problem)
        if final["error"]:
            fail(f"The call recorded an error: {final['error']}")
        print("\nPASSED: the agent listened, searched the web and spoke." if search else "\nPASSED: the agent listened and spoke.")


async def _ended(http: httpx.AsyncClient, call_id: str):
    call = (await http.get(f"/api/calls/{call_id}")).json()["call"]
    return call if call["status"] in ("ended", "failed") else None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://127.0.0.1:7860")
    parser.add_argument("--search", action="store_true", help="ask something that needs the web")
    args = parser.parse_args()
    asyncio.run(main(args.url, args.search))
