"""Cloudflare Access in front of the dashboard: only signed-in visitors get in."""

import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import access as access_module
from app.access import REFUSED, UNAVAILABLE, CloudflareAccess
from app.config import ConfigError, access_team_domain, load_config
from app.main import create_app
from tests.conftest import make_config

TEAM = "yourteam.cloudflareaccess.com"
AUD = "4714c1358e65fe4b408ad6d432a5f878f08194bdb4752441fd56faefa9b2b6f2"
CERTS = f"https://{TEAM}/cdn-cgi/access/certs"


def new_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def published(*keys: tuple[str, object]) -> dict:
    """What Cloudflare publishes at /cdn-cgi/access/certs, for these (key id, private key) pairs."""
    listed = []
    for key_id, private in keys:
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
        listed.append({**jwk, "kid": key_id, "alg": "RS256", "use": "sig"})
    return {"keys": listed, "public_cert": {"kid": keys[0][0], "cert": "…"}, "public_certs": []}


KEY = new_key()


def token(private=KEY, *, key_id="k1", algorithm="RS256", **changes) -> str:
    """A token as Cloudflare Access signs one for a visitor it let through."""
    now = int(time.time())
    claims = {
        "aud": [AUD],
        "email": "owner@example.com",
        "exp": now + 3600,
        "iat": now,
        "nbf": now,
        "iss": f"https://{TEAM}",
        "type": "app",
        "sub": "7335d417-61da-459d-899c-0a01c76a2f94",
        **changes,
    }
    return jwt.encode(claims, private, algorithm=algorithm, headers={"kid": key_id})


class Cloudflare:
    """A stand-in for the team's key endpoint. Counts how often it is asked."""

    def __init__(self, certs: dict | None = None, *, down: bool = False):
        self.certs = certs or published(("k1", KEY))
        self.down = down
        self.asked = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url) == CERTS
        self.asked += 1
        if self.down:
            raise httpx.ConnectError("unreachable")
        return httpx.Response(200, json=self.certs)


def guarded(tmp_path, cloudflare: Cloudflare | None = None, **config) -> TestClient:
    cfg = make_config(tmp_path, host="0.0.0.0", access_team_domain=TEAM, access_audience=AUD, **config)
    checker = CloudflareAccess(
        TEAM, AUD, client=httpx.AsyncClient(transport=httpx.MockTransport(cloudflare or Cloudflare()))
    )
    return TestClient(create_app(cfg, access=checker), base_url="https://oscar.example.test")


def signed(value: str) -> dict:
    return {"Cf-Access-Jwt-Assertion": value}


# Who gets in


def test_a_visitor_cloudflare_let_through_gets_in(tmp_path):
    with guarded(tmp_path) as client:
        for path in ["/", "/api/agent", "/api/calls", "/api/status", "/static/app.js"]:
            assert client.get(path, headers=signed(token())).status_code == 200, path


def test_without_a_sign_in_nothing_but_the_open_paths_answers(tmp_path):
    # As someone who goes straight to the server's address and port would find it.
    with guarded(tmp_path) as client:
        for path in ["/", "/api/agent", "/api/calls", "/api/status", "/static/app.js", "/api/events"]:
            response = client.get(path)
            assert (response.status_code, response.text) == (403, REFUSED), path
        assert client.put("/api/agent", json={}).status_code == 403
        assert client.post("/api/calls/browser", json={"sdp": "x", "type": "offer"}).status_code == 403
        assert client.post("/api/calls/phone", json={"to": "+14155550123"}).status_code == 403


@pytest.mark.parametrize(
    "forged",
    [
        token(new_key()),  # signed with a key that is not Cloudflare's
        token(aud=["another-application"]),  # made for another application
        token(iss="https://someone-else.cloudflareaccess.com"),  # by another team
        token(exp=int(time.time()) - 3600),  # expired an hour ago
        token(key_id="unknown"),  # a key Cloudflare does not list
        "not a token",
        jwt.encode({"aud": [AUD], "iss": f"https://{TEAM}", "exp": int(time.time()) + 60}, None, algorithm="none"),
    ],
    ids=["other key", "other application", "other team", "expired", "unknown key", "garbage", "unsigned"],
)
def test_a_token_cloudflare_did_not_give_is_refused(tmp_path, forged):
    with guarded(tmp_path) as client:
        response = client.get("/api/agent", headers=signed(forged))
    assert (response.status_code, response.text) == (403, REFUSED)


def test_a_token_without_an_expiry_is_refused(tmp_path):
    claims = {"aud": [AUD], "iss": f"https://{TEAM}", "email": "owner@example.com"}
    forever = jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": "k1"})
    with guarded(tmp_path) as client:
        assert client.get("/api/agent", headers=signed(forever)).status_code == 403


def test_the_health_check_and_twilios_webhooks_stay_open(tmp_path):
    with guarded(tmp_path) as client:
        assert client.get("/healthz").json() == {"ok": True, "calls": 0}
        # Reached Oscar (whose own Twilio signature check refuses it), not the sign-in check.
        response = client.post("/twilio/voice", data={"From": "+14155550123"})
        assert response.status_code == 403 and response.text != REFUSED


def test_a_websocket_without_a_sign_in_is_closed(tmp_path):
    with guarded(tmp_path) as client:
        with pytest.raises(WebSocketDisconnect) as closed:
            with client.websocket_connect("/api/anything"):
                pass
    assert closed.value.code == 1008


# Cloudflare's keys


def test_the_keys_are_fetched_once_and_kept(tmp_path):
    cloudflare = Cloudflare()
    with guarded(tmp_path, cloudflare) as client:
        for _ in range(5):
            assert client.get("/api/agent", headers=signed(token())).status_code == 200
    assert cloudflare.asked == 1


def test_a_new_signing_key_is_fetched_when_cloudflare_changes_keys(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(access_module.time, "monotonic", lambda: clock[0])
    new = new_key()
    cloudflare = Cloudflare()
    with guarded(tmp_path, cloudflare) as client:
        assert client.get("/api/agent", headers=signed(token())).status_code == 200
        cloudflare.certs = published(("k2", new))  # Cloudflare moved to a new key, and dropped the old
        clock[0] += access_module.REFETCH_AFTER_SECONDS
        assert client.get("/api/agent", headers=signed(token(new, key_id="k2"))).status_code == 200
        assert client.get("/api/agent", headers=signed(token())).status_code == 403  # the dropped key
    assert cloudflare.asked == 2


def test_made_up_key_ids_do_not_have_oscar_asking_cloudflare_all_the_time(tmp_path):
    cloudflare = Cloudflare()
    with guarded(tmp_path, cloudflare) as client:
        for number in range(10):
            assert client.get("/api/agent", headers=signed(token(new_key(), key_id=f"made-up-{number}"))).status_code == 403
    assert cloudflare.asked == 1


def test_when_the_keys_cannot_be_had_nobody_gets_in_and_it_says_why(tmp_path):
    cloudflare = Cloudflare(down=True)
    with guarded(tmp_path, cloudflare) as client:
        response = client.get("/api/agent", headers=signed(token()))
        assert (response.status_code, response.text) == (503, UNAVAILABLE)
        cloudflare.down = False  # back: the next request tries again
        assert client.get("/api/agent", headers=signed(token())).status_code == 200


# Settings


def test_without_access_settings_the_dashboard_is_as_before(tmp_path):
    cfg = make_config(tmp_path, host="0.0.0.0")
    assert not cfg.access_required
    with TestClient(create_app(cfg)) as client:
        assert client.get("/api/agent", headers={"host": "203.0.113.10:7860"}).status_code == 200


@pytest.fixture
def env(monkeypatch):
    for name in ["CF_ACCESS_TEAM_DOMAIN", "CF_ACCESS_AUD", "PUBLIC_URL", "HOST", "PORT"]:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_access_is_read_from_the_environment(env):
    env.setenv("CF_ACCESS_TEAM_DOMAIN", f"https://{TEAM.upper()}/")
    env.setenv("CF_ACCESS_AUD", f" {AUD} ")
    cfg = load_config()
    assert (cfg.access_team_domain, cfg.access_audience, cfg.access_required) == (TEAM, AUD, True)


@pytest.mark.parametrize("only", ["CF_ACCESS_TEAM_DOMAIN", "CF_ACCESS_AUD"])
def test_half_the_access_settings_is_refused(env, only):
    env.setenv(only, TEAM if only == "CF_ACCESS_TEAM_DOMAIN" else AUD)
    with pytest.raises(ConfigError, match="both CF_ACCESS_TEAM_DOMAIN and CF_ACCESS_AUD"):
        load_config()


@pytest.mark.parametrize(
    "wrong",
    ["oscar.example.com", "https://oscar.example.com", f"{TEAM}/cdn-cgi/access/certs", f"{TEAM}.attacker.example"],
)
def test_a_team_domain_that_is_not_one_is_refused(wrong):
    with pytest.raises(ConfigError, match="yourteam.cloudflareaccess.com"):
        access_team_domain(wrong)
