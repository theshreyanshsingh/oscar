import json

import pytest

from app.config import ConfigError, load_config
import sqlite3

from app import config, summary
from app.store import (
    ACCENTS,
    DEFAULT_AGENT,
    KNOWLEDGE_MAX_CHARACTERS,
    SCRIPT_MAX_CHARACTERS,
    VOICES,
    Store,
)
from app.summary import CallSummary
from tests.conftest import local_client, make_config


def test_dashboard_and_static_files_are_served(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "<title>Oscar</title>" in page.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_status_reports_missing_keys(tmp_path):
    cfg = make_config(tmp_path, openrouter_api_key="", public_url="")
    with local_client(cfg) as client:
        status = client.get("/api/status").json()
    # One key covers the model and the speech.
    assert status["agent"] == {"ready": False, "missing": ["OPENROUTER_API_KEY"]}
    assert status["phone"]["enabled"] is False
    assert status["phone"]["missing"] == ["PUBLIC_URL"]
    assert status["search"] == {"enabled": False}
    assert status["voices"] == VOICES and len(status["voices"]) == 13
    assert status["voices"]["cedar"] == "Cedar (male)"
    assert status["accents"] == ACCENTS and "australian" in status["accents"]
    assert next(iter(status["accents"])) == "indian"  # the default accent is listed first


def test_status_when_everything_is_configured(client):
    status = client.get("/api/status").json()
    assert status["agent"]["ready"] is True
    assert status["phone"]["enabled"] is True
    assert status["phone"]["voice_webhook"] == "https://oscar.example.test/twilio/voice"


def test_agent_settings_default_then_save(client):
    assert client.get("/api/agent").json() == DEFAULT_AGENT.model_dump()

    new = {
        "name": "  Ava ",
        "greeting": "Hello!",
        "system_prompt": "Be brief.",
        "model": "anthropic/claude-sonnet-5.5",
        "voice": "marin",
        "accent": "british",
        "knowledge": "  We open at 9 am.\nA haircut is $45.  ",
        "script": "\n1. Ask who is calling.\n2. Say goodbye.\n",
    }
    saved = client.put("/api/agent", json=new)
    assert saved.status_code == 200
    assert saved.json()["name"] == "Ava"
    kept = {
        **new,
        "name": "Ava",
        "knowledge": "We open at 9 am.\nA haircut is $45.",
        "script": "1. Ask who is calling.\n2. Say goodbye.",
    }
    assert client.get("/api/agent").json() == kept


def test_oscar_knows_nothing_about_a_business_until_it_is_told(client):
    assert client.get("/api/agent").json()["knowledge"] == ""
    # A dashboard page left open from before the fact sheet existed saves none.
    body = {k: v for k, v in DEFAULT_AGENT.model_dump().items() if k != "knowledge"}
    assert client.put("/api/agent", json={**body, "name": "Ava"}).status_code == 200
    assert client.get("/api/agent").json()["knowledge"] == ""


def test_oscar_has_no_script_until_it_is_given_one(client):
    assert client.get("/api/agent").json()["script"] == ""
    # A dashboard page left open from before scripts existed saves none.
    body = {k: v for k, v in DEFAULT_AGENT.model_dump().items() if k != "script"}
    assert client.put("/api/agent", json={**body, "name": "Ava"}).status_code == 200
    assert client.get("/api/agent").json()["script"] == ""

    script = "1. Ask who is calling.\n2. Ask what they need help with.\n"
    saved = client.put("/api/agent", json={**DEFAULT_AGENT.model_dump(), "script": script})
    assert saved.status_code == 200
    assert client.get("/api/agent").json()["script"] == script.strip()


def test_a_script_has_a_longest_length(client):
    body = {**DEFAULT_AGENT.model_dump(), "script": "x" * SCRIPT_MAX_CHARACTERS}
    assert client.put("/api/agent", json=body).status_code == 200
    too_long = client.put("/api/agent", json={**body, "script": "x" * (SCRIPT_MAX_CHARACTERS + 1)})
    assert too_long.status_code == 422
    assert too_long.json()["detail"][0]["loc"][-1] == "script"


def test_the_fact_sheet_can_be_as_long_as_was_tried_on_real_calls_and_no_longer(client):
    body = {**DEFAULT_AGENT.model_dump(), "knowledge": "x" * KNOWLEDGE_MAX_CHARACTERS}
    assert client.put("/api/agent", json=body).status_code == 200
    assert len(client.get("/api/agent").json()["knowledge"]) == KNOWLEDGE_MAX_CHARACTERS


def test_out_of_the_box_oscar_is_a_man_with_an_indian_accent(client):
    agent = client.get("/api/agent").json()
    assert (agent["voice"], agent["accent"]) == ("cedar", "indian")
    # The dashboard lists the default voice first.
    assert next(iter(client.get("/api/status").json()["voices"])) == "cedar"


def test_settings_saved_without_an_accent_get_the_default(client):
    # A dashboard page left open from before accents existed saves no accent.
    body = {k: v for k, v in DEFAULT_AGENT.model_dump().items() if k != "accent"}
    saved = client.put("/api/agent", json={**body, "voice": "onyx"})
    assert saved.status_code == 200
    assert client.get("/api/agent").json() == {**DEFAULT_AGENT.model_dump(), "voice": "onyx"}


def test_the_voice_can_keep_its_own_accent(client):
    body = {**DEFAULT_AGENT.model_dump(), "accent": "none"}
    assert client.put("/api/agent", json=body).status_code == 200
    assert client.get("/api/agent").json()["accent"] == "none"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", ""),
        ("name", "x" * 61),
        ("system_prompt", ""),
        ("model", "has spaces"),
        ("model", ""),
        ("voice", "not-a-voice"),
        ("accent", "klingon"),
        ("greeting", "x" * 501),
        ("knowledge", "x" * (KNOWLEDGE_MAX_CHARACTERS + 1)),
    ],
)
def test_agent_settings_validation(client, field, value):
    body = {**DEFAULT_AGENT.model_dump(), field: value}
    response = client.put("/api/agent", json=body)
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][-1] == field


def test_settings_survive_a_restart(cfg):
    with local_client(cfg) as client:
        body = {**DEFAULT_AGENT.model_dump(), "name": "Persisted"}
        assert client.put("/api/agent", json=body).status_code == 200
    with local_client(cfg) as client:
        assert client.get("/api/agent").json()["name"] == "Persisted"


def test_a_voice_from_the_old_speech_model_falls_back_to_the_default(client):
    # Calls used to speak with Deepgram's voices; one of those can still be saved.
    assert client.put("/api/agent", json={**DEFAULT_AGENT.model_dump(), "name": "Ava"}).status_code == 200
    store = client.app.state.store
    with store._lock:
        store._db.execute("UPDATE agent SET voice = 'aura-2-helena-en' WHERE id = 1")
    assert client.get("/api/agent").json() == {**DEFAULT_AGENT.model_dump(), "name": "Ava"}


def test_an_accent_that_is_no_longer_offered_falls_back_to_the_default(client):
    assert client.put("/api/agent", json={**DEFAULT_AGENT.model_dump(), "name": "Ava"}).status_code == 200
    store = client.app.state.store
    with store._lock:
        store._db.execute("UPDATE agent SET accent = 'martian' WHERE id = 1")
    assert client.get("/api/agent").json() == {**DEFAULT_AGENT.model_dump(), "name": "Ava"}


def test_settings_saved_before_accents_existed_still_open(tmp_path):
    # The agent table as Oscar first made it, with settings saved in it.
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE agent (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            name TEXT NOT NULL,
            greeting TEXT NOT NULL,
            system_prompt TEXT NOT NULL,
            model TEXT NOT NULL,
            voice TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO agent VALUES
            (1, 'Ava', 'Hello!', 'Be brief.', 'anthropic/claude-haiku-5.5', 'marin',
             '2026-11-10T00:00:00+00:00');
        """
    )
    old.close()

    store = Store(path)
    try:
        agent = store.get_agent()
        # Settings saved before accents could be chosen keep the accent Oscar had then.
        assert (agent.name, agent.voice, agent.accent) == ("Ava", "marin", "australian")
        assert agent.knowledge == "" and agent.script == ""
        store.save_agent(
            agent.model_copy(
                update={"accent": "british", "knowledge": "Open 9 to 5.", "script": "Ask who is calling."}
            )
        )
        saved = store.get_agent()
        assert (saved.accent, saved.knowledge, saved.script) == ("british", "Open 9 to 5.", "Ask who is calling.")
    finally:
        store.close()
    # Opening it again (or from the WhatsApp process) changes nothing more.
    again = Store(path)
    try:
        assert again.get_agent().accent == "british"
    finally:
        again.close()


def test_browser_call_options_are_checked(client):
    offer = {"sdp": "v=0", "type": "offer", "interruptible": "sometimes"}
    response = client.post("/api/calls/browser", json=offer)
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][-1] == "interruptible"


def test_calls_refused_without_keys(tmp_path):
    cfg = make_config(tmp_path, openrouter_api_key="")
    with local_client(cfg) as client:
        browser = client.post("/api/calls/browser", json={"sdp": "v=0", "type": "offer"})
        phone = client.post("/api/calls/phone", json={"to": "+14155550123"})
    assert browser.status_code == 400
    assert "OPENROUTER_API_KEY" in browser.json()["detail"]
    assert phone.status_code == 400


def test_phone_call_refused_when_phone_not_configured(tmp_path):
    cfg = make_config(tmp_path, public_url="")
    with local_client(cfg) as client:
        response = client.post("/api/calls/phone", json={"to": "+14155550123"})
    assert response.status_code == 400
    assert "PUBLIC_URL" in response.json()["detail"]


@pytest.mark.parametrize("number", ["4155550123", "+0123456789", "+1415", "+1 415 555 0123", ""])
def test_phone_number_must_be_e164(client, number):
    assert client.post("/api/calls/phone", json={"to": number}).status_code == 422


def test_unknown_call_is_404(client):
    assert client.get("/api/calls/nope").status_code == 404
    assert client.post("/api/calls/nope/end").status_code == 404


def test_server_restart_closes_calls_left_open(cfg):
    store = Store(cfg.db_path)
    open_call = store.create_call("browser", status="live")
    done_call = store.create_call("browser", status="ended")
    store.close()

    with local_client(cfg) as client:
        reopened = client.get(f"/api/calls/{open_call['id']}").json()["call"]
        untouched = client.get(f"/api/calls/{done_call['id']}").json()["call"]
    assert reopened["status"] == "failed"
    assert reopened["error"] == "The server stopped during this call."
    assert reopened["ended_at"]
    assert untouched["status"] == "ended"


def test_end_call_that_is_not_running_marks_it_ended(client, cfg):
    store = Store(cfg.db_path)
    call = store.create_call("browser", status="connecting")
    store.close()
    assert client.post(f"/api/calls/{call['id']}/end").json() == {"ended": True}
    assert client.get(f"/api/calls/{call['id']}").json()["call"]["status"] == "ended"
    assert client.post(f"/api/calls/{call['id']}/end").json() == {"ended": False}


def test_a_browser_call_whose_audio_never_connected_says_which_ports_to_open(tmp_path, monkeypatch):
    port_range = tmp_path / "ip_local_port_range"
    port_range.write_text("32768\t60999\n")
    monkeypatch.setattr(config, "LINUX_PORT_RANGE", port_range)
    cfg = make_config(tmp_path, host="0.0.0.0")
    with local_client(cfg) as client:
        call = client.app.state.store.create_call("browser", status="connecting")
        ended = client.post(f"/api/calls/{call['id']}/end", json={"reason": "audio_failed"})
        assert ended.json() == {"ended": True}
        call = client.get(f"/api/calls/{call['id']}").json()["call"]
    assert call["status"] == "failed"
    assert "UDP ports 32768-60999" in call["error"]
    assert "security group" in call["error"]


def test_on_this_computer_an_audio_failure_says_where_to_call_from(client):
    call = client.app.state.store.create_call("browser", status="connecting")
    client.post(f"/api/calls/{call['id']}/end", json={"reason": "audio_failed"})
    call = client.get(f"/api/calls/{call['id']}").json()["call"]
    assert call["status"] == "failed"
    assert "make browser calls on that computer" in call["error"]


@pytest.mark.parametrize(("kind", "status"), [("browser", "live"), ("phone_outbound", "connecting")])
def test_an_audio_failure_is_only_recorded_for_a_browser_call_still_connecting(client, kind, status):
    call = client.app.state.store.create_call(kind, status=status)
    client.post(f"/api/calls/{call['id']}/end", json={"reason": "audio_failed"})
    call = client.get(f"/api/calls/{call['id']}").json()["call"]
    assert call["status"] == "ended"
    assert call["error"] == ""


def test_hanging_up_takes_no_other_reason(client):
    call = client.app.state.store.create_call("browser", status="connecting")
    response = client.post(f"/api/calls/{call['id']}/end", json={"reason": "anything else"})
    assert response.status_code == 422
    assert client.get(f"/api/calls/{call['id']}").json()["call"]["status"] == "connecting"


def test_the_udp_ports_are_read_from_the_system(tmp_path, monkeypatch):
    port_range = tmp_path / "ip_local_port_range"
    port_range.write_text("40000 40999\n")
    monkeypatch.setattr(config, "LINUX_PORT_RANGE", port_range)
    assert config.browser_call_udp_ports() == (40000, 40999)
    monkeypatch.setattr(config, "LINUX_PORT_RANGE", tmp_path / "missing")
    monkeypatch.setattr(config.sys, "platform", "linux")
    assert config.browser_call_udp_ports() == (32768, 60999)


# Call summaries


def spoken_call(client, *, status="ended") -> dict:
    """A call with something said on it, as the store has it once it is over."""
    store = client.app.state.store
    call = store.create_call("browser", status=status)
    store.add_message(call["id"], "agent", "Hi, this is Oscar.")
    store.add_message(call["id"], "user", "It's Sam. Call me back on zero four one two three four five.")
    return call


def test_a_call_starts_without_a_summary(client):
    call = spoken_call(client)
    for listed in (
        client.get(f"/api/calls/{call['id']}").json()["call"],
        client.get("/api/calls").json()["calls"][0],
    ):
        assert {
            key: listed[key]
            for key in ("summary_status", "summary", "caller_name", "callback_number", "follow_up", "summary_error")
        } == dict.fromkeys(
            ("summary_status", "summary", "caller_name", "callback_number", "follow_up", "summary_error"), ""
        )


# What a call's searches found

BIKE_SHOP = {
    "title": "Bondi Bikes",
    "url": "https://bondibikes.example/hours",
    "excerpt": "Opening hours Open 9am to 5pm every day.",
    "news": False,
    "date": "",
}
BIKE_NOTES = [
    {"text": "Bondi Bikes is open 9am to 5pm every day.", "sources": [{"title": "Bondi Bikes", "url": BIKE_SHOP["url"]}]}
]


def test_a_call_starts_without_searches_or_research_notes(client):
    call = spoken_call(client)
    detail = client.get(f"/api/calls/{call['id']}").json()
    assert detail["searches"] == []
    for listed in (detail["call"], client.get("/api/calls").json()["calls"][0]):
        assert (listed["research"], listed["research_error"]) == ([], "")


def test_a_calls_page_lists_what_it_searched_for_and_the_notes_on_what_was_found(client):
    store = client.app.state.store
    call = spoken_call(client)
    other = spoken_call(client)
    store.add_search(call["id"], "opening hours of Bondi Bikes", [BIKE_SHOP])
    store.add_search(call["id"], "owner of Bondi Bikes", [])
    store.add_search(other["id"], "something else", [BIKE_SHOP])
    store.update_call(call["id"], research=json.dumps(BIKE_NOTES))

    detail = client.get(f"/api/calls/{call['id']}").json()

    # The pages are listed by title and address. What they said stays on the server.
    link = {"title": "Bondi Bikes", "url": BIKE_SHOP["url"], "news": False, "date": ""}
    assert [(s["query"], s["sources"]) for s in detail["searches"]] == [
        ("opening hours of Bondi Bikes", [link]),
        ("owner of Bondi Bikes", []),
    ]
    assert all(s["call_id"] == call["id"] and s["created_at"] for s in detail["searches"])
    assert "9am to 5pm every day." not in json.dumps(detail["searches"])
    assert store.get_searches(call["id"])[0]["sources"] == [BIKE_SHOP]  # kept whole for the notes
    # The notes come as a list, on the call's page and in the history.
    assert detail["call"]["research"] == BIKE_NOTES
    listed = {c["id"]: c for c in client.get("/api/calls").json()["calls"]}
    assert listed[call["id"]]["research"] == BIKE_NOTES and listed[other["id"]]["research"] == []


def test_a_summary_can_be_asked_for_from_the_dashboard(client, monkeypatch):
    async def write(messages, **details):
        return CallSummary(
            summary="Sam asked for a call back.",
            caller_name="Sam",
            callback_number="0412345",
            follow_up="Call Sam back.",
        )

    monkeypatch.setattr(summary, "write_summary", write)
    call = spoken_call(client)

    response = client.post(f"/api/calls/{call['id']}/summary")

    assert response.status_code == 200
    assert response.json()["call"]["summary_status"] == "writing"
    done = client.get(f"/api/calls/{call['id']}").json()["call"]
    assert (done["summary_status"], done["summary"], done["follow_up"]) == (
        "ready",
        "Sam asked for a call back.",
        "Call Sam back.",
    )
    assert (done["caller_name"], done["callback_number"]) == ("Sam", "0412345")


def test_a_summary_is_not_written_twice_at_once_or_for_a_call_still_going(client, monkeypatch):
    async def never(messages, **details):
        pytest.fail("a summary was asked for")

    monkeypatch.setattr(summary, "write_summary", never)
    store = client.app.state.store

    live = spoken_call(client, status="live")
    still_going = client.post(f"/api/calls/{live['id']}/summary")
    assert still_going.status_code == 409
    assert "still going" in still_going.json()["detail"]

    busy = spoken_call(client)
    store.update_call(busy["id"], summary_status="writing")
    already = client.post(f"/api/calls/{busy['id']}/summary")
    assert already.status_code == 409
    assert "already being written" in already.json()["detail"]

    silent = store.create_call("browser", status="failed")
    nothing = client.post(f"/api/calls/{silent['id']}/summary")
    assert nothing.status_code == 400
    assert "Nothing was said on this call" in nothing.json()["detail"]

    assert client.post("/api/calls/nope/summary").status_code == 404


def test_a_summary_needs_the_openrouter_key(tmp_path):
    with local_client(make_config(tmp_path, openrouter_api_key="")) as client:
        call = spoken_call(client)
        response = client.post(f"/api/calls/{call['id']}/summary")
    assert response.status_code == 400
    assert "OPENROUTER_API_KEY" in response.json()["detail"]


def test_a_summary_cut_short_by_a_restart_can_be_asked_for_again(cfg):
    store = Store(cfg.db_path)
    cut_short = store.create_call("browser", status="ended")
    store.update_call(cut_short["id"], summary_status="writing")
    written = store.create_call("browser", status="ended")
    store.update_call(written["id"], summary_status="ready", summary="The caller asked the time.")
    store.close()

    with local_client(cfg) as client:
        assert client.get(f"/api/calls/{cut_short['id']}").json()["call"]["summary_status"] == ""
        kept = client.get(f"/api/calls/{written['id']}").json()["call"]
    assert (kept["summary_status"], kept["summary"]) == ("ready", "The caller asked the time.")


def test_calls_logged_before_summaries_existed_still_open(tmp_path):
    # The calls table as Oscar made it before it wrote summaries, with a call in it.
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE calls (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            status TEXT NOT NULL,
            from_number TEXT NOT NULL DEFAULT '',
            to_number TEXT NOT NULL DEFAULT '',
            twilio_sid TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL,
            ended_at TEXT
        );
        INSERT INTO calls (id, kind, status, started_at, ended_at) VALUES
            ('abc', 'browser', 'ended', '2026-11-11T10:00:00+00:00', '2026-11-11T10:01:05+00:00');
        """
    )
    old.close()

    store = Store(path)
    try:
        call = store.get_call("abc")
        assert (call["status"], call["duration_seconds"]) == ("ended", 65)
        assert (call["summary_status"], call["summary"], call["follow_up"]) == ("", "", "")
        updated = store.update_call("abc", summary_status="ready", summary="The caller asked the time.")
        assert updated["summary"] == "The caller asked the time."
        # Nor did it keep what a call searched for, or write that up.
        assert (call["research"], call["research_error"], store.get_searches("abc")) == ([], "", [])
        store.add_search("abc", "the time in Kanpur", [BIKE_SHOP])
        store.update_call("abc", research=json.dumps(BIKE_NOTES))
    finally:
        store.close()
    # Opening it again (or from the WhatsApp process) changes nothing more.
    again = Store(path)
    try:
        assert again.get_call("abc")["summary"] == "The caller asked the time."
        assert again.get_call("abc")["research"] == BIKE_NOTES
        assert [s["sources"] for s in again.get_searches("abc")] == [[BIKE_SHOP]]
    finally:
        again.close()


# Who can open Oscar: no password; on this computer only local Host names (blocks
# DNS rebinding), on a server (HOST=0.0.0.0) anyone who reaches the port.


def test_nothing_asks_for_a_password(tmp_path):
    cfg = make_config(tmp_path, host="0.0.0.0")
    with local_client(cfg) as client:
        for path in ["/", "/api/status", "/api/calls", "/static/app.js", "/healthz"]:
            response = client.get(path)
            assert response.status_code == 200, path
            assert "WWW-Authenticate" not in response.headers
        # Twilio webhooks are still checked by their signature.
        assert client.post("/twilio/voice", data={}).status_code == 403


def test_unknown_host_is_refused_when_local_only(client):
    assert client.get("/api/status").status_code == 200
    assert client.get("/api/status", headers={"host": "127.0.0.1:7860"}).status_code == 200
    assert client.get("/api/status", headers={"host": "attacker.example"}).status_code == 400
    assert client.post("/api/calls/browser", headers={"host": "attacker.example"}).status_code == 400


def test_the_public_url_is_accepted_when_local_only(tmp_path):
    # A tunnel (ngrok, cloudflared) passes its own name on as the Host: Twilio's
    # webhooks and the dashboard through the tunnel come in under it.
    cfg = make_config(tmp_path, public_url="https://abc.ngrok-free.app")
    with local_client(cfg) as client:
        assert client.get("/api/status", headers={"host": "abc.ngrok-free.app"}).status_code == 200
        assert client.post("/twilio/voice", data={}, headers={"host": "abc.ngrok-free.app"}).status_code == 403
        assert client.get("/api/status", headers={"host": "attacker.example"}).status_code == 400


def test_any_host_is_accepted_on_a_server(tmp_path):
    cfg = make_config(tmp_path, host="0.0.0.0")
    with local_client(cfg) as client:
        for host in ["13.185.17.24:7860", "abc.ngrok-free.app"]:
            assert client.get("/api/status", headers={"host": host}).status_code == 200, host


# Configuration rules


@pytest.fixture
def env(monkeypatch):
    """A clean environment for load_config(), whatever is in the developer's .env."""
    for name in [
        "PUBLIC_URL",
        "HOST",
        "PORT",
        "DATABASE_PATH",
        "OPENROUTER_BASE_URL",
        "FIRECRAWL_API_KEY",
        "FIRECRAWL_BASE_URL",
        "CF_ACCESS_TEAM_DOMAIN",
        "CF_ACCESS_AUD",
    ]:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_defaults_are_local_only(env):
    cfg = load_config()
    assert (cfg.host, cfg.port, cfg.public_url, cfg.local_only) == ("127.0.0.1", 7860, "", True)


def test_a_server_host_or_public_url_needs_no_password(env):
    env.setenv("DASHBOARD_PASSWORD", "left over in an old .env")
    env.setenv("HOST", "0.0.0.0")
    env.setenv("PUBLIC_URL", "https://abc.ngrok-free.app")
    cfg = load_config()
    assert (cfg.host, cfg.local_only, cfg.public_url) == ("0.0.0.0", False, "https://abc.ngrok-free.app")


def test_public_url_must_be_https(env):
    env.setenv("PUBLIC_URL", "http://abc.ngrok-free.app")
    with pytest.raises(ConfigError, match="https"):
        load_config()


def test_config_trims_trailing_slash_and_builds_stream_url(env):
    env.setenv("PUBLIC_URL", "https://abc.ngrok-free.app/")
    cfg = load_config()
    assert cfg.public_url == "https://abc.ngrok-free.app"
    assert cfg.media_stream_url == "wss://abc.ngrok-free.app/twilio/media"


def test_bad_port_is_a_config_error(env):
    env.setenv("PORT", "eighty")
    with pytest.raises(ConfigError, match="PORT"):
        load_config()


def test_openrouter_address_defaults_and_can_be_overridden(env):
    assert load_config().openrouter_base_url == "https://openrouter.ai/api/v1"
    env.setenv("OPENROUTER_BASE_URL", "http://127.0.0.1:9999/api/v1/")
    assert load_config().openrouter_base_url == "http://127.0.0.1:9999/api/v1"


def test_one_key_is_all_calls_need(env):
    env.setenv("OPENROUTER_API_KEY", "sk-or-something")
    env.delenv("DEEPGRAM_API_KEY", raising=False)
    assert load_config().missing_agent_keys() == []
    env.setenv("OPENROUTER_API_KEY", "")
    assert load_config().missing_agent_keys() == ["OPENROUTER_API_KEY"]


def test_web_search_is_on_when_there_is_a_firecrawl_key(env):
    cfg = load_config()
    assert (cfg.search_enabled, cfg.firecrawl_base_url) == (False, "https://api.firecrawl.dev/v2")
    env.setenv("FIRECRAWL_API_KEY", "  fc-test  ")
    env.setenv("FIRECRAWL_BASE_URL", "http://127.0.0.1:9998/v2/")
    cfg = load_config()
    assert (cfg.search_enabled, cfg.firecrawl_api_key) == (True, "fc-test")
    assert cfg.firecrawl_base_url == "http://127.0.0.1:9998/v2"


def test_status_says_when_web_search_is_on(tmp_path):
    with local_client(make_config(tmp_path, firecrawl_api_key="fc-test")) as client:
        assert client.get("/api/status").json()["search"] == {"enabled": True}


def test_a_search_note_is_kept_in_the_transcript(client):
    # Not speech: a line recording what Oscar looked up.
    store = client.app.state.store
    call = store.create_call("browser", status="live")
    store.add_message(call["id"], "user", "What's the weather?")
    store.add_message(call["id"], "note", "Searched the web for: weather today")
    messages = client.get(f"/api/calls/{call['id']}").json()["messages"]
    assert [(m["role"], m["text"]) for m in messages] == [
        ("user", "What's the weather?"),
        ("note", "Searched the web for: weather today"),
    ]
