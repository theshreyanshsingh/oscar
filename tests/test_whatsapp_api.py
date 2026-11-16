"""The dashboard's view of the WhatsApp process, through the shared database."""

from datetime import datetime, timedelta, timezone

from app.store import DEFAULT_WHATSAPP_SETTINGS, WHATSAPP_ACTIVITY_KEPT, Store, WhatsAppSettings

SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"></svg>'


def test_not_running_by_default(client):
    data = client.get("/api/whatsapp").json()
    assert data["state"]["running"] is False
    assert data["state"]["status"] == "stopped"
    assert data["settings"] == DEFAULT_WHATSAPP_SETTINGS.model_dump()
    assert data["activity"] == []
    assert data["agent_name"] == "Oscar"
    assert data["openrouter_ready"] is True
    assert client.get("/api/whatsapp/qr.svg").status_code == 404


def test_waiting_for_scan_serves_the_code(client, cfg):
    store = Store(cfg.db_path)
    store.set_whatsapp_state("waiting_for_scan", detail="Scan it", qr_svg=SVG)
    state = client.get("/api/whatsapp").json()["state"]
    assert state["running"] and state["has_qr"] and state["qr_version"]

    qr = client.get("/api/whatsapp/qr.svg")
    assert qr.status_code == 200
    assert qr.headers["content-type"] == "image/svg+xml"
    assert qr.text == SVG

    # A new code gets a new version, so the dashboard reloads the image.
    first = state["qr_version"]
    store.set_whatsapp_state("waiting_for_scan", detail="Scan it", qr_svg=SVG.replace("10", "11"))
    assert client.get("/api/whatsapp").json()["state"]["qr_version"] != first

    # Once linked, the code is gone and the number is shown.
    store.set_whatsapp_state("connected", detail="Online", phone="+15005550006")
    state = client.get("/api/whatsapp").json()["state"]
    assert (state["status"], state["phone"], state["has_qr"]) == ("connected", "+15005550006", False)
    assert client.get("/api/whatsapp/qr.svg").status_code == 404

    # Later updates keep the number unless a new one is given.
    store.set_whatsapp_state("starting", detail="Reconnecting")
    assert client.get("/api/whatsapp").json()["state"]["phone"] == "+15005550006"
    store.close()


def test_stale_heartbeat_means_not_running(client, cfg):
    store = Store(cfg.db_path)
    store.set_whatsapp_state("waiting_for_scan", detail="Scan it", qr_svg=SVG)
    old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    store._db.execute("UPDATE whatsapp_state SET heartbeat_at = ?", (old,))
    state = client.get("/api/whatsapp").json()["state"]
    assert state["running"] is False
    assert state["status"] == "stopped"
    assert state["last_status"] == "waiting_for_scan"
    assert client.get("/api/whatsapp/qr.svg").status_code == 404  # an old code is useless

    store.touch_whatsapp_heartbeat()
    assert client.get("/api/whatsapp").json()["state"]["running"] is True
    store.close()


def test_logged_out_reason_survives_the_process_exiting(client, cfg):
    store = Store(cfg.db_path)
    store.set_whatsapp_state("logged_out", detail="WhatsApp logged Oscar out.")
    old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    store._db.execute("UPDATE whatsapp_state SET heartbeat_at = ?", (old,))
    state = client.get("/api/whatsapp").json()["state"]
    assert (state["status"], state["last_status"]) == ("stopped", "logged_out")
    assert state["detail"] == "WhatsApp logged Oscar out."
    store.close()


def test_save_settings(client):
    body = {
        "instructions": "  Keep it short.  ",
        "web_search": False,
        "reply_in_private_chats": True,
        "own_number": True,
    }
    saved = client.put("/api/whatsapp/settings", json=body)
    assert saved.status_code == 200
    expected = {**body, "instructions": "Keep it short."}
    assert saved.json() == expected
    assert client.get("/api/whatsapp").json()["settings"] == expected

    too_long = client.put("/api/whatsapp/settings", json={**body, "instructions": "x" * 4001})
    assert too_long.status_code == 422


def test_oscar_has_a_number_to_itself_unless_told_otherwise(client):
    assert client.get("/api/whatsapp").json()["settings"]["own_number"] is False
    # A page loaded before the setting existed leaves it out when it saves.
    saved = client.put("/api/whatsapp/settings", json={"instructions": "Hi", "web_search": True})
    assert saved.json()["own_number"] is False


def test_settings_saved_by_an_earlier_version_of_oscar_still_load(cfg):
    import sqlite3

    store = Store(cfg.db_path)
    store.save_whatsapp_settings(WhatsAppSettings(instructions="Old ones", own_number=True))
    store.close()
    with sqlite3.connect(cfg.db_path) as db:
        db.execute("ALTER TABLE whatsapp_settings DROP COLUMN own_number")  # as it used to be

    store = Store(cfg.db_path)
    try:
        settings = store.get_whatsapp_settings()
        assert (settings.instructions, settings.own_number) == ("Old ones", False)
        store.save_whatsapp_settings(settings.model_copy(update={"own_number": True}))
        assert store.get_whatsapp_settings().own_number is True
    finally:
        store.close()


def test_activity_is_listed_newest_first_and_pruned(client, cfg):
    store = Store(cfg.db_path)
    for i in range(WHATSAPP_ACTIVITY_KEPT + 5):
        store.add_whatsapp_activity(chat_name="Weekend", asker="Jay", request=f"q{i}", reply=f"a{i}")
    store.add_whatsapp_activity(
        chat_name="Weekend", asker="Roman", request="last", reply="Sorry", error="out of credits"
    )
    activity = client.get("/api/whatsapp").json()["activity"]
    assert len(activity) == 30
    assert activity[0]["request"] == "last" and activity[0]["error"] == "out of credits"
    total = store._db.execute("SELECT COUNT(*) FROM whatsapp_activity").fetchone()[0]
    assert total == WHATSAPP_ACTIVITY_KEPT
    store.close()


def test_database_files_are_private(cfg):
    store = Store(cfg.db_path)
    store.add_whatsapp_activity(chat_name="c", asker="a", request="r", reply="x")
    assert oct(cfg.db_path.stat().st_mode & 0o777) == "0o600"
    store.close()


def test_whatsapp_api_is_password_protected(tmp_path):
    from tests.conftest import local_client, make_config

    cfg = make_config(tmp_path, dashboard_password="s3cret")
    with local_client(cfg) as client:
        assert client.get("/api/whatsapp").status_code == 401
        assert client.get("/api/whatsapp/qr.svg").status_code == 401
        assert client.put("/api/whatsapp/settings", json={}).status_code == 401


def test_exited_process_is_not_running_but_keeps_its_reason(client, cfg):
    store = Store(cfg.db_path)
    store.set_whatsapp_state("error", detail="The linking code expired before it was scanned.")
    assert client.get("/api/whatsapp").json()["state"]["running"] is True  # heartbeat is fresh

    store.mark_whatsapp_exited()
    state = client.get("/api/whatsapp").json()["state"]
    assert state["running"] is False
    assert (state["status"], state["last_status"]) == ("stopped", "error")
    assert state["detail"] == "The linking code expired before it was scanned."

    # Starting again makes it count as running straight away.
    store.set_whatsapp_state("starting", detail="Connecting to WhatsApp…")
    assert client.get("/api/whatsapp").json()["state"]["running"] is True
    store.close()
