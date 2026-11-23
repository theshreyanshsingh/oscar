import dataclasses
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.main import create_app
from app.numbers import twilio_client as real_twilio_client  # before no_real_twilio replaces it

PUBLIC_URL = "https://oscar.example.test"
TWILIO_AUTH_TOKEN = "test-twilio-auth-token"
# The dashboard's password in tests (DASHBOARD_PASSWORD): test clients sign in with it.
DASHBOARD_PASSWORD = "test-dashboard-password"


def make_config(tmp_path: Path, **overrides) -> Config:
    base = Config(
        openrouter_api_key="test-openrouter-key",
        twilio_account_sid="AC" + "0" * 32,
        twilio_auth_token=TWILIO_AUTH_TOKEN,
        twilio_phone_number="+15005550006",
        public_url=PUBLIC_URL,
        host="127.0.0.1",
        port=0,
        db_path=tmp_path / "oscar-test.db",
        dashboard_password=DASHBOARD_PASSWORD,
    )
    return dataclasses.replace(base, **overrides)


def sign_in(client: TestClient, password: str = DASHBOARD_PASSWORD, **request) -> TestClient:
    """Signs a test client in at /login, as the owner does in the browser."""
    response = client.post("/login", data={"password": password}, follow_redirects=False, **request)
    assert response.status_code == 303, response.text
    return client


def local_client(cfg: Config, **options) -> TestClient:
    """A test client that talks to the app the way a local browser does (Host: localhost),
    signed in when the dashboard asks for its password. `options` go to create_app."""
    client = TestClient(create_app(cfg, **options), base_url="http://localhost")
    if cfg.dashboard_mode == "password":
        sign_in(client)
    return client


@asynccontextmanager
async def dashboard_http(base: str, password: str = DASHBOARD_PASSWORD):
    """An HTTP client signed in to an Oscar running at `base`."""
    async with httpx.AsyncClient(base_url=base, timeout=30) as http:
        response = await http.post("/login", data={"password": password})
        assert response.status_code == 303, response.text
        yield http


@pytest.fixture
def cfg(tmp_path) -> Config:
    return make_config(tmp_path)


@pytest.fixture
def client(cfg):
    with local_client(cfg) as test_client:
        yield test_client


class NoKeyCredit:
    """Stands in for app.costs.KeyCredit: OpenRouter never says, so no call is held back.

    Keeps tests off the network. A test about spending passes its own KeyCredit
    to create_app.
    """

    def __init__(self, **options):
        pass

    async def get(self, *, fresh: bool = False):
        return None

    async def too_little_for_a_call(self) -> str:
        return ""

    async def aclose(self) -> None:
        pass


@pytest.fixture(autouse=True)
def offline_key_credit(monkeypatch):
    monkeypatch.setattr("app.main.KeyCredit", NoKeyCredit)


class NoModelCatalog:
    """Stands in for app.catalog.ModelCatalog: no models are known. Keeps tests off
    the network; a test about the list passes its own ModelCatalog to create_app."""

    def __init__(self, **options):
        pass

    async def get(self) -> list[dict]:
        return []

    async def aclose(self) -> None:
        pass


@pytest.fixture(autouse=True)
def offline_model_catalog(monkeypatch):
    monkeypatch.setattr("app.main.ModelCatalog", NoModelCatalog)


class _NoTwilio:
    """An HTTP client for the Twilio library that never reaches Twilio."""

    def __init__(self):
        import logging

        super().__init__(logging.getLogger("no-twilio"), is_async=False)

    def request(self, *args, **kwargs):
        raise ConnectionError("Tests do not reach the real Twilio.")


@pytest.fixture(autouse=True)
def no_real_twilio(monkeypatch):
    """No test reaches Twilio's real servers (Oscar looks at the account's numbers when it
    starts, for one): tests that need Twilio put their own stand-in in its place."""
    from twilio.http import HttpClient
    from twilio.rest import Client

    from app import numbers, phone

    class NoTwilio(_NoTwilio, HttpClient):
        pass

    def client(cfg: Config) -> Client:
        return Client(cfg.twilio_account_sid, cfg.twilio_auth_token, http_client=NoTwilio())

    monkeypatch.setattr(numbers, "twilio_client", client)
    monkeypatch.setattr(phone, "_twilio_client", client)
