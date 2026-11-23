"""Twilio numbers run from the dashboard (app/numbers.py).

The real Twilio library makes every request; only Twilio's servers are a
stand-in (FakeTwilio, an HTTP client the library is given), answering with the
JSON shapes Twilio's API documents. So these tests see exactly what Twilio would
be sent: which address, which form fields.
"""

import json
import logging
import re
import time

import pytest
from twilio.http import HttpClient
from twilio.http.response import Response
from twilio.rest import Client

from app import numbers as numbers_module
from app.numbers import NumbersError, TwilioNumbers, calls_from
from app.store import Store
from tests.conftest import PUBLIC_URL, local_client, make_config, real_twilio_client

ACCOUNT = "AC" + "0" * 32
WEBHOOK = f"{PUBLIC_URL}/twilio/voice"
STATUS = f"{PUBLIC_URL}/twilio/status"
API = f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT}"


def number(sid_digit: str, phone: str, **fields) -> dict:
    """An IncomingPhoneNumber as Twilio's API returns it."""
    return {
        "sid": "PN" + sid_digit * 32,
        "account_sid": ACCOUNT,
        "phone_number": phone,
        "friendly_name": fields.pop("friendly_name", phone),
        "capabilities": fields.pop("capabilities", {"voice": True, "sms": True, "mms": False, "fax": False}),
        "voice_url": "",
        "voice_method": "POST",
        "voice_application_sid": "",
        "status_callback": "",
        "status_callback_method": "POST",
        "trunk_sid": None,
        "voice_receive_mode": "voice",
        **fields,
    }


class FakeTwilio(HttpClient):
    """Twilio's REST API, as far as app/numbers.py uses it."""

    def __init__(self):
        super().__init__(logging.getLogger("fake-twilio"), is_async=False)
        self.requests: list[tuple[str, str, dict]] = []  # (method, url, params or form fields)
        self.account_type = "Full"
        self.numbers = {
            n["sid"]: n
            for n in [
                number("1", "+61255550101", voice_url=WEBHOOK),
                number("2", "+61255550102", voice_url="https://crm.example.com/voice", status_callback="https://crm.example.com/status"),
                number("3", "+61255550103", voice_application_sid="AP" + "3" * 32),
                number("4", "+61255550104", voice_url="https://old-tunnel.ngrok-free.app/twilio/voice", status_callback="https://old-tunnel.ngrok-free.app/twilio/status"),
                number("5", "+61255550105", trunk_sid="TK" + "5" * 32),
                number("6", "+61255550106", capabilities={"voice": False, "sms": True, "mms": False, "fax": False}),
            ]
        }
        self.caller_ids = [
            {"sid": "PN" + "a" * 32, "phone_number": "+61412345678", "friendly_name": "My mobile", "account_sid": ACCOUNT},
            {"sid": "PN" + "b" * 32, "phone_number": "+61255550101", "friendly_name": "Owned too", "account_sid": ACCOUNT},
        ]
        self.available = [
            {
                "friendly_name": "(415) 555-0123", "phone_number": "+14155550123", "lata": "722", "locality": "San Francisco",
                "rate_center": "SNFC", "latitude": "37.77", "longitude": "-122.41", "region": "CA", "postal_code": "94103",
                "iso_country": "US", "address_requirements": "none", "beta": False,
                "capabilities": {"voice": True, "SMS": True, "MMS": True},
            },
            {
                "friendly_name": "(415) 555-0199", "phone_number": "+14155550199", "lata": None, "locality": None,
                "rate_center": None, "latitude": None, "longitude": None, "region": "CA", "postal_code": None,
                "iso_country": "US", "address_requirements": "local", "beta": True,
                "capabilities": {"voice": True, "SMS": False, "MMS": False},
            },
        ]
        self.prices = {
            "US": {"country": "United States", "iso_country": "US", "price_unit": "USD",
                   "phone_number_prices": [{"number_type": "local", "base_price": "1.15", "current_price": "1.15"},
                                           {"number_type": "toll free", "base_price": "2.15", "current_price": "2.15"}]},
        }
        self.refuse: dict[tuple[str, str], tuple[int, dict]] = {}  # (method, path pattern) -> (status, body)
        self.unreachable = False
        self.times_out_after_buying = False  # Twilio buys the number, but its answer never comes

    def request(self, method, url, params=None, data=None, headers=None, auth=None, timeout=None, allow_redirects=False):
        fields = dict(data or {}) if method == "POST" else dict(params or {})
        self.requests.append((method, url, fields))
        if self.unreachable:
            raise ConnectionError("no route to host")
        assert auth == (ACCOUNT, TOKEN), "every request carries the account's credentials"
        for (wanted_method, pattern), (status, body) in self.refuse.items():
            if method == wanted_method and re.search(pattern, url):
                return Response(status, json.dumps(body))
        status, body = self.answer(method, url, fields)
        if self.times_out_after_buying and method == "POST" and url.endswith("/IncomingPhoneNumbers.json"):
            raise TimeoutError("read timed out")
        return Response(status, json.dumps(body))

    def answer(self, method, url, fields):
        meta = {"uri": "/x.json", "page": 0, "page_size": 50, "first_page_uri": "/x.json", "next_page_uri": None, "previous_page_uri": None, "start": 0, "end": 0}
        if method == "GET" and url == f"{API}.json":
            return 200, {"sid": ACCOUNT, "friendly_name": "Taylor Fencing", "status": "active", "type": self.account_type}
        wanted = fields.get("PhoneNumber")  # Twilio's filter by number
        if method == "GET" and url == f"{API}/IncomingPhoneNumbers.json":
            listed = [n for n in self.numbers.values() if not wanted or n["phone_number"] == wanted]
            return 200, {**meta, "incoming_phone_numbers": listed}
        if method == "GET" and url == f"{API}/OutgoingCallerIds.json":
            listed = [n for n in self.caller_ids if not wanted or n["phone_number"] == wanted]
            return 200, {**meta, "outgoing_caller_ids": listed}
        found = re.fullmatch(rf"{API}/IncomingPhoneNumbers/(PN\w+)\.json", url)
        if found:
            sid = found.group(1)
            if sid not in self.numbers:
                return 404, {"code": 20404, "message": f"The requested resource {sid} was not found", "status": 404}
            if method == "POST":
                renamed = {"VoiceUrl": "voice_url", "VoiceMethod": "voice_method", "VoiceApplicationSid": "voice_application_sid",
                           "StatusCallback": "status_callback", "StatusCallbackMethod": "status_callback_method"}
                for field, value in fields.items():
                    self.numbers[sid][renamed[field]] = value
            return 200, self.numbers[sid]
        if method == "POST" and url == f"{API}/IncomingPhoneNumbers.json":
            bought = number("9", fields["PhoneNumber"], voice_url=fields.get("VoiceUrl", ""), status_callback=fields.get("StatusCallback", ""))
            self.numbers[bought["sid"]] = bought
            return 201, bought
        if method == "GET" and url == f"{API}/AvailablePhoneNumbers.json":
            return 200, {"uri": "/x.json", "countries": [
                {"country_code": "US", "country": "United States", "uri": "/US.json", "beta": False,
                 "subresource_uris": {"local": "/US/Local.json", "toll_free": "/US/TollFree.json"}},
                {"country_code": "AU", "country": "Australia", "uri": "/AU.json", "beta": False,
                 "subresource_uris": {"local": "/AU/Local.json", "mobile": "/AU/Mobile.json", "toll_free": "/AU/TollFree.json"}},
                {"country_code": "XX", "country": "Nowhere", "uri": "/XX.json", "beta": False, "subresource_uris": {"voip": "/XX/Voip.json"}},
            ]}
        found = re.fullmatch(rf"{API}/AvailablePhoneNumbers/(\w\w)/(Local|Mobile|TollFree)\.json", url)
        if found:
            return 200, {**meta, "available_phone_numbers": self.available}
        found = re.fullmatch(r"https://pricing\.twilio\.com/v1/PhoneNumbers/Countries/(\w\w)", url)
        if found:
            if found.group(1) not in self.prices:
                return 404, {"code": 20404, "message": "Not found", "status": 404}
            return 200, self.prices[found.group(1)]
        raise AssertionError(f"Twilio was not expected to be asked {method} {url}")

    def posted(self) -> list[tuple[str, dict]]:
        return [(url, fields) for method, url, fields in self.requests if method == "POST"]


TOKEN = "test-auth-token"


@pytest.fixture
def twilio(monkeypatch, tmp_path) -> FakeTwilio:
    # Oscar's own client gives every request to Twilio a timeout.
    real = real_twilio_client(make_config(tmp_path, twilio_auth_token=TOKEN))
    assert real.http_client.timeout == numbers_module.TWILIO_TIMEOUT_SECONDS
    fake = FakeTwilio()

    def client(cfg):
        return Client(cfg.twilio_account_sid, cfg.twilio_auth_token, http_client=fake)

    monkeypatch.setattr(numbers_module, "twilio_client", client)
    return fake


@pytest.fixture
def cfg(tmp_path):
    return make_config(tmp_path, twilio_auth_token=TOKEN)


@pytest.fixture
def store(cfg):
    store = Store(cfg.db_path)
    yield store
    store.close()


OLD_TUNNEL = "https://old-tunnel.ngrok-free.app"


@pytest.fixture
def numbers(cfg, store, twilio) -> TwilioNumbers:
    store.remember_oscar_address(OLD_TUNNEL)  # where Oscar was before its tunnel changed
    return TwilioNumbers(cfg, store)


# Seeing the account's numbers


def test_each_number_says_whether_it_reaches_oscar(numbers, twilio):
    overview = numbers.overview()
    states = {n["phone_number"]: (n["state"], n["detail"]) for n in overview["numbers"]}
    assert states == {
        "+61255550101": ("connected", "Oscar answers its calls."),
        "+61255550102": ("elsewhere", "Its calls go to crm.example.com."),
        "+61255550103": ("elsewhere", "A TwiML app in Twilio answers its calls."),
        "+61255550104": ("old_address", "Its calls go to old-tunnel.ngrok-free.app, an older address of Oscar. Connect it again."),
        "+61255550105": ("trunk", "A SIP trunk in Twilio answers it, so Oscar cannot be connected to it here."),
        "+61255550106": ("unset", "Nothing answers its calls yet."),
    }
    assert overview["numbers"][5]["capabilities"] == {"voice": False, "sms": True, "mms": False}
    # Verified caller IDs Oscar can call from, not counting the account's own numbers again.
    assert overview["caller_ids"] == [{"phone_number": "+61412345678", "friendly_name": "My mobile"}]
    assert (overview["trial"], overview["calls_from"], overview["calls_from_chosen"]) == (False, "+15005550006", False)
    assert overview["webhook"] == WEBHOOK
    assert all(method == "GET" for method, _, _ in twilio.requests)  # looking changes nothing


def test_a_trial_account_says_so(numbers, twilio):
    twilio.account_type = "Trial"
    assert numbers.overview()["trial"] is True


# Connecting a number to Oscar, and back


def test_connecting_points_the_number_at_oscar_and_keeps_what_it_had(numbers, twilio, store):
    sid = "PN" + "2" * 32
    connected = numbers.connect(sid)
    assert (connected["state"], connected["phone_number"]) == ("connected", "+61255550102")
    ((url, fields),) = twilio.posted()
    assert url == f"{API}/IncomingPhoneNumbers/{sid}.json"
    # Its status callback belongs to something else, so it is left alone, and it has
    # no TwiML app to take off, so nothing is sent about one.
    assert fields == {"VoiceUrl": WEBHOOK, "VoiceMethod": "POST"}
    kept = store.get_number_connection(sid)
    assert (kept["voice_url"], kept["status_callback"]) == ("https://crm.example.com/voice", "https://crm.example.com/status")

    numbers.disconnect(sid)
    (_, put_back) = twilio.posted()[-1]
    assert put_back == {"VoiceUrl": "https://crm.example.com/voice", "VoiceMethod": "POST"}
    assert twilio.numbers[sid]["status_callback"] == "https://crm.example.com/status"
    assert store.get_number_connection(sid) is None


def test_a_number_a_twiml_app_answered_gets_its_app_back(numbers, twilio):
    sid = "PN" + "3" * 32
    numbers.connect(sid)
    (_, fields) = twilio.posted()[-1]
    # No one used its status callback, so Oscar takes it (it tells Oscar when a call ends).
    assert fields == {"VoiceUrl": WEBHOOK, "VoiceMethod": "POST", "VoiceApplicationSid": "",
                      "StatusCallback": STATUS, "StatusCallbackMethod": "POST"}
    back = numbers.disconnect(sid)
    (_, put_back) = twilio.posted()[-1]
    assert put_back == {"VoiceUrl": "", "VoiceMethod": "POST", "VoiceApplicationSid": "AP" + "3" * 32,
                        "StatusCallback": "", "StatusCallbackMethod": "POST"}
    assert back["state"] == "elsewhere"
    assert twilio.numbers[sid]["voice_application_sid"] == "AP" + "3" * 32
    assert (twilio.numbers[sid]["voice_url"], twilio.numbers[sid]["status_callback"]) == ("", "")


def test_a_number_at_an_older_address_of_oscar_is_moved_to_this_one(numbers, twilio, store):
    sid = "PN" + "4" * 32
    numbers.connect(sid)
    (_, fields) = twilio.posted()[-1]
    assert fields["VoiceUrl"] == WEBHOOK and fields["StatusCallback"] == STATUS
    assert store.get_number_connection(sid) is None  # an old address of Oscar's is not worth putting back


def test_a_number_a_sip_trunk_answers_is_left_alone(numbers, twilio):
    with pytest.raises(NumbersError, match="SIP trunk") as refused:
        numbers.connect("PN" + "5" * 32)
    assert refused.value.status == 409
    assert twilio.posted() == []


def test_only_a_number_oscar_answers_can_be_disconnected(numbers, twilio):
    with pytest.raises(NumbersError, match="does not answer") as refused:
        numbers.disconnect("PN" + "2" * 32)
    assert refused.value.status == 409 and twilio.posted() == []


def test_a_sid_that_is_not_one_is_refused_before_asking_twilio(numbers, twilio):
    with pytest.raises(NumbersError, match="not a Twilio number"):
        numbers.connect("../Calls")
    assert twilio.requests == []


# Finding and buying a number


def test_countries_list_the_kinds_of_number_each_has(numbers, twilio):
    countries = numbers.countries()
    assert countries == [
        {"code": "AU", "name": "Australia", "types": ["local", "mobile", "toll_free"]},
        {"code": "US", "name": "United States", "types": ["local", "toll_free"]},
    ]
    asked = len(twilio.requests)
    assert numbers.countries() == countries and len(twilio.requests) == asked  # kept, not asked again


def test_a_search_asks_for_numbers_that_take_calls_and_says_the_price(numbers, twilio):
    found = numbers.search("us", "local", area_code="415")
    method, url, params = twilio.requests[0]
    assert (method, url) == ("GET", f"{API}/AvailablePhoneNumbers/US/Local.json")
    assert params == {"AreaCode": 415, "VoiceEnabled": "true", "PageSize": 20}
    assert found["price"] == {"monthly": "1.15", "unit": "USD"}
    first, second = found["numbers"]
    assert first == {
        "phone_number": "+14155550123", "friendly_name": "(415) 555-0123", "place": "San Francisco, CA",
        "capabilities": {"voice": True, "sms": True, "mms": True}, "address_needed": "", "beta": False,
    }
    assert second["address_needed"] == "Twilio needs an address in its country on file for it"
    assert (second["place"], second["beta"]) == ("CA", True)


def test_toll_free_numbers_are_priced_as_toll_free(numbers, twilio):
    found = numbers.search("US", "toll_free", contains="800")
    assert twilio.requests[0][1] == f"{API}/AvailablePhoneNumbers/US/TollFree.json"
    assert twilio.requests[0][2]["Contains"] == "800"
    assert found["price"] == {"monthly": "2.15", "unit": "USD"}


def test_a_price_in_a_lower_case_currency_is_shown_in_capitals(numbers, twilio):
    # Twilio's reference gives the unit as an ISO 4217 code "(e.g. `usd`, `eur`, `jpy`)".
    twilio.prices["US"]["price_unit"] = "usd"
    assert numbers.search("US", "local")["price"] == {"monthly": "1.15", "unit": "USD"}


def test_a_price_twilio_does_not_give_is_left_unknown(numbers, twilio):
    assert numbers.search("AU", "mobile")["price"] is None


@pytest.mark.parametrize(
    "args, problem",
    [(("U", "local"), "Choose a country"), (("US", "voip"), "local, mobile or toll-free"),
     (("US", "local", "4a"), "area code"), (("US", "local", "", "5"), "2 to 16")],
)
def test_a_search_that_cannot_work_is_refused_before_asking_twilio(numbers, twilio, args, problem):
    with pytest.raises(NumbersError, match=problem):
        numbers.search(*args)
    assert twilio.requests == []


def test_a_number_bought_is_connected_and_becomes_the_one_oscar_calls_from(tmp_path, twilio):
    cfg = make_config(tmp_path, twilio_auth_token=TOKEN, twilio_phone_number="")
    store = Store(cfg.db_path)
    bought = TwilioNumbers(cfg, store).buy("+14155550123")
    ((url, fields),) = twilio.posted()
    assert url == f"{API}/IncomingPhoneNumbers.json"
    assert fields == {"PhoneNumber": "+14155550123", "VoiceUrl": WEBHOOK, "VoiceMethod": "POST",
                      "StatusCallback": STATUS, "StatusCallbackMethod": "POST"}
    assert (bought["phone_number"], bought["state"]) == ("+14155550123", "connected")
    assert calls_from(cfg, store) == "+14155550123"
    store.close()


def test_a_second_number_bought_leaves_the_number_oscar_calls_from(numbers, twilio, store):
    numbers.buy("+14155550123")
    assert store.get_calls_from() == ""  # TWILIO_PHONE_NUMBER is still the one


def test_a_number_already_taken_is_explained_in_twilios_words(numbers, twilio):
    twilio.refuse[("POST", r"IncomingPhoneNumbers\.json$")] = (
        400, {"code": 21422, "message": "PhoneNumber +14155550123 is not available.", "status": 400},
    )
    with pytest.raises(NumbersError) as refused:
        numbers.buy("+14155550123")
    assert str(refused.value) == "Twilio said: PhoneNumber +14155550123 is not available."
    assert refused.value.status == 400


def test_a_number_that_needs_paperwork_says_where_to_add_it(numbers, twilio):
    twilio.refuse[("POST", r"IncomingPhoneNumbers\.json$")] = (
        400, {"code": 21631, "message": "Phone Number Requires an Address but the 'AddressSid' parameter was empty.", "status": 400},
    )
    with pytest.raises(NumbersError, match="Regulatory Compliance"):
        numbers.buy("+61255550199")


def test_wrong_credentials_say_how_to_fix_them(numbers, twilio):
    twilio.refuse[("GET", r".")] = (401, {"code": 20003, "message": "Authenticate", "status": 401})
    with pytest.raises(NumbersError, match="refused the Account SID and Auth Token") as refused:
        numbers.overview()
    assert refused.value.status == 400


@pytest.mark.parametrize(
    ("body", "said"),
    [
        ({"message": "Authenticate"}, "refused the Account SID and Auth Token"),  # no error code
        ({"code": 20005, "message": "Account not active"}, "Twilio said: Account not active."),
    ],
)
def test_only_refused_keys_are_blamed_on_the_keys(numbers, twilio, body, said):
    twilio.refuse[("GET", r".")] = (401, {**body, "status": 401})
    with pytest.raises(NumbersError) as refused:
        numbers.overview()
    assert said in str(refused.value)


RFC_9457 = {"type": "https://www.twilio.com/docs/errors/21422", "title": "Phone number unavailable",
            "status": 400, "code": 21422, "detail": "+14155550199 is not available"}


@pytest.mark.parametrize("asked", ["a list", "an instance", "a purchase"])
def test_a_refusal_in_twilios_newer_form_is_explained_in_its_words(numbers, twilio, asked):
    # The library raises these as a bare TwilioException (a list's page) or a
    # TwilioServiceException (anything else), neither of them a TwilioRestException.
    method, ask = {
        "a list": ("GET", numbers.countries),  # only a list's pages
        "an instance": ("GET", lambda: numbers.connect("PN" + "2" * 32)),
        "a purchase": ("POST", lambda: numbers.buy("+14155550199")),
    }[asked]
    twilio.refuse[(method, r".")] = (400, RFC_9457)
    with pytest.raises(NumbersError) as refused:
        ask()
    assert str(refused.value) == "Twilio said: +14155550199 is not available."
    assert refused.value.status == 400


def test_twilio_out_of_reach_is_said_plainly(numbers, twilio):
    twilio.unreachable = True
    with pytest.raises(NumbersError, match="Could not reach Twilio") as refused:
        numbers.overview()
    assert refused.value.status == 502


# The number Oscar calls from


@pytest.mark.parametrize("chosen", ["+61255550102", "+61412345678"])
def test_oscar_can_call_from_a_number_of_the_account_or_a_verified_one(numbers, store, chosen):
    assert numbers.choose_calls_from(chosen) == chosen
    assert store.get_calls_from() == chosen


@pytest.mark.parametrize("chosen", ["+61255550106", "+14155550000"])
def test_oscar_cannot_call_from_a_number_that_takes_no_calls_or_is_not_the_accounts(numbers, store, chosen):
    with pytest.raises(NumbersError, match="not a number of this Twilio account"):
        numbers.choose_calls_from(chosen)
    assert store.get_calls_from() == ""


# Through the dashboard's API


def test_the_dashboard_sees_connects_and_chooses(tmp_path, twilio):
    with local_client(make_config(tmp_path, twilio_auth_token=TOKEN)) as client:
        overview = client.get("/api/numbers").json()
        assert len(overview["numbers"]) == 6
        connected = client.post(f"/api/numbers/{'PN' + '6' * 32}/connect").json()["number"]
        assert connected["state"] == "connected"
        assert client.put("/api/numbers/calls-from", json={"phone_number": "+61412345678"}).json() == {"calls_from": "+61412345678"}
        assert client.get("/api/status").json()["phone"]["number"] == "+61412345678"
        refused = client.post(f"/api/numbers/{'PN' + '5' * 32}/connect")
        assert refused.status_code == 409 and "SIP trunk" in refused.json()["detail"]
        found = client.get("/api/numbers/search", params={"country": "US", "kind": "local", "area_code": "415"}).json()
        assert found["price"]["monthly"] == "1.15"
        bought = client.post("/api/numbers/buy", json={"phone_number": "+14155550123"}).json()
        assert bought["number"]["state"] == "connected"
        assert client.get("/api/numbers/countries").json()["countries"][0]["code"] == "AU"


def test_numbers_need_the_twilio_account_set_up(tmp_path, twilio):
    with local_client(make_config(tmp_path, twilio_account_sid="", twilio_auth_token="")) as client:
        response = client.get("/api/numbers")
        assert response.status_code == 400
        assert response.json()["detail"] == "Add TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN to the .env file, then restart Oscar."
    assert twilio.requests == []



# Found by review: another app's webhook, refusals on lists, a purchase that times out


def test_another_apps_webhook_on_the_same_path_is_not_taken_for_oscar(numbers, twilio, store):
    sid = "PN" + "7" * 32
    twilio.numbers[sid] = number("7", "+61255550107", voice_url="https://crm.example.com/twilio/voice",
                                 status_callback="https://crm.example.com/twilio/status")
    view = next(n for n in numbers.overview()["numbers"] if n["sid"] == sid)
    assert (view["state"], view["detail"]) == ("elsewhere", "Its calls go to crm.example.com.")
    with pytest.raises(NumbersError, match="does not answer"):
        numbers.disconnect(sid)  # nothing of the other app's is touched
    numbers.connect(sid)
    (_, fields) = twilio.posted()[-1]
    assert "StatusCallback" not in fields  # the other app's status callback stays
    numbers.disconnect(sid)
    assert twilio.numbers[sid]["voice_url"] == "https://crm.example.com/twilio/voice"


def test_oscars_own_address_is_remembered_for_when_it_changes(cfg, store, twilio):
    TwilioNumbers(cfg, store)
    assert PUBLIC_URL in store.oscar_addresses()


def test_disconnecting_puts_back_what_the_number_had_most_recently(numbers, twilio, store):
    sid = "PN" + "2" * 32
    numbers.connect(sid)
    numbers.disconnect(sid)
    twilio.numbers[sid]["voice_url"] = "https://new-crm.example.com/voice"  # changed in Twilio's console since
    numbers.connect(sid)
    numbers.disconnect(sid)
    assert twilio.numbers[sid]["voice_url"] == "https://new-crm.example.com/voice"


def test_a_list_twilio_refuses_is_explained_in_its_own_words(numbers, twilio):
    twilio.refuse[("GET", r"AvailablePhoneNumbers/US/Local")] = (
        400, {"code": 21452, "message": "No phone numbers found in this area code", "status": 400},
    )
    with pytest.raises(NumbersError) as refused:
        numbers.search("US", "local", area_code="999")
    assert str(refused.value) == "Twilio said: No phone numbers found in this area code."
    assert refused.value.status == 400


def test_credentials_refused_on_a_list_say_how_to_fix_them(numbers, twilio):
    twilio.refuse[("GET", r"IncomingPhoneNumbers\.json")] = (401, {"code": 20003, "message": "Authenticate", "status": 401})
    with pytest.raises(NumbersError, match="refused the Account SID and Auth Token"):
        numbers.choose_calls_from("+61255550102")


def test_a_purchase_whose_answer_never_came_is_looked_for_before_saying_it_failed(tmp_path, twilio):
    cfg = make_config(tmp_path, twilio_auth_token=TOKEN, twilio_phone_number="")
    store = Store(cfg.db_path)
    twilio.times_out_after_buying = True
    bought = TwilioNumbers(cfg, store).buy("+14155550123")
    assert (bought["phone_number"], bought["state"]) == ("+14155550123", "connected")
    assert store.get_calls_from() == "+14155550123"
    # Looked up by the number itself, not by reading the whole account.
    method, url, fields = twilio.requests[-1]
    assert (method, url, fields["PhoneNumber"]) == ("GET", f"{API}/IncomingPhoneNumbers.json", "+14155550123")
    store.close()


def test_a_purchase_that_cannot_be_confirmed_says_to_look_before_trying_again(numbers, twilio):
    twilio.unreachable = True
    with pytest.raises(NumbersError, match="not known whether \\+14155550123 was bought. Press Refresh"):
        numbers.buy("+14155550123")


def test_the_number_to_call_from_is_looked_up_by_itself(numbers, twilio):
    numbers.choose_calls_from("+61412345678")
    asked = [fields.get("PhoneNumber") for method, _, fields in twilio.requests if method == "GET"]
    assert asked == ["+61412345678", "+61412345678"]  # the account's numbers, then verified caller IDs


# On starting, Oscar brings its number to the agent by itself


def test_on_starting_numbers_at_an_older_address_of_oscar_are_moved_to_this_one(numbers, twilio):
    said = numbers.connect_by_itself()
    assert said == ["+61255550104 sent its calls to an older address of Oscar: they now come to this one."]
    assert twilio.numbers["PN" + "4" * 32]["voice_url"] == WEBHOOK
    # Only that number was changed: another app's numbers, the trunk's and the texts-only one were not.
    assert [url for url, _ in twilio.posted()] == [f"{API}/IncomingPhoneNumbers/{'PN' + '4' * 32}.json"]


def test_on_starting_the_number_oscar_calls_from_is_connected_when_nothing_answers_it(numbers, twilio, store):
    twilio.numbers["PN" + "7" * 32] = number("7", "+61255550107")
    store.set_calls_from("+61255550107")
    said = numbers.connect_by_itself()
    assert "+61255550107, the number Oscar calls from, answered nothing: its calls now come to Oscar." in said
    assert twilio.numbers["PN" + "7" * 32]["voice_url"] == WEBHOOK


def test_on_starting_a_number_another_app_answers_is_not_taken(numbers, twilio, store):
    store.set_calls_from("+61255550102")
    (about,) = [sentence for sentence in numbers.connect_by_itself() if sentence.startswith("+61255550102")]
    assert about.startswith("+61255550102, the number Oscar calls from, does not bring its calls to Oscar.")
    assert about.endswith("Connect it in Phone, Your numbers.")
    assert twilio.numbers["PN" + "2" * 32]["voice_url"] == "https://crm.example.com/voice"


def test_on_starting_the_only_number_on_the_account_is_the_one_oscar_calls_from(tmp_path, twilio):
    store = Store(tmp_path / "only.db")
    twilio.numbers = {"PN" + "7" * 32: number("7", "+61255550107")}
    numbers = TwilioNumbers(make_config(tmp_path, twilio_phone_number="", twilio_auth_token=TOKEN), store)
    said = numbers.connect_by_itself()
    assert said == [
        "+61255550107 is the number Oscar calls from: it is the only one on the Twilio account.",
        "+61255550107, the number Oscar calls from, answered nothing: its calls now come to Oscar.",
    ]
    assert store.get_calls_from() == "+61255550107"
    assert twilio.numbers["PN" + "7" * 32]["voice_url"] == WEBHOOK


def test_on_starting_with_several_numbers_none_is_chosen_for_you(tmp_path, twilio):
    store = Store(tmp_path / "several.db")
    twilio.numbers = {sid: number(sid[2], phone) for sid, phone in [("PN" + "7" * 32, "+61255550107"), ("PN" + "8" * 32, "+61255550108")]}
    numbers = TwilioNumbers(make_config(tmp_path, twilio_phone_number="", twilio_auth_token=TOKEN), store)
    assert numbers.connect_by_itself() == []
    assert store.get_calls_from() == "" and twilio.posted() == []


def test_the_dashboard_says_what_oscar_did_on_starting(tmp_path, twilio):
    store = Store(tmp_path / "oscar-test.db")
    store.remember_oscar_address(OLD_TUNNEL)
    store.close()
    with local_client(make_config(tmp_path, twilio_auth_token=TOKEN)) as client:
        for _ in range(100):
            overview = client.get("/api/numbers").json()
            if overview["at_start"]:
                break
            time.sleep(0.05)
        assert overview["at_start"] == ["+61255550104 sent its calls to an older address of Oscar: they now come to this one."]
        states = {n["phone_number"]: n["state"] for n in overview["numbers"]}
        assert states["+61255550104"] == "connected"


# A number disconnected in the dashboard stays as the owner left it


def test_a_number_disconnected_in_the_dashboard_is_not_connected_again_on_starting(numbers, twilio, store):
    twilio.numbers["PN" + "7" * 32] = number("7", "+61255550107")
    store.set_calls_from("+61255550107")
    numbers.connect("PN" + "7" * 32)
    numbers.disconnect("PN" + "7" * 32)  # the owner's choice: it answers nothing again
    assert twilio.numbers["PN" + "7" * 32]["voice_url"] == ""

    said = numbers.connect_by_itself()
    assert twilio.numbers["PN" + "7" * 32]["voice_url"] == ""
    assert (
        "+61255550107, the number Oscar calls from, was disconnected in Your numbers, so Oscar left it as it is. "
        "Connect it there for Oscar to answer its calls."
    ) in said

    # Connected again in the dashboard, it is looked after again.
    numbers.connect("PN" + "7" * 32)
    twilio.numbers["PN" + "7" * 32]["voice_url"] = ""  # emptied in Twilio's console since
    numbers.connect_by_itself()
    assert twilio.numbers["PN" + "7" * 32]["voice_url"] == WEBHOOK


def test_choosing_a_number_that_answers_nothing_to_call_from_connects_it(numbers, twilio, store):
    twilio.numbers["PN" + "7" * 32] = number("7", "+61255550107")
    numbers.choose_calls_from("+61255550107")
    assert store.get_calls_from() == "+61255550107"
    assert twilio.numbers["PN" + "7" * 32]["voice_url"] == WEBHOOK


@pytest.mark.parametrize("chosen", ["+61255550102", "+61412345678"])
def test_choosing_a_number_another_app_answers_or_a_caller_id_changes_nothing_in_twilio(numbers, twilio, chosen):
    # +61255550102 is answered by a CRM; +61412345678 is a verified caller ID.
    numbers.choose_calls_from(chosen)
    assert twilio.posted() == []


def test_choosing_a_number_disconnected_in_the_dashboard_leaves_it_disconnected(numbers, twilio):
    twilio.numbers["PN" + "7" * 32] = number("7", "+61255550107")
    numbers.connect("PN" + "7" * 32)
    numbers.disconnect("PN" + "7" * 32)
    numbers.choose_calls_from("+61255550107")
    assert twilio.numbers["PN" + "7" * 32]["voice_url"] == ""
