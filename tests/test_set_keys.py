"""Saving keys and settings into .env (scripts/set_keys.py)."""

import io

import pytest
from dotenv import dotenv_values

from scripts import set_keys
from scripts.set_keys import PHONE, Setting, account_sid, phone_number, with_value


def test_a_value_replaces_its_own_line_and_leaves_the_rest():
    text = "OPENROUTER_API_KEY=old\nTWILIO_AUTH_TOKEN=\nPORT=7860\n"
    assert with_value(text, "TWILIO_AUTH_TOKEN", "abc123") == (
        "OPENROUTER_API_KEY=old\nTWILIO_AUTH_TOKEN=abc123\nPORT=7860\n"
    )


def test_a_setting_not_in_the_file_yet_is_added_at_the_end():
    assert with_value("PORT=7860", "TWILIO_AUTH_TOKEN", "s3cret-token") == (
        "PORT=7860\nTWILIO_AUTH_TOKEN=s3cret-token\n"
    )


def test_a_value_with_odd_characters_is_saved_as_typed():
    # A key may hold characters that mean something in a regular expression.
    assert with_value("TWILIO_AUTH_TOKEN=x\n", "TWILIO_AUTH_TOKEN", r"a\1$&b") == (
        "TWILIO_AUTH_TOKEN=a\\1$&b\n"
    )


def test_a_second_line_for_the_same_setting_is_dropped_so_the_new_value_is_the_one_read():
    # The empty line from .env.example, and one added at the end by hand later.
    text = "OPENROUTER_API_KEY=\nPORT=7860\nOPENROUTER_API_KEY=sk-or-old\n"
    saved = with_value(text, "OPENROUTER_API_KEY", "sk-or-new")
    assert saved == "OPENROUTER_API_KEY=sk-or-new\nPORT=7860\n"
    assert dotenv_values(stream=io.StringIO(saved))["OPENROUTER_API_KEY"] == "sk-or-new"


@pytest.mark.parametrize("written", ["export OPENROUTER_API_KEY=sk-or-old", "OPENROUTER_API_KEY = sk-or-old"])
def test_a_line_written_by_hand_another_way_is_replaced_too(written):
    saved = with_value(f"# Keys\n{written}\r\nPORT=7860\r\n", "OPENROUTER_API_KEY", "sk-or-new")
    assert saved == "# Keys\nOPENROUTER_API_KEY=sk-or-new\r\nPORT=7860\r\n"


def test_a_longer_name_is_not_mistaken_for_a_shorter_one():
    text = "TWILIO_PHONE_NUMBER_OLD=+1\nTWILIO_PHONE_NUMBER=\n"
    assert with_value(text, "TWILIO_PHONE_NUMBER", "+14155550123") == (
        "TWILIO_PHONE_NUMBER_OLD=+1\nTWILIO_PHONE_NUMBER=+14155550123\n"
    )


@pytest.mark.parametrize(
    ("check", "value", "fine"),
    [
        (account_sid, "AC" + "0" * 32, True),
        (account_sid, "AC" + "a1" * 16, True),
        (account_sid, "SK" + "0" * 32, False),  # an API key SID, not the account's
        (account_sid, "AC123", False),
        (phone_number, "+14155550123", True),
        (phone_number, "+919876543210", True),
        (phone_number, "4155550123", False),
        (phone_number, "+1 415 555 0123", False),
        (phone_number, "+0123456789", False),
    ],
)
def test_values_are_checked_before_they_are_saved(check, value, fine):
    assert (check(value) == "") is fine


def test_a_value_that_will_not_do_is_asked_for_again(monkeypatch, capsys):
    typed = iter(["4155550123", "+14155550123"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(typed))
    number = next(s for s in PHONE if s.name == "TWILIO_PHONE_NUMBER")
    assert set_keys.ask(number) == "+14155550123"
    assert "international format" in capsys.readouterr().out


def test_secrets_are_typed_hidden_and_enter_keeps_what_is_saved(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("a secret was asked for in the open"))
    monkeypatch.setattr(set_keys.getpass, "getpass", lambda prompt: "  ")
    assert set_keys.ask(Setting("TWILIO_AUTH_TOKEN", "Twilio Auth Token")) == ""


def test_phone_setup_saves_what_phone_calls_need_and_never_shows_secrets(tmp_path, monkeypatch, capsys):
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=keep-me\nTWILIO_ACCOUNT_SID=\nTWILIO_AUTH_TOKEN=\n")
    monkeypatch.setattr(set_keys, "ROOT_DIR", tmp_path)
    shown = iter(["AC" + "0" * 32, "+14155550123"])
    hidden = iter(["the-auth-token-value"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(shown))
    monkeypatch.setattr(set_keys.getpass, "getpass", lambda prompt: next(hidden))

    set_keys.main(PHONE)

    assert (tmp_path / ".env").read_text() == (
        "OPENROUTER_API_KEY=keep-me\n"
        f"TWILIO_ACCOUNT_SID=AC{'0' * 32}\n"
        "TWILIO_AUTH_TOKEN=the-auth-token-value\n"
        "TWILIO_PHONE_NUMBER=+14155550123\n"
    )
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600
    printed = capsys.readouterr().out
    assert "the-auth-token-value" not in printed
    assert "TWILIO_AUTH_TOKEN: saved (20 characters)" in printed
    assert "TWILIO_PHONE_NUMBER: saved (+14155550123)" in printed


# The dashboard's password (--password)


def test_the_password_is_typed_hidden_twice_and_saved_so_oscar_reads_it_back(tmp_path, monkeypatch, capsys):
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=keep-me\nDASHBOARD_PASSWORD=\nHOST=0.0.0.0\n")
    monkeypatch.setattr(set_keys, "ROOT_DIR", tmp_path)
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("the password was asked for in the open"))
    chosen = "it's a #long pass\\word"
    typed = iter(["short", chosen, "a slip", chosen, chosen])
    monkeypatch.setattr(set_keys.getpass, "getpass", lambda prompt: next(typed))

    set_keys.main(set_keys.PASSWORD)

    saved = (tmp_path / ".env").read_text()
    assert dotenv_values(stream=io.StringIO(saved)) == {
        "OPENROUTER_API_KEY": "keep-me",
        "DASHBOARD_PASSWORD": chosen,
        "HOST": "0.0.0.0",
    }
    printed = capsys.readouterr().out
    assert "Use at least 10 characters." in printed
    assert "The two were not the same." in printed
    assert "DASHBOARD_PASSWORD: saved (22 characters)" in printed
    assert chosen not in printed and "#long" not in saved.split("DASHBOARD_PASSWORD=")[0]
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600


def test_enter_keeps_the_password_already_saved(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("DASHBOARD_PASSWORD='kept-password-1'\n")
    monkeypatch.setattr(set_keys, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(set_keys.getpass, "getpass", lambda prompt: "")
    set_keys.main(set_keys.PASSWORD)
    assert (tmp_path / ".env").read_text() == "DASHBOARD_PASSWORD='kept-password-1'\n"


@pytest.mark.parametrize(
    ("value", "fine"),
    [("ten-chars!", True), ("nine-char", False), ("has ${HOME} in it", False), ("spaces are fine here", True)],
)
def test_passwords_are_checked_before_they_are_saved(value, fine):
    assert (set_keys.password_problem(value) == "") is fine


@pytest.mark.parametrize("value", ["plain-password", "with 'quotes' and \"more\"", "a # not a comment", "back\\slash\\\\", "dollar$sign"])
def test_any_password_is_read_back_exactly_as_typed(value):
    saved = with_value("PORT=7860\n", "DASHBOARD_PASSWORD", set_keys.env_quoted(value))
    assert dotenv_values(stream=io.StringIO(saved))["DASHBOARD_PASSWORD"] == value
