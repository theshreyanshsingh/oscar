"""Twilio phone numbers, run from the dashboard.

With the Twilio account's Account SID and Auth Token in .env (and PUBLIC_URL),
the dashboard shows the account's numbers, connects a number to Oscar (Twilio is
told to send that number's calls to /twilio/voice: no pasting webhooks in
Twilio's console), searches for numbers to buy with Twilio's monthly price, buys
one already connected, and chooses which number Oscar's own calls come from.

Connecting a number changes only how it answers calls (its voice webhook, and
the TwiML app it may have instead), and the status callback when nothing else
uses it. What it had before is kept (Store.save_number_connection), so
disconnecting puts it back as it was. The dashboard asks before taking a number
over from another address or a TwiML app. A number a SIP trunk answers is left
alone: connecting would take it off the trunk.

A webhook counts as Oscar's only at an address Oscar has had (PUBLIC_URL, kept
in Store.oscar_addresses as it changes): another app on the same account can use
the same /twilio/voice path, and must not be taken for an old Oscar.

Every request to Twilio is made here, on a worker thread (they block), with a
timeout. Releasing a number is left to Twilio's console: it cannot be undone.
"""

import json
import re
import threading
import time
from urllib.parse import urlsplit

from loguru import logger
from twilio.base.exceptions import TwilioException, TwilioRestException, TwilioServiceException
from twilio.http.http_client import TwilioHttpClient
from twilio.rest import Client

from app.config import Config
from app.store import Store

TWILIO_TIMEOUT_SECONDS = 15
# The kinds of number that can be searched for, as Twilio names them in its address
# (AvailablePhoneNumbers/{country}/Local) and in its prices ("toll free").
NUMBER_TYPES = {"local": "Local", "mobile": "Mobile", "toll_free": "Toll-free"}
SEARCH_RESULTS = 20
# The list of countries Twilio sells numbers in is kept this long.
COUNTRIES_KEEP_SECONDS = 24 * 60 * 60
# The most numbers (and verified caller IDs) the dashboard lists.
NUMBERS_READ = 1000
ADDRESS_NEEDS = {
    "any": "Twilio needs an address on file for it",
    "local": "Twilio needs an address in its country on file for it",
    "foreign": "Twilio needs an address outside its country on file for it",
}
# Said with a refusal that is about Twilio's paperwork for a country's numbers.
REGULATORY_HINT = (
    " Numbers in some countries need an address or other documents first: add them in "
    "Twilio's console, under Phone Numbers, Regulatory Compliance."
)


class NumbersError(Exception):
    """Twilio refused, or could not be reached. The message is a sentence for the dashboard."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def twilio_client(cfg: Config) -> Client:
    return Client(
        cfg.twilio_account_sid,
        cfg.twilio_auth_token,
        http_client=TwilioHttpClient(timeout=TWILIO_TIMEOUT_SECONDS),
    )


KEYS_REFUSED = (
    "Twilio refused the Account SID and Auth Token in .env. Check them in Twilio's "
    "console (Account Info), save them with scripts.set_keys --phone, and restart Oscar."
)


def keys_refused(exc: TwilioRestException) -> bool:
    """Twilio refused the Account SID and Auth Token: error 20003, or an HTTP 401 that
    names no error (another code, like 20005 for an account that is not active, is not)."""
    # With no code in Twilio's answer, the library gives the HTTP status as the code.
    if exc.code not in (None, exc.status):
        return exc.code == 20003
    return exc.status == 401


def twilio_message(exc: TwilioRestException) -> str:
    """Twilio's own words for a refusal, without the full stop, and without what the
    Twilio library puts in front of them ("Unable to create record: ...")."""
    message = re.sub(r"\s+", " ", str(exc.msg or "")).strip()
    message = re.sub(r"^Unable to (?:create|update|fetch|delete) (?:record|page):\s*", "", message)
    return message.rstrip(".") or f"error {exc.status}"


def twilio_refusal(exc: Exception) -> TwilioRestException | None:
    """Twilio's refusal, whichever way the library raised it, or None when Twilio did not
    answer. A list's page that Twilio refuses comes as a bare TwilioException("Unable to
    fetch page", response), and an answer in RFC 9457's form as a TwilioServiceException."""
    if isinstance(exc, TwilioRestException):
        return exc
    if isinstance(exc, TwilioServiceException):
        return TwilioRestException(
            exc.status, exc.uri, msg=str(exc.detail or exc.title or ""), code=exc.code, method=exc.method
        )
    if isinstance(exc, TwilioException) and len(exc.args) >= 2 and hasattr(exc.args[1], "status_code"):
        response = exc.args[1]
        try:
            body = json.loads(response.text or "{}")
        except (TypeError, ValueError):
            body = {}
        body = body if isinstance(body, dict) else {}
        return TwilioRestException(
            response.status_code,
            "",
            # Twilio's usual answer says "message"; one in RFC 9457's form, "detail" and "title".
            msg=str(body.get("message") or body.get("detail") or body.get("title") or ""),
            code=body.get("code"),
        )
    return None


def describe(exc: Exception) -> NumbersError:
    """What went wrong with a request to Twilio, in words, with Twilio's own message."""
    exc = twilio_refusal(exc) or exc
    if isinstance(exc, TwilioRestException):
        if keys_refused(exc):
            return NumbersError(KEYS_REFUSED, status=400)
        message = twilio_message(exc)
        hint = REGULATORY_HINT if re.search(r"address|bundle|regulat|identity", message, re.IGNORECASE) else ""
        return NumbersError(f"Twilio said: {message}.{hint}", status=400 if 400 <= (exc.status or 0) < 500 else 502)
    return NumbersError(f"Could not reach Twilio ({type(exc).__name__}). Try again in a moment.")


def voice_webhook(cfg: Config) -> str:
    return f"{cfg.public_url}/twilio/voice"


def status_webhook(cfg: Config) -> str:
    return f"{cfg.public_url}/twilio/status"


def origin(url: str) -> str:
    """A web address's scheme and host (and port): https://oscar.example.com."""
    parts = urlsplit(url or "")
    return f"{parts.scheme}://{parts.netloc}".lower() if parts.scheme and parts.netloc else ""


def _ours(url: str, known: set[str]) -> bool:
    """A webhook of Oscar's, at its address now or one it had before (a tunnel's changes)."""
    path = urlsplit(url or "").path
    return path.endswith(("/twilio/voice", "/twilio/status")) and origin(url) in known


def _host(url: str) -> str:
    return urlsplit(url or "").hostname or url


def capabilities(value: object) -> dict[str, bool]:
    """{"voice", "sms", "mms"}: Twilio writes them "voice", "SMS" or "sms" depending on the list."""
    found = {str(key).lower(): bool(on) for key, on in value.items()} if isinstance(value, dict) else {}
    return {name: found.get(name, False) for name in ("voice", "sms", "mms")}


def number_state(cfg: Config, number, known: set[str] = frozenset()) -> tuple[str, str]:
    """Whether a number of the account reaches Oscar, as (state, what to say about it).
    `known`: the addresses Oscar has had."""
    if getattr(number, "trunk_sid", None):
        return "trunk", "A SIP trunk in Twilio answers it, so Oscar cannot be connected to it here."
    if getattr(number, "voice_receive_mode", None) == "fax":
        return "fax", "It is set to receive faxes, not calls."
    if getattr(number, "voice_application_sid", None):
        return "elsewhere", "A TwiML app in Twilio answers its calls."
    url = getattr(number, "voice_url", None) or ""
    if url == voice_webhook(cfg) and (getattr(number, "voice_method", None) or "POST").upper() == "POST":
        return "connected", "Oscar answers its calls."
    if _ours(url, set(known) | {origin(cfg.public_url)}):
        return "old_address", f"Its calls go to {_host(url)}, an older address of Oscar. Connect it again."
    if url:
        return "elsewhere", f"Its calls go to {_host(url)}."
    return "unset", "Nothing answers its calls yet."


def number_view(cfg: Config, number, known: set[str] = frozenset()) -> dict:
    state, detail = number_state(cfg, number, known)
    return {
        "sid": number.sid,
        "phone_number": number.phone_number,
        "friendly_name": number.friendly_name or number.phone_number,
        "capabilities": capabilities(number.capabilities),
        "state": state,
        "detail": detail,
    }


def calls_from(cfg: Config, store: Store) -> str:
    """The number Oscar's own calls come from: the one chosen in the dashboard, or TWILIO_PHONE_NUMBER."""
    return store.get_calls_from() or cfg.twilio_phone_number


class TwilioNumbers:
    """The Twilio account's numbers. Each method blocks (run it with asyncio.to_thread)."""

    def __init__(self, cfg: Config, store: Store, client_factory=None):
        self._cfg = cfg
        self._store = store
        self._client_factory = client_factory
        if origin(cfg.public_url):
            store.remember_oscar_address(origin(cfg.public_url))
        self._countries: list[dict] = []
        self._countries_at = 0.0
        self._lock = threading.Lock()

    def _client(self) -> Client:
        return (self._client_factory or twilio_client)(self._cfg)

    def _owned(self, client: Client) -> list:
        return list(client.incoming_phone_numbers.list(limit=NUMBERS_READ))

    def _known(self) -> set[str]:
        """The addresses Oscar has had, this one included."""
        return self._store.oscar_addresses() | {origin(self._cfg.public_url)}

    def _view(self, number) -> dict:
        return number_view(self._cfg, number, self._known())

    def _find(self, client: Client, sid: str):
        if not re.fullmatch(r"PN[0-9a-fA-F]{32}", sid or ""):
            raise NumbersError("That is not a Twilio number's SID.", status=400)
        return client.incoming_phone_numbers(sid).fetch()

    def overview(self) -> dict:
        """The account's numbers, the numbers Oscar can call from, and which one it does."""
        client = self._client()
        try:
            account = client.api.accounts(self._cfg.twilio_account_sid).fetch()
            numbers = self._owned(client)
            caller_ids = list(client.outgoing_caller_ids.list(limit=NUMBERS_READ))
        except Exception as exc:
            raise describe(exc) from None
        owned = [self._view(number) for number in numbers]
        verified = [
            {"phone_number": entry.phone_number, "friendly_name": entry.friendly_name or entry.phone_number}
            for entry in caller_ids
            if entry.phone_number not in {number["phone_number"] for number in owned}
        ]
        return {
            "trial": (getattr(account, "type", "") or "").lower() == "trial",
            "numbers": owned,
            "caller_ids": verified,
            "calls_from": calls_from(self._cfg, self._store),
            "calls_from_chosen": bool(self._store.get_calls_from()),
            "webhook": voice_webhook(self._cfg),
            # The account has more numbers than the dashboard lists.
            "more": len(numbers) >= NUMBERS_READ,
        }

    def connect_by_itself(self) -> list[str]:
        """What Oscar does with the account's numbers when it starts, so its number reaches
        the agent without a visit to the dashboard. In sentences, for the log and the dashboard.

        - A number still sending calls to an older address of Oscar (a tunnel that changed)
          is moved to this one.
        - The number Oscar calls from is connected when nothing answers its calls.
        - With no number chosen to call from and only one on the account that takes calls,
          that one becomes it (and is connected the same way).

        A number another app answers (a webhook, a TwiML app, a SIP trunk) is never taken:
        that is asked in the dashboard first. Nor is one disconnected in the dashboard:
        it stays as it is until it is connected there again."""
        client = self._client()
        known = self._known()
        left_alone = self._store.numbers_left_alone()
        try:
            numbers = self._owned(client)
        except Exception as exc:
            raise describe(exc) from None
        voice = [number for number in numbers if capabilities(number.capabilities)["voice"]]
        here = calls_from(self._cfg, self._store)
        said: list[str] = []
        if not here and len(voice) == 1:
            here = voice[0].phone_number
            self._store.set_calls_from(here)
            said.append(f"{here} is the number Oscar calls from: it is the only one on the Twilio account.")
        for number in numbers:
            state, detail = number_state(self._cfg, number, known)
            if state == "old_address":
                done = f"{number.phone_number} sent its calls to an older address of Oscar: they now come to this one."
            elif state == "unset" and number.phone_number == here and number.sid not in left_alone:
                done = f"{number.phone_number}, the number Oscar calls from, answered nothing: its calls now come to Oscar."
            else:
                if number.phone_number == here and number.sid in left_alone and state == "unset":
                    said.append(
                        f"{number.phone_number}, the number Oscar calls from, was disconnected in Your numbers, "
                        "so Oscar left it as it is. Connect it there for Oscar to answer its calls."
                    )
                elif number.phone_number == here and state != "connected":
                    said.append(
                        f"{number.phone_number}, the number Oscar calls from, does not bring its calls to Oscar. "
                        f"{detail} Connect it in Phone, Your numbers."
                    )
                continue
            try:
                self.connect(number.sid)
            except NumbersError as exc:
                said.append(f"{number.phone_number} could not be connected to Oscar: {exc}")
                continue
            said.append(done)
        return said

    def connect(self, sid: str) -> dict:
        """Have Twilio send this number's calls to Oscar."""
        client = self._client()
        try:
            number = self._find(client, sid)
            known = self._known()
            state, detail = number_state(self._cfg, number, known)
            if state in ("trunk", "fax"):
                raise NumbersError(detail, status=409)
            if state != "connected" and not _ours(number.voice_url or "", known):
                # How it answered calls until now, to put back on disconnecting.
                self._store.save_number_connection(
                    number.sid,
                    number.phone_number,
                    {
                        "voice_url": number.voice_url,
                        "voice_method": number.voice_method,
                        "voice_application_sid": number.voice_application_sid,
                        "status_callback": number.status_callback,
                        "status_callback_method": number.status_callback_method,
                    },
                )
            changes = {"voice_url": voice_webhook(self._cfg), "voice_method": "POST"}
            if number.voice_application_sid:
                # A TwiML app comes before the voice URL; an empty value takes it off.
                changes["voice_application_sid"] = ""
            # The status callback is only taken when no one else uses it.
            if not number.status_callback or _ours(number.status_callback, known):
                changes.update(status_callback=status_webhook(self._cfg), status_callback_method="POST")
            updated = client.incoming_phone_numbers(number.sid).update(**changes)
        except NumbersError:
            raise
        except Exception as exc:
            raise describe(exc) from None
        # Connected by the owner (or by Oscar): no longer one to leave alone.
        self._store.stop_leaving_number_alone(number.sid)
        return self._view(updated)

    def disconnect(self, sid: str) -> dict:
        """Put the number back as it was before Oscar was connected (or answering nothing)."""
        client = self._client()
        try:
            number = self._find(client, sid)
            known = self._known()
            if not _ours(number.voice_url or "", known):
                raise NumbersError("Oscar does not answer this number's calls.", status=409)
            before = self._store.get_number_connection(number.sid) or {}
            changes = {
                "voice_url": before.get("voice_url", ""),
                "voice_method": before.get("voice_method") or "POST",
            }
            if before.get("voice_application_sid"):
                changes["voice_application_sid"] = before["voice_application_sid"]
            if _ours(number.status_callback or "", known):
                changes["status_callback"] = before.get("status_callback", "")
                changes["status_callback_method"] = before.get("status_callback_method") or "POST"
            updated = client.incoming_phone_numbers(number.sid).update(**changes)
        except NumbersError:
            raise
        except Exception as exc:
            raise describe(exc) from None
        self._store.forget_number_connection(number.sid)
        # The owner's choice: Oscar does not connect it again by itself.
        self._store.leave_number_alone(number.sid, number.phone_number)
        return self._view(updated)

    def countries(self) -> list[dict]:
        """[{"code", "name", "types"}] of the countries Twilio has numbers in, by name."""
        with self._lock:
            if self._countries and time.monotonic() - self._countries_at < COUNTRIES_KEEP_SECONDS:
                return self._countries
        try:
            listed = list(self._client().available_phone_numbers.list())
        except Exception as exc:
            raise describe(exc) from None
        countries = []
        for country in listed:
            kinds = [kind for kind in NUMBER_TYPES if kind in (country.subresource_uris or {})]
            if country.country_code and kinds:
                countries.append({"code": country.country_code, "name": country.country or country.country_code, "types": kinds})
        countries.sort(key=lambda country: country["name"])
        with self._lock:
            self._countries, self._countries_at = countries, time.monotonic()
        return countries

    def price(self, client: Client, country: str, kind: str) -> dict | None:
        """Twilio's monthly price for a kind of number in a country: {"monthly", "unit"}, or None."""
        try:
            prices = client.pricing.v1.phone_numbers.countries(country).fetch()
        except Exception:
            return None
        wanted = kind.replace("_", "")
        for entry in prices.phone_number_prices or []:
            if not isinstance(entry, dict):
                continue
            if re.sub(r"[\s_-]", "", str(entry.get("number_type", ""))).lower() == wanted:
                monthly = entry.get("current_price") or entry.get("base_price")
                if monthly not in (None, ""):
                    # An ISO 4217 code, which Twilio's reference shows in either case.
                    return {"monthly": str(monthly), "unit": (prices.price_unit or "USD").upper()}
        return None

    def search(self, country: str, kind: str, area_code: str = "", contains: str = "") -> dict:
        """Numbers to buy that can take calls, and what one costs a month."""
        country = (country or "").upper()
        if not re.fullmatch(r"[A-Z]{2}", country):
            raise NumbersError("Choose a country.", status=400)
        if kind not in NUMBER_TYPES:
            raise NumbersError("Choose local, mobile or toll-free.", status=400)
        area_code = (area_code or "").strip()
        contains = (contains or "").strip()
        if area_code and not re.fullmatch(r"\d{2,5}", area_code):
            raise NumbersError("An area code is 2 to 5 digits.", status=400)
        if contains and not re.fullmatch(r"[0-9A-Za-z*]{2,16}", contains):
            raise NumbersError("Search for 2 to 16 digits or letters (* stands for any digit).", status=400)
        client = self._client()
        options: dict = {"voice_enabled": True, "limit": SEARCH_RESULTS}
        if area_code:
            options["area_code"] = int(area_code)
        if contains:
            options["contains"] = contains
        try:
            listing = getattr(client.available_phone_numbers(country), kind)
            found = list(listing.list(**options))
        except Exception as exc:
            raise describe(exc) from None
        return {
            "numbers": [
                {
                    "phone_number": number.phone_number,
                    "friendly_name": number.friendly_name or number.phone_number,
                    "place": ", ".join(part for part in (number.locality, number.region) if part),
                    "capabilities": capabilities(number.capabilities),
                    "address_needed": ADDRESS_NEEDS.get(number.address_requirements or "none", ""),
                    "beta": bool(number.beta),
                }
                for number in found
                if number.phone_number
            ],
            "price": self.price(client, country, kind),
        }

    def buy(self, phone_number: str) -> dict:
        """Buy a number from Twilio, already connected to Oscar. Twilio charges the account for it."""
        if not re.fullmatch(r"\+[1-9]\d{6,14}", phone_number or ""):
            raise NumbersError("That is not a number Twilio sells, like +14155550123.", status=400)
        client = self._client()
        try:
            bought = client.incoming_phone_numbers.create(
                phone_number=phone_number,
                voice_url=voice_webhook(self._cfg),
                voice_method="POST",
                status_callback=status_webhook(self._cfg),
                status_callback_method="POST",
            )
        except Exception as exc:
            if twilio_refusal(exc):
                raise describe(exc) from None
            # No answer in time (or the connection broke): Twilio may have bought it all
            # the same. Saying it failed could have it bought twice, so look first.
            bought = self._owned_number(client, phone_number)
            if bought is None:
                raise NumbersError(
                    f"Twilio did not answer in time, so it is not known whether {phone_number} was "
                    "bought. Press Refresh under Your numbers before trying again: if it is listed, it was."
                ) from None
        # The first number Oscar has is the one its calls come from.
        if not calls_from(self._cfg, self._store):
            self._store.set_calls_from(bought.phone_number)
        return self._view(bought)

    def _owned_number(self, client: Client, phone_number: str):
        """The account's number with exactly this phone number, or None (None too when Twilio cannot say)."""
        try:
            found = client.incoming_phone_numbers.list(phone_number=phone_number, limit=20)
        except Exception:
            return None
        return next((number for number in found if number.phone_number == phone_number), None)

    def choose_calls_from(self, phone_number: str) -> str:
        """Make Oscar's calls come from this number: one of the account's that takes calls,
        or a number verified in Twilio as a caller ID."""
        client = self._client()
        try:
            # Looked up by the number itself, so an account with many numbers is no problem.
            owned = [
                number
                for number in client.incoming_phone_numbers.list(phone_number=phone_number, limit=20)
                if number.phone_number == phone_number and capabilities(number.capabilities)["voice"]
            ]
            verified = [
                entry
                for entry in client.outgoing_caller_ids.list(phone_number=phone_number, limit=20)
                if entry.phone_number == phone_number
            ]
        except Exception as exc:
            raise describe(exc) from None
        if not owned and not verified:
            raise NumbersError(
                f"{phone_number} is not a number of this Twilio account that takes calls, nor one verified in it.",
                status=400,
            )
        self._store.set_calls_from(phone_number)
        # A number of the account that answers nothing is connected too, so whoever calls
        # it back reaches Oscar (one disconnected in the dashboard, or that another app
        # answers, is left as it is).
        for number in owned:
            state, _ = number_state(self._cfg, number, self._known())
            if state == "unset" and number.sid not in self._store.numbers_left_alone():
                try:
                    self.connect(number.sid)
                except NumbersError as exc:
                    logger.warning(f"{phone_number} is the number Oscar calls from, but could not be connected: {exc}")
        return phone_number
