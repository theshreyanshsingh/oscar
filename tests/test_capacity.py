"""How many calls Oscar is on at once (MAX_CALLS): one more is turned away politely."""

import pytest

from app.config import ConfigError, load_config
from tests.conftest import local_client, make_config
from tests.test_api import env  # noqa: F401 - the clean environment for load_config()
from tests.test_phone import signed_post

CALLER = {"CallSid": "CA" + "7" * 32, "From": "+14155550123", "To": "+15005550006"}


def test_eight_calls_at_once_unless_max_calls_says_otherwise(env):
    assert load_config().max_calls == 8
    env.setenv("MAX_CALLS", "3")
    assert load_config().max_calls == 3


@pytest.mark.parametrize("value", ["lots", "0", "101", "-1"])
def test_max_calls_must_be_a_sensible_number(env, value):
    env.setenv("MAX_CALLS", value)
    with pytest.raises(ConfigError, match="MAX_CALLS"):
        load_config()


@pytest.fixture
def full(tmp_path):
    """Oscar with room for one call, and one call going."""
    with local_client(make_config(tmp_path, max_calls=1)) as client:
        client.app.state.store.create_call("browser", status="live")
        yield client


def test_someone_phoning_in_hears_the_lines_are_busy_and_the_call_is_kept(full):
    response = signed_post(full, "/twilio/voice", CALLER)
    assert response.status_code == 200
    assert "all our lines are busy" in response.text and "<Hangup" in response.text
    assert "<Stream" not in response.text
    turned_away = next(c for c in full.get("/api/calls").json()["calls"] if c["kind"] == "phone_inbound")
    assert turned_away["status"] == "failed" and turned_away["from_number"] == CALLER["From"]
    assert "already on 1 call, the most it takes at once (MAX_CALLS" in turned_away["error"]


def test_a_browser_call_or_a_call_out_waits_for_a_free_line(full):
    for path, body in [("/api/calls/browser", {"sdp": "v=0", "type": "offer"}), ("/api/calls/phone", {"to": "+14155550123"})]:
        response = full.post(path, json=body)
        assert response.status_code == 503, path
        assert "already on 1 call" in response.json()["detail"]


def test_a_call_that_ended_frees_its_line(full):
    store = full.app.state.store
    (going,) = [c for c in full.get("/api/calls").json()["calls"]]
    store.update_call(going["id"], status="ended")
    response = signed_post(full, "/twilio/voice", CALLER)
    assert "<Stream" in response.text
