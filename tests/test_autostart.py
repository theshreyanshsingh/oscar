"""Starting Oscar at login (scripts/autostart.py). macOS's launchctl is faked."""

import plistlib
import socket
import subprocess
import sys

import httpx
import pytest

from app.config import ConfigError
from scripts import autostart
from scripts.autostart import (
    LABEL,
    CannotDo,
    agent_file,
    job_state,
    launch_agent,
    log_file,
    remove,
    restart,
    set_up,
    status,
)
from tests.conftest import make_config

SERVICE = f"gui/501/{LABEL}"


def done(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class Launchd:
    """A stand-in for launchctl that remembers whether it has the job, and what it was asked."""

    def __init__(self, *, loaded: bool = False, pid: int | None = 4321):
        self.loaded = loaded
        self.pid = pid
        self.asked: list[tuple[str, ...]] = []
        self.refuses_the_job = ""  # what bootstrap says when it will not take the job
        self.never_stops = False
        self.loaded_from: dict | None = None  # the job as it was read at bootstrap

    def __call__(self, *args: str) -> subprocess.CompletedProcess:
        self.asked.append(args)
        verb = args[0]
        if verb == "print":
            if not self.loaded:
                return done(113, stderr=f'Could not find service "{LABEL}" in domain for user gui: 501')
            running = f"\tstate = running\n\tpid = {self.pid}\n" if self.pid else "\tstate = spawn scheduled\n"
            return done(stdout=f"{args[1]} = {{\n\tactive count = 1\n{running}\tlast exit code = 0\n}}\n")
        if verb == "bootout":
            if not self.loaded:
                return done(3, stderr="Boot-out failed: 3: No such process")
            self.loaded = self.never_stops
            return done()
        if verb == "bootstrap":
            if self.refuses_the_job:
                return done(5, stderr=self.refuses_the_job)
            with open(args[2], "rb") as file:
                self.loaded_from = plistlib.load(file)
            self.loaded = True
            return done()
        raise AssertionError(f"launchctl was asked something unexpected: {args}")

    def did(self) -> list[tuple[str, ...]]:
        """What it was asked to do (not what it was asked about)."""
        return [args for args in self.asked if args[0] != "print"]


@pytest.fixture
def mac(monkeypatch, tmp_path):
    """A Mac where Oscar answers as soon as it is started and is on no call."""
    launchd = Launchd()
    monkeypatch.setattr(autostart, "launchctl", launchd)
    monkeypatch.setattr(autostart.os, "getuid", lambda: 501)
    monkeypatch.setattr(autostart.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(autostart, "START_PATIENCE_SECONDS", 0.05)
    monkeypatch.setattr(autostart, "STOP_PATIENCE_SECONDS", 0.05)
    monkeypatch.setattr(autostart, "is_answering", lambda cfg: launchd.loaded)
    monkeypatch.setattr(autostart, "calls_in_progress", lambda cfg: 0)
    monkeypatch.setattr(autostart, "port_in_use", lambda cfg: False)
    home, root = tmp_path / "home", tmp_path / "Oscar"
    home.mkdir()
    return launchd, home, root


@pytest.fixture
def cfg(tmp_path):
    return make_config(tmp_path, port=7860)


def on_a_call(monkeypatch):
    monkeypatch.setattr(autostart, "calls_in_progress", lambda cfg: 1)


# What macOS is told


def test_the_job_runs_oscar_from_its_own_folder_at_login_and_keeps_it_running(tmp_path):
    root, home = tmp_path / "Oscar", tmp_path / "home"
    assert launch_agent(root, home) == {
        "Label": "local.oscar.server",
        "ProgramArguments": [f"{root}/.venv/bin/python", "-m", "app"],
        "WorkingDirectory": str(root),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Interactive",
        "StandardOutPath": f"{home}/Library/Logs/Oscar/oscar.log",
        "StandardErrorPath": f"{home}/Library/Logs/Oscar/oscar.log",
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
    }
    assert agent_file(home) == home / "Library/LaunchAgents/local.oscar.server.plist"
    assert log_file(home) == home / "Library/Logs/Oscar/oscar.log"


def test_the_job_is_named_in_the_users_own_session(monkeypatch):
    monkeypatch.setattr(autostart.os, "getuid", lambda: 502)
    assert autostart.service() == "gui/502/local.oscar.server"


def test_the_state_of_the_job_is_read_from_what_macos_prints(mac):
    launchd, _, _ = mac
    assert job_state() == (False, None)
    launchd.loaded = True
    assert job_state() == (True, 4321)
    launchd.pid = None  # between a stop and the start that follows
    assert job_state() == (True, None)
    assert launchd.asked == [("print", SERVICE)] * 3


# Setting it up


def test_setting_up_writes_the_job_and_starts_oscar(mac, cfg):
    launchd, home, root = mac

    said = set_up(cfg, root=root, home=home)

    assert said == "Oscar now starts by itself when you log in, and it is running: http://127.0.0.1:7860"
    file = agent_file(home)
    assert plistlib.loads(file.read_bytes()) == launch_agent(root, home)
    # macOS was handed that file, in the user's own session.
    assert launchd.did() == [("bootstrap", "gui/501", str(file))]
    assert launchd.loaded_from == launch_agent(root, home)
    # The log is there before Oscar writes to it, and only its owner can read it.
    log = log_file(home)
    assert log.exists() and log.stat().st_mode & 0o777 == 0o600
    assert log.parent.stat().st_mode & 0o777 == 0o700


def test_a_log_that_is_already_there_is_kept_and_closed_to_others(mac, cfg):
    _, home, root = mac
    log = log_file(home)
    log.parent.mkdir(parents=True)
    log.write_text("earlier lines\n")
    log.chmod(0o644)
    set_up(cfg, root=root, home=home)
    assert log.read_text() == "earlier lines\n" and log.stat().st_mode & 0o777 == 0o600


def test_setting_up_stops_when_something_else_holds_oscars_port(mac, cfg, monkeypatch):
    launchd, home, root = mac
    monkeypatch.setattr(autostart, "port_in_use", lambda cfg: True)
    with pytest.raises(CannotDo) as problem:
        set_up(cfg, root=root, home=home)
    assert str(problem.value).startswith("Something is already answering on port 7860")
    # Nothing was written or started: the job would only fail over and over.
    assert not agent_file(home).exists() and launchd.did() == []


def test_setting_up_again_brings_the_job_up_to_date(mac, cfg, monkeypatch):
    launchd, home, root = mac
    set_up(cfg, root=root, home=home)
    launchd.asked.clear()
    # Its own Oscar holds the port: that is no reason to stop.
    monkeypatch.setattr(autostart, "port_in_use", lambda cfg: True)
    moved = root.parent / "Oscar 2"

    set_up(cfg, root=moved, home=home)

    file = agent_file(home)
    assert launchd.did() == [("bootout", SERVICE), ("bootstrap", "gui/501", str(file))]
    assert launchd.loaded_from["WorkingDirectory"] == str(moved)


def test_macos_refusing_the_job_is_passed_on_in_its_own_words(mac, cfg):
    launchd, home, root = mac
    launchd.refuses_the_job = "Bootstrap failed: 5: Input/output error"
    with pytest.raises(CannotDo) as problem:
        set_up(cfg, root=root, home=home)
    assert str(problem.value) == "macOS would not take the job: Bootstrap failed: 5: Input/output error"


def test_an_oscar_that_never_answers_is_reported_with_where_to_look(mac, cfg, monkeypatch):
    _, home, root = mac
    monkeypatch.setattr(autostart, "is_answering", lambda cfg: False)
    with pytest.raises(CannotDo) as problem:
        set_up(cfg, root=root, home=home)
    message = str(problem.value)
    assert message.startswith("Oscar was started but is not answering at http://127.0.0.1:7860.")
    assert str(log_file(home)) in message and "choose Allow" in message


def test_oscar_is_given_time_to_start_answering(mac, cfg, monkeypatch):
    launchd, home, root = mac
    asked = []

    def answering(cfg):
        asked.append(1)
        return len(asked) >= 4  # still loading the first three times

    monkeypatch.setattr(autostart, "is_answering", answering)
    monkeypatch.setattr(autostart, "START_PATIENCE_SECONDS", 5)
    set_up(cfg, root=root, home=home)
    assert len(asked) == 4
    assert autostart.wait_until(lambda: False, 0.05) is False


# A call in progress is not cut off


@pytest.mark.parametrize("what", [set_up, restart, remove])
def test_nothing_stops_an_oscar_that_is_on_a_call(mac, cfg, monkeypatch, what):
    launchd, home, root = mac
    set_up(cfg, root=root, home=home)
    launchd.asked.clear()
    on_a_call(monkeypatch)

    with pytest.raises(CannotDo) as problem:
        what(cfg, home=home)

    assert str(problem.value) == "Oscar is on a call. End the call first, or add --force to cut it off."
    assert launchd.did() == [] and launchd.loaded and agent_file(home).exists()


@pytest.mark.parametrize("what", [set_up, restart, remove])
def test_force_goes_ahead_during_a_call(mac, cfg, monkeypatch, what):
    launchd, home, root = mac
    set_up(cfg, root=root, home=home)
    launchd.asked.clear()
    on_a_call(monkeypatch)
    what(cfg, home=home, force=True)
    assert launchd.did()[0] == ("bootout", SERVICE)


# Restarting


def test_restarting_stops_oscar_and_starts_it_again_from_the_same_job(mac, cfg):
    launchd, home, root = mac
    set_up(cfg, root=root, home=home)
    file = agent_file(home)
    file.write_bytes(plistlib.dumps({**launch_agent(root, home), "Marked": True}))
    launchd.asked.clear()

    said = restart(cfg, home=home)

    assert said == "Oscar was restarted and is running: http://127.0.0.1:7860"
    assert launchd.did() == [("bootout", SERVICE), ("bootstrap", "gui/501", str(file))]
    assert launchd.loaded_from["Marked"] is True  # the job's file was left as it was


def test_restarting_an_oscar_that_was_never_set_up_says_so(mac, cfg):
    launchd, home, _ = mac
    with pytest.raises(CannotDo) as problem:
        restart(cfg, home=home)
    assert str(problem.value) == (
        "Oscar is not set up to start by itself. Run this without --restart to set it up."
    )
    assert launchd.did() == []


def test_a_job_macos_will_not_stop_is_not_started_twice(mac, cfg):
    launchd, home, root = mac
    set_up(cfg, root=root, home=home)
    launchd.asked.clear()
    launchd.never_stops = True
    with pytest.raises(CannotDo) as problem:
        restart(cfg, home=home)
    assert str(problem.value) == "macOS did not stop Oscar. Try again in a minute."
    assert launchd.did() == [("bootout", SERVICE)]


# Removing


def test_removing_stops_oscar_and_deletes_the_job(mac, cfg):
    launchd, home, root = mac
    set_up(cfg, root=root, home=home)
    launchd.asked.clear()

    said = remove(cfg, home=home)

    assert said.startswith("Oscar is stopped and no longer starts by itself.")
    assert launchd.did() == [("bootout", SERVICE)]
    assert not launchd.loaded and not agent_file(home).exists()
    assert log_file(home).exists()  # the log is left to read


def test_removing_a_job_that_is_only_a_file_deletes_the_file(mac, cfg):
    launchd, home, root = mac
    file = agent_file(home)
    file.parent.mkdir(parents=True)
    file.write_bytes(plistlib.dumps(launch_agent(root, home)))
    assert remove(cfg, home=home).startswith("Oscar is stopped")
    assert not file.exists() and launchd.did() == []


def test_removing_what_was_never_set_up_changes_nothing(mac, cfg):
    launchd, home, _ = mac
    assert remove(cfg, home=home) == "Oscar was not set up to start by itself. Nothing to remove."
    assert launchd.did() == []


# Status


def test_status_says_whether_it_is_set_up_running_and_answering(mac, cfg):
    launchd, home, root = mac
    log = f"Log: {log_file(home)}"
    assert status(cfg, home=home).splitlines() == [
        "Set up to start by itself: no",
        "Running: no",
        "Answering at http://127.0.0.1:7860: no",
        log,
    ]
    set_up(cfg, root=root, home=home)
    assert status(cfg, home=home).splitlines() == [
        "Set up to start by itself: yes",
        "Running: yes (process 4321)",
        "Answering at http://127.0.0.1:7860: yes",
        log,
    ]
    launchd.pid = None
    assert status(cfg, home=home).splitlines()[1] == "Running: no (macOS is starting it again)"
    # Stopped by hand, with its file left: macOS takes it up again at the next login.
    launchd.loaded = False
    assert status(cfg, home=home).splitlines()[:2] == [
        "Set up to start by itself: no (its file is there, so it will be from your next login)",
        "Running: no",
    ]
    assert launchd.did() == [("bootstrap", "gui/501", str(agent_file(home)))]  # status changes nothing


# Asking Oscar itself


class Answer:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("not JSON")
        return self._body


def test_oscar_is_asked_whether_it_is_up_and_how_many_calls_it_is_on(monkeypatch, tmp_path):
    asked = []

    def get(url, **options):
        asked.append((url, options))
        return Answer(body={"ok": True, "calls": 2})

    monkeypatch.setattr(autostart.httpx, "get", get)
    cfg = make_config(tmp_path, port=7860)

    assert autostart.is_answering(cfg) is True
    assert autostart.calls_in_progress(cfg) == 2
    # Both at /healthz, which stays open when Cloudflare Access guards the rest.
    assert asked == [
        ("http://127.0.0.1:7860/healthz", {"timeout": 2}),
        ("http://127.0.0.1:7860/healthz", {"timeout": 3}),
    ]


def test_an_older_oscar_is_asked_for_its_calls_the_old_way(monkeypatch, tmp_path):
    # Before 20 November 2026 /healthz said only {"ok": true}. Restarting such an
    # Oscar after pulling the new code must still notice a call in progress.
    asked = []

    def get(url, **options):
        asked.append(url)
        return Answer(body={"active_call_ids": ["a"]} if url.endswith("/api/status") else {"ok": True})

    monkeypatch.setattr(autostart.httpx, "get", get)
    assert autostart.calls_in_progress(make_config(tmp_path, port=7860)) == 1
    assert asked == ["http://127.0.0.1:7860/healthz", "http://127.0.0.1:7860/api/status"]


@pytest.mark.parametrize(
    "answer",
    [
        lambda url, **options: (_ for _ in ()).throw(httpx.ConnectError("refused")),
        lambda url, **options: Answer(status_code=503),
        lambda url, **options: Answer(body={"unexpected": True}),
        lambda url, **options: Answer(body=["unexpected"]),
    ],
)
def test_an_oscar_that_does_not_answer_properly_counts_as_on_no_call(monkeypatch, cfg, answer):
    monkeypatch.setattr(autostart.httpx, "get", answer)
    assert autostart.calls_in_progress(cfg) == 0


@pytest.mark.parametrize(
    ("host", "url"),
    [
        ("127.0.0.1", "http://127.0.0.1:7860"),
        ("0.0.0.0", "http://127.0.0.1:7860"),
        ("::", "http://127.0.0.1:7860"),
        ("::1", "http://[::1]:7860"),
    ],
)
def test_oscar_listening_at_every_address_is_asked_on_this_machine(tmp_path, host, url):
    assert autostart.address(make_config(tmp_path, host=host, port=7860)) == url


def test_an_oscar_that_is_unreachable_or_unwell_is_not_answering(monkeypatch, cfg):
    def refuse(url, **options):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(autostart.httpx, "get", refuse)
    assert autostart.is_answering(cfg) is False
    monkeypatch.setattr(autostart.httpx, "get", lambda url, **options: Answer(status_code=503))
    assert autostart.is_answering(cfg) is False


def test_a_port_is_in_use_when_something_listens_on_it(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        assert autostart.port_in_use(make_config(tmp_path, port=port)) is True
    assert autostart.port_in_use(make_config(tmp_path, port=port)) is False


# The command itself


@pytest.fixture
def command(mac, cfg, monkeypatch):
    launchd, home, _ = mac
    monkeypatch.setattr(autostart, "load_config", lambda: cfg)
    monkeypatch.setattr(autostart.Path, "home", lambda: home)
    monkeypatch.setattr(sys, "platform", "darwin")
    return launchd, home


def test_the_command_sets_up_then_reports_restarts_and_removes(command, capsys):
    launchd, home = command
    autostart.main([])
    assert capsys.readouterr().out.startswith("Oscar now starts by itself when you log in")
    assert plistlib.loads(agent_file(home).read_bytes())["WorkingDirectory"] == str(autostart.ROOT_DIR)

    autostart.main(["--status"])
    assert capsys.readouterr().out.startswith("Set up to start by itself: yes\nRunning: yes (process 4321)\n")

    autostart.main(["--restart"])
    assert capsys.readouterr().out == "Oscar was restarted and is running: http://127.0.0.1:7860\n"

    autostart.main(["--remove"])
    assert capsys.readouterr().out.startswith("Oscar is stopped and no longer starts by itself.")
    assert not launchd.loaded and not agent_file(home).exists()


def test_the_command_explains_what_it_could_not_do_and_fails(command, capsys, monkeypatch):
    with pytest.raises(SystemExit) as stopped:
        autostart.main(["--restart"])
    assert stopped.value.code == 1
    printed = capsys.readouterr()
    assert printed.out == "" and printed.err.startswith("Oscar is not set up to start by itself.")

    # --force is passed on, whatever is asked for.
    launchd, _ = command
    autostart.main([])
    on_a_call(monkeypatch)
    for asked in ([], ["--restart"], ["--remove"]):
        launchd.asked.clear()
        with pytest.raises(SystemExit):
            autostart.main(asked)
        assert launchd.did() == [] and launchd.loaded
        autostart.main([*asked, "--force"])
        assert launchd.did()[0] == ("bootout", SERVICE)
    assert not launchd.loaded


def test_the_command_is_for_a_mac_with_settings_that_load(command, capsys, monkeypatch):
    launchd, _ = command

    def broken():
        raise ConfigError("PORT must be a number, got 'abc'.")

    monkeypatch.setattr(autostart, "load_config", broken)
    with pytest.raises(SystemExit):
        autostart.main([])
    assert capsys.readouterr().err == "Oscar's settings need fixing first: PORT must be a number, got 'abc'.\n"

    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(SystemExit):
        autostart.main([])
    assert capsys.readouterr().err == "This sets up a macOS launch agent, so it only works on a Mac.\n"
    assert launchd.asked == []


def test_only_one_thing_can_be_asked_for_at_a_time(command):
    with pytest.raises(SystemExit) as stopped:
        autostart.main(["--restart", "--remove"])
    assert stopped.value.code == 2
