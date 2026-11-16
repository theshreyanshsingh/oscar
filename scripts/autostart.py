"""Have Oscar start by itself when you log in to this Mac, and stay running.

    .venv/bin/python -m scripts.autostart             # set it up, and start Oscar now
    .venv/bin/python -m scripts.autostart --status    # is it set up, and is Oscar answering?
    .venv/bin/python -m scripts.autostart --restart   # after changing Oscar's code or its .env
    .venv/bin/python -m scripts.autostart --remove    # stop Oscar, and do not start it at login

macOS does the work, through a "launch agent" this writes to
~/Library/LaunchAgents/local.oscar.server.plist: Oscar is started when you log
in, and started again if it ever stops. What Oscar prints goes to
~/Library/Logs/Oscar/oscar.log.

Nothing here cuts off a call that is going: end it first, or add --force.
"""

import argparse
import os
import plistlib
import re
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from app.config import ROOT_DIR, Config, ConfigError, load_config

LABEL = "local.oscar.server"
# Oscar answers a few seconds after it starts (it loads its turn-taking models first).
START_PATIENCE_SECONDS = 45
# macOS gives a job 20 seconds to stop before it kills it.
STOP_PATIENCE_SECONDS = 25


class CannotDo(Exception):
    """What was asked for could not be done. The message is for the person at the terminal."""


def agent_file(home: Path) -> Path:
    return home / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def log_file(home: Path) -> Path:
    return home / "Library" / "Logs" / "Oscar" / "oscar.log"


def launch_agent(root: Path, home: Path) -> dict:
    """What macOS is told about the job."""
    log = str(log_file(home))
    return {
        "Label": LABEL,
        # The virtual environment's python, by this very path: that is how
        # Python finds the packages installed for Oscar.
        "ProgramArguments": [str(root / ".venv" / "bin" / "python"), "-m", "app"],
        "WorkingDirectory": str(root),
        "RunAtLoad": True,  # at login
        "KeepAlive": True,  # and again whenever it stops
        # A call's audio is handled every 20 milliseconds: macOS must not slow
        # the job down the way it does background work.
        "ProcessType": "Interactive",
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
    }


def service() -> str:
    """The job's name as launchctl takes it: in this user's own login session."""
    return f"gui/{os.getuid()}/{LABEL}"


def launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def job_state() -> tuple[bool, int | None]:
    """Whether macOS has the job, and the process it is running as (None when it is not running)."""
    result = launchctl("print", service())
    if result.returncode != 0:
        return False, None
    pid = re.search(r"^\s*pid = (\d+)\s*$", result.stdout, flags=re.MULTILINE)
    return True, int(pid.group(1)) if pid else None


def address(cfg: Config) -> str:
    return f"http://{cfg.host}:{cfg.port}"


def is_answering(cfg: Config) -> bool:
    try:
        return httpx.get(f"{address(cfg)}/healthz", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def calls_in_progress(cfg: Config) -> int:
    """How many calls Oscar is on right now (none, if it is not answering at all)."""
    try:
        status = httpx.get(f"{address(cfg)}/api/status", timeout=3).json()
        return len(status["active_call_ids"])
    except (httpx.HTTPError, ValueError, LookupError, TypeError):
        return 0


def port_in_use(cfg: Config) -> bool:
    with socket.socket() as probe:
        probe.settimeout(1)
        return probe.connect_ex((cfg.host, cfg.port)) == 0


def wait_until(check: Callable[[], bool], seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.5)
    return False


def not_during_a_call(cfg: Config, force: bool) -> None:
    if not force and calls_in_progress(cfg):
        raise CannotDo("Oscar is on a call. End the call first, or add --force to cut it off.")


def stop_job() -> None:
    """Have macOS stop the job and forget it (until the next login, if its file stays)."""
    launchctl("bootout", service())
    if not wait_until(lambda: not job_state()[0], STOP_PATIENCE_SECONDS):
        raise CannotDo("macOS did not stop Oscar. Try again in a minute.")


def start_job(cfg: Config, home: Path) -> None:
    """Hand the job to macOS and wait for Oscar to answer."""
    log = log_file(home)
    # The log can name what a caller asked Oscar to look up: for its owner only,
    # like the call history.
    log.parent.mkdir(parents=True, exist_ok=True)
    log.parent.chmod(0o700)
    log.touch()
    log.chmod(0o600)
    started = launchctl("bootstrap", f"gui/{os.getuid()}", str(agent_file(home)))
    if started.returncode != 0:
        raise CannotDo(f"macOS would not take the job: {started.stderr.strip() or started.stdout.strip()}")
    if not wait_until(lambda: is_answering(cfg), START_PATIENCE_SECONDS):
        raise CannotDo(
            f"Oscar was started but is not answering at {address(cfg)}. Its log says why: {log}\n"
            "If macOS asked whether python may use files in your Desktop folder, choose Allow, "
            "then run this again with --restart."
        )


def set_up(cfg: Config, *, root: Path = ROOT_DIR, home: Path, force: bool = False) -> str:
    """Write the launch agent and start Oscar under it. Run again, it brings the job up to date."""
    if job_state()[0]:
        not_during_a_call(cfg, force)
        stop_job()
    elif port_in_use(cfg):
        raise CannotDo(
            f"Something is already answering on port {cfg.port}: most likely Oscar, started "
            "another way. Stop it first (Ctrl+C in its terminal), then run this again."
        )
    file = agent_file(home)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(plistlib.dumps(launch_agent(root, home)))
    start_job(cfg, home)
    return f"Oscar now starts by itself when you log in, and it is running: {address(cfg)}"


def restart(cfg: Config, *, home: Path, force: bool = False) -> str:
    if not job_state()[0]:
        raise CannotDo(
            "Oscar is not set up to start by itself. Run this without --restart to set it up."
        )
    not_during_a_call(cfg, force)
    stop_job()
    start_job(cfg, home)
    return f"Oscar was restarted and is running: {address(cfg)}"


def remove(cfg: Config, *, home: Path, force: bool = False) -> str:
    file = agent_file(home)
    loaded = job_state()[0]
    if not loaded and not file.exists():
        return "Oscar was not set up to start by itself. Nothing to remove."
    if loaded:
        not_during_a_call(cfg, force)
        stop_job()
    file.unlink(missing_ok=True)
    return "Oscar is stopped and no longer starts by itself. Start it by hand with: .venv/bin/python -m app"


def status(cfg: Config, *, home: Path) -> str:
    loaded, pid = job_state()
    if not loaded:
        set_up_line = "Set up to start by itself: no"
        if agent_file(home).exists():
            set_up_line += " (its file is there, so it will be from your next login)"
        running = "Running: no"
    else:
        set_up_line = "Set up to start by itself: yes"
        running = f"Running: yes (process {pid})" if pid else "Running: no (macOS is starting it again)"
    answering = "yes" if is_answering(cfg) else "no"
    return "\n".join(
        [set_up_line, running, f"Answering at {address(cfg)}: {answering}", f"Log: {log_file(home)}"]
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    what = parser.add_mutually_exclusive_group()
    what.add_argument("--status", action="store_true", help="say whether it is set up and running")
    what.add_argument("--restart", action="store_true", help="restart Oscar (after changing its code or .env)")
    what.add_argument("--remove", action="store_true", help="stop Oscar and do not start it at login")
    parser.add_argument("--force", action="store_true", help="go ahead even if a call is in progress")
    args = parser.parse_args(argv)
    try:
        if sys.platform != "darwin":
            raise CannotDo("This sets up a macOS launch agent, so it only works on a Mac.")
        try:
            cfg = load_config()
        except ConfigError as exc:
            raise CannotDo(f"Oscar's settings need fixing first: {exc}") from None
        home = Path.home()
        if args.status:
            print(status(cfg, home=home))
        elif args.restart:
            print(restart(cfg, home=home, force=args.force))
        elif args.remove:
            print(remove(cfg, home=home, force=args.force))
        else:
            print(set_up(cfg, home=home, force=args.force))
    except CannotDo as problem:
        print(problem, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
