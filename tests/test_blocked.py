"""Numbers whose calls Oscar refuses before answering."""

import pytest

from app.phone import is_blocked
from tests.conftest import local_client, make_config
from tests.test_phone import signed_post

SPAM = {"CallSid": "CA" + "8" * 32, "From": "+61412345678", "To": "+15005550006"}


@pytest.mark.parametrize(
    ("caller", "blocked", "refused"),
    [
        ("+61412345678", ["+61 412 345 678"], True),
        ("+61412345678", ["0412 345 678"], True),  # written without the country code
        ("+61412345678", ["61412345678"], True),
        ("+61412345678", ["0412 345 679"], False),
        ("+14155550123", ["(415) 555-0123"], True),
        ("+61412345678", ["345 678"], False),  # too short to tell: never blocks
        ("anonymous", ["0412 345 678"], False),
        ("+61412345678", [], False),
    ],
)
def test_a_number_matches_however_it_was_written(caller, blocked, refused):
    assert is_blocked(caller, blocked) is refused


@pytest.fixture
def client(tmp_path):
    with local_client(make_config(tmp_path)) as test_client:
        yield test_client


def test_a_blocked_caller_is_refused_unanswered_and_not_in_the_history(client):
    assert client.put("/api/blocked-numbers", json={"numbers": ["0412 345 678"]}).status_code == 200
    response = signed_post(client, "/twilio/voice", SPAM)
    assert response.status_code == 200
    assert '<Reject reason="rejected"' in response.text and "<Stream" not in response.text
    assert client.get("/api/calls").json()["calls"] == []
    # Anyone else gets through.
    other = signed_post(client, "/twilio/voice", {**SPAM, "From": "+61499999999"})
    assert "<Stream" in other.text


def test_the_list_is_tidied_and_kept(client):
    saved = client.put(
        "/api/blocked-numbers", json={"numbers": [" 0412  345 678 ", "", "0412345678", "+1 415 555 0123"]}
    )
    # Blank lines go, and the same digits written twice are kept once.
    assert saved.json() == {"numbers": ["0412 345 678", "+1 415 555 0123"]}
    assert client.get("/api/blocked-numbers").json() == saved.json()
    assert client.put("/api/blocked-numbers", json={"numbers": []}).json() == {"numbers": []}


@pytest.mark.parametrize("number", ["12345", "call me", "+61 412 345 678 ext 9", "1" * 17])
def test_something_that_is_not_a_number_is_refused(client, number):
    response = client.put("/api/blocked-numbers", json={"numbers": [number]})
    assert response.status_code == 400 and "is not a phone number" in response.json()["detail"]
