"""Saving keys and settings into .env (scripts/set_keys.py)."""

import pytest

from scripts import set_keys
from scripts.set_keys import PHONE, Setting, account_sid, password, phone_number, with_value


def test_a_value_replaces_its_own_line_and_leaves_the_rest():
    text = "OPENROUTER_API_KEY=old\nTWILIO_AUTH_TOKEN=\nPORT=7860\n"
    assert with_value(text, "TWILIO_AUTH_TOKEN", "abc123") == (
        "OPENROUTER_API_KEY=old\nTWILIO_AUTH_TOKEN=abc123\nPORT=7860\n"
    )


def test_a_setting_not_in_the_file_yet_is_added_at_the_end():
    assert with_value("PORT=7860", "DASHBOARD_PASSWORD", "s3cret-pass") == (
        "PORT=7860\nDASHBOARD_PASSWORD=s3cret-pass\n"
    )


def test_a_value_with_odd_characters_is_saved_as_typed():
    # A password may hold characters that mean something in a regular expression.
    assert with_value("DASHBOARD_PASSWORD=x\n", "DASHBOARD_PASSWORD", r"a\1$&b") == (
        "DASHBOARD_PASSWORD=a\\1$&b\n"
    )


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
        (password, "long-enough", True),
        (password, "short", False),
        (password, "has a space in it", False),
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
    hidden = iter(["the-auth-token-value", "my-dashboard-pass"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(shown))
    monkeypatch.setattr(set_keys.getpass, "getpass", lambda prompt: next(hidden))

    set_keys.main(PHONE)

    assert (tmp_path / ".env").read_text() == (
        "OPENROUTER_API_KEY=keep-me\n"
        f"TWILIO_ACCOUNT_SID=AC{'0' * 32}\n"
        "TWILIO_AUTH_TOKEN=the-auth-token-value\n"
        "TWILIO_PHONE_NUMBER=+14155550123\n"
        "DASHBOARD_PASSWORD=my-dashboard-pass\n"
    )
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600
    printed = capsys.readouterr().out
    assert "the-auth-token-value" not in printed and "my-dashboard-pass" not in printed
    assert "TWILIO_AUTH_TOKEN: saved (20 characters)" in printed
    assert "TWILIO_PHONE_NUMBER: saved (+14155550123)" in printed
