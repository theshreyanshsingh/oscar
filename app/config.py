"""Settings read from the environment (and the project's .env file).

See .env.example for what each value does.
"""

import os
import re
import sys
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
    host: str
    port: int
    db_path: Path
    openrouter_base_url: str = DEFAULT_BASE_URL
    firecrawl_api_key: str = ""
    firecrawl_base_url: str = DEFAULT_FIRECRAWL_URL
    # Cloudflare Access, when it guards the dashboard (app/access.py): the team
    # domain, such as "yourteam.cloudflareaccess.com", and the application's
    # audience (AUD) tag. Both or neither.
    access_team_domain: str = ""
    access_audience: str = ""

    @property
    def local_only(self) -> bool:
        """Whether only this computer can open Oscar (HOST=127.0.0.1 or localhost).

        Otherwise, such as with HOST=0.0.0.0 on a server, anyone who can reach the
        port can use the dashboard: there is no password, so the server's firewall
        decides who can.
        """
        return self.host in LOCAL_ONLY_HOSTS

    @property
    def access_required(self) -> bool:
        """Whether only people signed in through Cloudflare Access may use the dashboard."""
        return bool(self.access_team_domain and self.access_audience)

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

# Where Linux keeps the range of ports it hands out to programs that ask for any port.
LINUX_PORT_RANGE = Path("/proc/sys/net/ipv4/ip_local_port_range")


def browser_call_udp_ports() -> tuple[int, int]:
    """The UDP ports a browser call's audio arrives on (first and last).

    The audio goes straight between the browser and this machine, not through
    the dashboard's web address (so not through a proxy such as Cloudflare). Each
    call takes ports the system picks from its range for programs that ask for
    any port, so on a server that whole range has to be let in.
    """
    try:
        low, high = LINUX_PORT_RANGE.read_text().split()
        return int(low), int(high)
    except (OSError, ValueError):
        # Linux's own default, or a Mac's.
        return (32768, 60999) if sys.platform == "linux" else (49152, 65535)


def call_audio_unreachable(cfg: Config) -> str:
    """What to tell the owner when a browser call's audio never connected."""
    if cfg.local_only:
        return (
            "The call's audio could not connect. It goes straight between the browser and "
            "the computer running Oscar, so make browser calls on that computer."
        )
    low, high = browser_call_udp_ports()
    return (
        "The call's audio could not reach Oscar's server. It goes straight to the server "
        f"over UDP, not through the web address: let in UDP ports {low}-{high} in the "
        "server's firewall (on EC2, its security group), then call again."
    )


def load_config() -> Config:
    public_url = _env("PUBLIC_URL").rstrip("/")
    host = _env("HOST", "127.0.0.1")

    if public_url and not public_url.startswith("https://"):
        raise ConfigError("PUBLIC_URL must start with https:// (Twilio requires a secure URL).")

    port_raw = _env("PORT", "7860")
    try:
        port = int(port_raw)
    except ValueError:
        raise ConfigError(f"PORT must be a number, got {port_raw!r}.") from None

    db_path = Path(_env("DATABASE_PATH") or ROOT_DIR / "data" / "oscar.db")

    team_domain = access_team_domain(_env("CF_ACCESS_TEAM_DOMAIN"))
    audience = _env("CF_ACCESS_AUD")
    if bool(team_domain) != bool(audience):
        raise ConfigError(
            "Cloudflare Access needs both CF_ACCESS_TEAM_DOMAIN and CF_ACCESS_AUD in .env "
            "(or neither, to leave the dashboard open)."
        )

    return Config(
        openrouter_api_key=_env("OPENROUTER_API_KEY"),
        twilio_account_sid=_env("TWILIO_ACCOUNT_SID"),
        twilio_auth_token=_env("TWILIO_AUTH_TOKEN"),
        twilio_phone_number=_env("TWILIO_PHONE_NUMBER"),
        public_url=public_url,
        host=host,
        port=port,
        db_path=db_path,
        openrouter_base_url=_env("OPENROUTER_BASE_URL").rstrip("/") or DEFAULT_BASE_URL,
        firecrawl_api_key=_env("FIRECRAWL_API_KEY"),
        firecrawl_base_url=_env("FIRECRAWL_BASE_URL").rstrip("/") or DEFAULT_FIRECRAWL_URL,
        access_team_domain=team_domain,
        access_audience=audience,
    )


def access_team_domain(value: str) -> str:
    """The team domain as Cloudflare writes it ("yourteam.cloudflareaccess.com"), from what was given.

    "https://yourteam.cloudflareaccess.com/" is taken too. Anything that is not a
    Cloudflare Access team domain is refused: the dashboard's own address in its
    place would let nobody in, without saying why.
    """
    domain = value.strip().removeprefix("https://").removeprefix("http://").rstrip("/").lower()
    if not domain:
        return ""
    if "/" in domain or not re.fullmatch(r"[a-z0-9-]+\.cloudflareaccess\.com", domain):
        raise ConfigError(
            f"CF_ACCESS_TEAM_DOMAIN must be your Cloudflare Access team domain, like "
            f"yourteam.cloudflareaccess.com (got {value.strip()!r})."
        )
    return domain
