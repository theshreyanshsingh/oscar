"""Save your keys and settings into .env without secrets showing on screen.

    .venv/bin/python -m scripts.set_keys            # OpenRouter, and Firecrawl for web search
    .venv/bin/python -m scripts.set_keys --phone    # Twilio and a dashboard password, for phone calls

OpenRouter's key is needed. Everything else is optional. Press Enter at a
prompt to keep the value already saved.
"""

import argparse
import getpass
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass

from app.config import ROOT_DIR


def no_spaces(value: str) -> str:
    return "That has a space in it." if re.search(r"\s", value) else ""


def account_sid(value: str) -> str:
    if re.fullmatch(r"AC[0-9a-fA-F]{32}", value):
        return ""
    return "An Account SID starts with AC and is 34 characters long."


def phone_number(value: str) -> str:
    if re.fullmatch(r"\+[1-9]\d{6,14}", value):
        return ""
    return "Write the number in international format, such as +14155550123."


def password(value: str) -> str:
    if len(value) < 8:
        return "Use at least 8 characters."
    return no_spaces(value)


@dataclass(frozen=True)
class Setting:
    name: str
    label: str
    secret: bool = True  # typed without showing on screen
    problem: Callable[[str], str] = no_spaces  # why a value will not do, or ""


KEYS = [
    Setting("OPENROUTER_API_KEY", "OpenRouter API key (https://openrouter.ai/keys)"),
    Setting("FIRECRAWL_API_KEY", "Firecrawl API key, for web search (optional; https://firecrawl.dev)"),
]

# From https://console.twilio.com: Account SID and Auth Token are under
# "Account Info" on the first page; the number is under Phone Numbers.
PHONE = [
    Setting("TWILIO_ACCOUNT_SID", "Twilio Account SID", secret=False, problem=account_sid),
    Setting("TWILIO_AUTH_TOKEN", "Twilio Auth Token"),
    Setting(
        "TWILIO_PHONE_NUMBER",
        "Your Twilio phone number, like +14155550123",
        secret=False,
        problem=phone_number,
    ),
    Setting(
        "DASHBOARD_PASSWORD",
        "A password for the dashboard, which phone calls put on the internet (you choose it)",
        problem=password,
    ),
]


def with_value(text: str, name: str, value: str) -> str:
    """The .env text with `name` set to `value`, replacing its line or adding one."""
    line = f"{name}={value}"
    pattern = re.compile(rf"^{re.escape(name)}=.*$", flags=re.MULTILINE)
    if pattern.search(text):
        return pattern.sub(lambda _: line, text, count=1)
    return text.rstrip("\n") + f"\n{line}\n"


def ask(setting: Setting) -> str:
    """What the user typed for a setting: a usable value, or "" to keep what is saved."""
    while True:
        if setting.secret:
            value = getpass.getpass(f"{setting.label}, then Enter (typing is hidden): ").strip()
        else:
            value = input(f"{setting.label}, then Enter: ").strip()
        problem = setting.problem(value) if value else ""
        if not problem:
            return value
        print(f"  {problem} Try again, or press Enter to skip.")


def main(settings: list[Setting]) -> None:
    env_file = ROOT_DIR / ".env"
    if not env_file.exists():
        shutil.copyfile(ROOT_DIR / ".env.example", env_file)
    env_file.chmod(0o600)
    text = env_file.read_text()

    for setting in settings:
        value = ask(setting)
        if not value:
            print(f"  {setting.name}: kept as it was")
            continue
        text = with_value(text, setting.name, value)
        shown = f"{len(value)} characters" if setting.secret else value
        print(f"  {setting.name}: saved ({shown})")

    env_file.write_text(text)
    print(f"Saved to {env_file}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--phone", action="store_true", help="the settings phone calls need")
    main(PHONE if parser.parse_args().phone else KEYS)
