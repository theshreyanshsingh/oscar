"""Settings read from the environment (and the project's .env file).

See .env.example for what each value does.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from app.openrouter import DEFAULT_BASE_URL
from app.search import DEFAULT_FIRECRAWL_URL

ROOT_DIR = Path(__file__).resolve().parent.parent

load_dotenv(ROOT_DIR / ".env", override=False)


class ConfigError(RuntimeError):
    """The environment is set up in a way the app must refuse to run with."""


@dataclass(frozen=True)
class Config:
    openrouter_api_key: str
    twilio_account_sid: str
    twilio_auth_token: str
    twilio_phone_number: str
    public_url: str
    dashboard_password: str
    host: str
    port: int
    db_path: Path
    openrouter_base_url: str = DEFAULT_BASE_URL
    firecrawl_api_key: str = ""
    firecrawl_base_url: str = DEFAULT_FIRECRAWL_URL

    @property
    def search_enabled(self) -> bool:
        """Whether Oscar can search the web, on calls and in WhatsApp (it needs a Firecrawl key)."""
        return bool(self.firecrawl_api_key)

    def missing_agent_keys(self) -> list[str]:
        """Keys the agent needs before it can take any call.

        One key covers everything: OpenRouter serves the model and the speech.
        """
        return [] if self.openrouter_api_key else ["OPENROUTER_API_KEY"]

    def missing_phone_settings(self) -> list[str]:
        """Settings real phone calls need on top of the agent keys."""
        required = {
            "TWILIO_ACCOUNT_SID": self.twilio_account_sid,
            "TWILIO_AUTH_TOKEN": self.twilio_auth_token,
            "TWILIO_PHONE_NUMBER": self.twilio_phone_number,
            "PUBLIC_URL": self.public_url,
        }
        return [name for name, value in required.items() if not value]

    @property
    def phone_enabled(self) -> bool:
        return not self.missing_phone_settings()

    @property
    def media_stream_url(self) -> str:
        """WebSocket URL Twilio streams call audio to."""
        return "wss://" + self.public_url.removeprefix("https://") + "/twilio/media"


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


LOCAL_ONLY_HOSTS = {"127.0.0.1", "localhost"}


def load_config() -> Config:
    public_url = _env("PUBLIC_URL").rstrip("/")
    dashboard_password = _env("DASHBOARD_PASSWORD")
    host = _env("HOST", "127.0.0.1")

    if host not in LOCAL_ONLY_HOSTS and not dashboard_password:
        raise ConfigError(
            f"HOST={host} lets other devices on your network open the dashboard, so set "
            "DASHBOARD_PASSWORD too (or use HOST=127.0.0.1)."
        )

    if public_url and not public_url.startswith("https://"):
        raise ConfigError("PUBLIC_URL must start with https:// (Twilio requires a secure URL).")
    if public_url and not dashboard_password:
        raise ConfigError(
            "PUBLIC_URL makes this server reachable from the internet, so set "
            "DASHBOARD_PASSWORD too. Otherwise anyone could open the dashboard "
            "and place calls on your Twilio account."
        )

    port_raw = _env("PORT", "7860")
    try:
        port = int(port_raw)
    except ValueError:
        raise ConfigError(f"PORT must be a number, got {port_raw!r}.") from None

    db_path = Path(_env("DATABASE_PATH") or ROOT_DIR / "data" / "oscar.db")

    return Config(
        openrouter_api_key=_env("OPENROUTER_API_KEY"),
        twilio_account_sid=_env("TWILIO_ACCOUNT_SID"),
        twilio_auth_token=_env("TWILIO_AUTH_TOKEN"),
        twilio_phone_number=_env("TWILIO_PHONE_NUMBER"),
        public_url=public_url,
        dashboard_password=dashboard_password,
        host=host,
        port=port,
        db_path=db_path,
        openrouter_base_url=_env("OPENROUTER_BASE_URL").rstrip("/") or DEFAULT_BASE_URL,
        firecrawl_api_key=_env("FIRECRAWL_API_KEY"),
        firecrawl_base_url=_env("FIRECRAWL_BASE_URL").rstrip("/") or DEFAULT_FIRECRAWL_URL,
    )
