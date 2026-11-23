"""Save your keys and settings into .env without secrets showing on screen.

    .venv/bin/python -m scripts.set_keys              # OpenRouter, and Firecrawl for web search
    .venv/bin/python -m scripts.set_keys --phone      # Twilio, for phone calls
    .venv/bin/python -m scripts.set_keys --password   # the dashboard's password

OpenRouter's key is needed. Everything else is optional. Press Enter at a
prompt to keep the value already saved. A new password takes effect when
Oscar restarts, and signs everyone out.
"""

import argparse
import getpass
import io
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

# Oscar's folder. (Not taken from app.config: this has to work straight after a
# git pull, before deploy.sh has installed the new code's packages.)
ROOT_DIR = Path(__file__).resolve().parent.parent

# The dashboard's password is at least this long: anyone who finds the address can try it.
PASSWORD_LEAST = 10


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


def password_problem(value: str) -> str:
    if len(value) < PASSWORD_LEAST:
        return f"Use at least {PASSWORD_LEAST} characters."
    if "${" in value:
        return "Leave out ${ (the .env file reads it as the name of another setting)."
    return ""


def env_quoted(value: str) -> str:
    """A value as .env keeps it whatever it holds (spaces, #, quotes): in single quotes."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


@dataclass(frozen=True)
class Setting:
    name: str
    label: str
    secret: bool = True  # typed without showing on screen
    problem: Callable[[str], str] = no_spaces  # why a value will not do, or ""
    twice: bool = False  # typed a second time, to catch a slip nobody could see
    quoted: bool = False  # saved in quotes, so any character is kept as typed


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
        "The number Oscar calls from, like +14155550123 (optional, and a number chosen on the dashboard comes first)",
        secret=False,
        problem=phone_number,
    ),
]

# The dashboard's password (app/signin.py).
PASSWORD = [
    Setting(
        "DASHBOARD_PASSWORD",
        f"A password for the dashboard (at least {PASSWORD_LEAST} characters)",
        problem=password_problem,
        twice=True,
        quoted=True,
    ),
]


def with_value(text: str, name: str, value: str) -> str:
    """The .env text with `name` set to `value`, replacing its line or adding one.

    Any later line for `name` is dropped: Oscar reads the last one, so a value
    left there would be used instead. A line written by hand as
    `export NAME=...` or `NAME = ...` counts as one of its lines.
    """
    line = f"{name}={value}"
    is_its_line = re.compile(rf"[ \t]*(export[ \t]+)?{re.escape(name)}[ \t]*=").match
    lines, replaced = [], False
    for each in text.splitlines(keepends=True):
        if not is_its_line(each):
            lines.append(each)
        elif not replaced:
            lines.append(line + each[len(each.rstrip("\r\n")) :])  # keeps the line's own ending
            replaced = True
    if replaced:
        return "".join(lines)
    return text.rstrip("\n") + f"\n{line}\n"


def ask(setting: Setting) -> str:
    """What the user typed for a setting: a usable value, or "" to keep what is saved."""
    while True:
        if setting.secret:
            value = getpass.getpass(f"{setting.label}, then Enter (typing is hidden): ").strip()
        else:
            value = input(f"{setting.label}, then Enter: ").strip()
        problem = setting.problem(value) if value else ""
        if not problem and value and setting.twice:
            again = getpass.getpass("The same again, to check it, then Enter: ").strip()
            if again != value:
                problem = "The two were not the same."
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
        saved = with_value(text, setting.name, env_quoted(value) if setting.quoted else value)
        if dotenv_values(stream=io.StringIO(saved)).get(setting.name) != value:
            # Never save a value Oscar would read back as something else.
            print(f"  {setting.name}: not saved (the .env file would not keep it as typed)")
            continue
        text = saved
        shown = f"{len(value)} characters" if setting.secret else value
        print(f"  {setting.name}: saved ({shown})")

    env_file.write_text(text)
    print(f"Saved to {env_file}")


def restart_note(settings: list[Setting]) -> str:
    if settings is PASSWORD:
        return (
            "Restart Oscar so it asks for this password (everyone signed in is signed out): "
            "pm2 restart oscar on a server, or .venv/bin/python -m scripts.autostart --restart on a Mac."
        )
    return "Restart Oscar so it reads them."


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--phone", action="store_true", help="the settings phone calls need")
    which.add_argument("--password", action="store_true", help="the dashboard's password")
    args = parser.parse_args()
    chosen = PHONE if args.phone else PASSWORD if args.password else KEYS
    main(chosen)
    print(restart_note(chosen))
