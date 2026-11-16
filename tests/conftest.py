import dataclasses
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.main import create_app

PUBLIC_URL = "https://oscar.example.test"
TWILIO_AUTH_TOKEN = "test-twilio-auth-token"


def make_config(tmp_path: Path, **overrides) -> Config:
    base = Config(
        openrouter_api_key="test-openrouter-key",
        twilio_account_sid="AC" + "0" * 32,
        twilio_auth_token=TWILIO_AUTH_TOKEN,
        twilio_phone_number="+15005550006",
        public_url=PUBLIC_URL,
        dashboard_password="",
        host="127.0.0.1",
        port=0,
        db_path=tmp_path / "oscar-test.db",
    )
    return dataclasses.replace(base, **overrides)


def local_client(cfg: Config) -> TestClient:
    """A test client that talks to the app the way a local browser does (Host: localhost)."""
    return TestClient(create_app(cfg), base_url="http://localhost")


@pytest.fixture
def cfg(tmp_path) -> Config:
    return make_config(tmp_path)


@pytest.fixture
def client(cfg):
    with local_client(cfg) as test_client:
        yield test_client
