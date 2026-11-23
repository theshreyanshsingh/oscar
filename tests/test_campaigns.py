"""Call lists: Oscar rings a list of numbers one after another (app/campaigns.py)."""

import asyncio
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app import campaigns
from app.calls import CallManager
from app.campaigns import (
    CampaignDialer,
    CampaignRequest,
    agent_for_contact,
    dialable,
    fill,
    in_calling_hours,
    outcome,
    parse_contacts,
)
from app.events import EventBus
from app.phone import PhoneCallError
from app.store import DEFAULT_AGENT, Store
from app.summary import note_do_not_call
from tests.conftest import local_client, make_config

LIST_CSV = "First name,Phone,Company\nSam,0412 345 678,Acme\nLee,+1 415 555 0123,\n"


# Reading the CSV


def test_a_csv_with_headings_gives_each_persons_number_and_details():
    contacts, headings, problems = parse_contacts(LIST_CSV, "+61")
    assert headings == ["First name", "Phone", "Company"]
    assert contacts == [
        {"number": "+61412345678", "fields": {"First name": "Sam", "Company": "Acme"}},
        {"number": "+14155550123", "fields": {"First name": "Lee"}},
    ]
    assert problems == []


def test_a_csv_without_headings_finds_the_column_of_numbers():
    contacts, headings, problems = parse_contacts("Sam;+61412345678\nLee;+61412345679\n")
    assert headings == ["Column 1", "Column 2"]
    assert [c["number"] for c in contacts] == ["+61412345678", "+61412345679"]
    assert contacts[0]["fields"] == {"Column 1": "Sam"}


def test_rows_that_cannot_be_called_are_explained_by_their_row_in_the_file():
    text = "Name,Mobile\nSam,0412 345 678\nBad,abc\n\nDup,+61 412 345 678\nNone,\n"
    contacts, _, problems = parse_contacts(text, "+61")
    assert [c["number"] for c in contacts] == ["+61412345678"]
    assert problems == [
        "Row 3: abc is not a phone number.",
        "Row 5: +61 412 345 678 is already on the list.",
        "Row 6: no phone number.",
    ]


def test_a_number_without_its_country_code_needs_one_given():
    contacts, _, problems = parse_contacts("Phone\n0412 345 678\n")
    assert contacts == []
    assert problems == ["Row 2: 0412 345 678 has no country code. Add one below, or write it as +…."]


def test_a_file_without_any_numbers_says_so():
    assert parse_contacts("Name,Town\nSam,Perth\n")[2] == [
        'No column of phone numbers was found. Give it a heading such as "Phone".'
    ]
    assert parse_contacts("\n\n")[2] == ["The file has no rows."]


def test_a_list_has_a_most_numbers(monkeypatch):
    monkeypatch.setattr(campaigns, "CONTACTS_MOST", 2)
    text = "Phone\n" + "".join(f"+6141234567{i}\n" for i in range(3))
    assert parse_contacts(text) == ([], ["Phone"], ["A list can have 2 numbers at most."])


@pytest.mark.parametrize(
    "raw, code, number",
    [
        ("+61 412 345 678", "", "+61412345678"),
        ("0061 412 345 678", "", "+61412345678"),
        ("0412 345 678", "+61", "+61412345678"),
        ("(415) 555-0123", "+1", "+14155550123"),
        ("0412 345 678", "", None),
        ("+61 412", "", None),
        ("call me", "+61", None),
        # The country code already there, as Excel keeps "+61 412 345 678" in a number cell.
        ("61412345678", "+61", "+61412345678"),
        ("14155550123", "+1", "+14155550123"),
        ("1 (415) 555-0123", "+1", "+14155550123"),
        ("919876543210", "+91", "+919876543210"),
        # A number of the country that only looks as if it starts with the code.
        ("9123456789", "+91", "+919123456789"),
        # An Italian number keeps its 0 after the country code.
        ("06 1234 5678", "+39", "+390612345678"),
        # Numbers libphonenumber does not know for real ones follow the plain rule.
        ("07700 900123", "+44", "+447700900123"),
        ("12345678", "+61", "+6112345678"),
    ],
)
def test_what_can_be_dialled(raw, code, number):
    assert dialable(raw, code) == number


def test_placeholders_are_filled_whatever_their_spacing_and_capitals():
    fields = {"First name": "Sam", "Company": "Acme"}
    assert fill("Hi {{ First Name }}, from {{company}}{{nothing}}.", fields) == "Hi Sam, from Acme."


# The list's settings


def _request(**changes) -> CampaignRequest:
    return CampaignRequest(**{"name": "Quotes", "contacts_csv": LIST_CSV, **changes})


@pytest.mark.parametrize(
    "changes",
    [
        {"calling_from": "18:00", "calling_until": "09:00"},
        {"calling_from": "9am"},
        {"timezone": "Mars/Olympus"},
        {"country_code": "61a"},
        {"at_once": 6},
        {"name": ""},
    ],
)
def test_a_list_with_settings_that_cannot_work_is_refused(changes):
    with pytest.raises(ValidationError):
        _request(**changes)


def test_settings_are_tidied():
    request = _request(country_code="61", calling_from="9:05", timezone="Australia/Sydney")
    assert (request.country_code, request.calling_from) == ("+61", "09:05")


def test_each_call_is_told_about_the_list_and_the_person():
    campaign = {"name": "Quotes", "instructions": "Ask {{First name}} about the fence.", "greeting": "Hi {{first_name}}!", "voicemail_message": ""}
    contact = {"number": "+61412345678", "fields": {"First name": "Sam", "Company": "Acme"}}
    agent = agent_for_contact(DEFAULT_AGENT.model_copy(update={"voicemail_message": "Call us back."}), campaign, contact)
    assert agent.system_prompt.startswith(DEFAULT_AGENT.system_prompt)
    assert 'list of calls you are making, called "Quotes"' in agent.system_prompt
    assert "Ask Sam about the fence." in agent.system_prompt
    assert agent.system_prompt.endswith("- First name: Sam\n- Company: Acme")
    assert agent.greeting == "Hi Sam!"
    assert agent.voicemail_message == "Call us back."  # the list has none of its own


def test_calls_are_only_made_in_the_lists_hours_in_its_time_zone():
    campaign = {"calling_from": 9 * 60, "calling_until": 18 * 60, "timezone": "Australia/Perth"}
    # 01:30 UTC is 09:30 in Perth, 23:00 UTC is 07:00 the next morning there.
    assert in_calling_hours(campaign, datetime(2026, 11, 23, 1, 30, tzinfo=timezone.utc))
    assert not in_calling_hours(campaign, datetime(2026, 11, 23, 23, 0, tzinfo=timezone.utc))
    assert not in_calling_hours(campaign, datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc))  # 18:00


# How each call went


def _call(**fields) -> dict:
    return {"status": "ended", "error": "", "answered_by": "", "transferred_to": "", **fields}


@pytest.mark.parametrize(
    "call, spoke, expected",
    [
        (_call(), True, ("answered", "")),
        (_call(), False, ("no_answer", "Picked up, but no one spoke.")),
        (_call(answered_by="voicemail"), True, ("voicemail", "A voicemail answered.")),
        (_call(answered_by="fax"), False, ("failed", "A fax machine answered.")),
        (_call(status="failed", error="No one answered."), False, ("no_answer", "No one answered.")),
        (_call(status="failed", error="The line was busy."), False, ("busy", "The line was busy.")),
        (_call(status="failed", error="Twilio could not connect the call."), False, ("failed", "Twilio could not connect the call.")),
        (_call(transferred_to="+61412345678"), True, ("answered", "Put through to a person.")),
    ],
)
def test_how_a_call_went(call, spoke, expected):
    assert outcome(call, spoke) == expected


# The dialer


class Dialling:
    """Stands in for app.phone.place_outbound_call: every call is placed and left ringing."""

    def __init__(self, calls: CallManager, refuse: str | PhoneCallError = ""):
        self.calls = calls
        self.refuse = refuse
        self.placed: list[tuple[str, object, str]] = []
        self.from_numbers: list[str] = []

    async def __call__(self, cfg, calls, number, *, agent=None, campaign_id="", from_number=""):
        if self.refuse:
            raise self.refuse if isinstance(self.refuse, PhoneCallError) else PhoneCallError(self.refuse)
        self.placed.append((number, agent, campaign_id))
        self.from_numbers.append(from_number)
        return calls.create("phone_outbound", status="ringing", to_number=number, campaign_id=campaign_id)


class Credit:
    def __init__(self, too_little: str = ""):
        self.too_little = too_little

    async def too_little_for_a_call(self):
        return self.too_little


@pytest.fixture
def dialer(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, max_calls=4)
    store = Store(cfg.db_path)
    calls = CallManager(store, EventBus())
    dialling = Dialling(calls)
    monkeypatch.setattr(campaigns, "place_outbound_call", dialling)
    monkeypatch.setattr(campaigns, "in_calling_hours", lambda campaign, now=None: True)
    yield CampaignDialerHarness(cfg, calls, store, dialling)
    store.close()


class CampaignDialerHarness:
    def __init__(self, cfg, calls, store, dialling):
        self.cfg, self.calls, self.store, self.dialling = cfg, calls, store, dialling
        self.credit = Credit()
        self.dialer = CampaignDialer(cfg, calls, self.credit)

    def make(self, numbers: list[str], *, at_once: int = 1, status: str = "running") -> dict:
        contacts = [{"number": number, "fields": {"Name": f"Person {i}"}} for i, number in enumerate(numbers)]
        return self.store.create_campaign(
            {
                "name": "Quotes",
                "status": status,
                "instructions": "Ask {{name}} about the quote.",
                "greeting": "",
                "voicemail_message": "",
                "calling_from": 9 * 60,
                "calling_until": 18 * 60,
                "timezone": "UTC",
                "at_once": at_once,
            },
            contacts,
        )

    def tick(self):
        asyncio.run(self.dialer.tick())

    def statuses(self, campaign: dict) -> list[str]:
        return [contact["status"] for contact in self.store.campaign_contacts(campaign["id"])]

    def end(self, number: str, **fields):
        call = next(c for c in self.store.list_calls(100) if c["to_number"] == number)
        self.store.update_call(call["id"], **{"status": "ended", "ended_at": "2026-11-23T10:00:00+00:00", **fields})
        return call


NUMBERS = ["+61412345671", "+61412345672", "+61412345673"]


def test_a_running_list_is_called_one_number_at_a_time(dialer):
    campaign = dialer.make(NUMBERS)
    dialer.tick()
    assert dialer.statuses(campaign) == ["calling", "waiting", "waiting"]
    ((number, agent, campaign_id),) = dialer.dialling.placed
    assert (number, campaign_id) == (NUMBERS[0], campaign["id"])
    assert "Ask Person 0 about the quote." in agent.system_prompt
    dialer.tick()  # still on the first call
    assert len(dialer.dialling.placed) == 1

    call = dialer.end(NUMBERS[0])
    dialer.store.add_message(call["id"], "user", "Hello?")
    dialer.tick()
    assert dialer.statuses(campaign)[:2] == ["answered", "calling"]


def test_several_at_once_leaves_a_line_free_for_callers(dialer):
    campaign = dialer.make(NUMBERS + ["+61412345674", "+61412345675"], at_once=5)
    dialer.tick()
    # MAX_CALLS is 4 here: three calls out, one line kept for someone calling in.
    assert dialer.statuses(campaign).count("calling") == 3


def test_numbers_that_asked_not_to_be_called_or_are_blocked_are_skipped(dialer):
    dialer.store.add_do_not_call("+61 412 345 671")
    dialer.store.save_blocked_numbers(["0412 345 672"])
    campaign = dialer.make(NUMBERS)
    dialer.tick()
    contacts = dialer.store.campaign_contacts(campaign["id"])
    assert [(c["status"], c["detail"]) for c in contacts] == [
        ("skipped", "Asked not to be called again."),
        ("skipped", "On the blocked list."),
        ("calling", ""),
    ]


def test_a_list_waits_outside_its_hours(dialer, monkeypatch):
    monkeypatch.setattr(campaigns, "in_calling_hours", lambda campaign, now=None: False)
    campaign = dialer.make(NUMBERS)
    dialer.tick()
    assert dialer.dialling.placed == []
    campaign = dialer.store.get_campaign(campaign["id"])
    assert campaign["status"] == "running"
    assert campaign["status_detail"] == "Waiting for calling hours (09:00 to 18:00, UTC)."


def test_a_list_pauses_when_the_key_runs_out(dialer):
    dialer.credit.too_little = "The OpenRouter key has run out."
    campaign = dialer.make(NUMBERS)
    dialer.tick()
    campaign = dialer.store.get_campaign(campaign["id"])
    assert (campaign["status"], campaign["status_detail"]) == ("paused", "Paused: The OpenRouter key has run out.")
    assert dialer.dialling.placed == []


def test_a_call_twilio_refuses_is_noted_and_the_list_goes_on(dialer):
    dialer.dialling.refuse = "Twilio refused the call: not a valid number."
    campaign = dialer.make(NUMBERS[:1])
    dialer.tick()
    (contact,) = dialer.store.campaign_contacts(campaign["id"])
    assert (contact["status"], contact["detail"]) == ("failed", "Twilio refused the call: not a valid number.")
    dialer.tick()
    assert dialer.store.get_campaign(campaign["id"])["status"] == "done"


def test_a_list_pauses_when_twilio_refuses_the_account_not_the_person(dialer):
    # Wrong keys, a suspended account or a number Oscar cannot call from: every call
    # would be refused, so no one is marked failed and the list waits for the fix.
    refusal = "Twilio refused the call: The source phone number provided is not yet verified for your account."
    dialer.dialling.refuse = PhoneCallError(refusal, stops_lists=True)
    campaign = dialer.make(NUMBERS, at_once=3)
    dialer.tick()
    campaign = dialer.store.get_campaign(campaign["id"])
    assert (campaign["status"], campaign["status_detail"]) == ("paused", f"Paused: {refusal}")
    contacts = dialer.store.campaign_contacts(campaign["id"])
    assert {(contact["status"], contact["detail"]) for contact in contacts} == {("waiting", "")}

    dialer.dialling.refuse = ""
    dialer.store.update_campaign(campaign["id"], status="running", status_detail="")
    dialer.tick()
    assert [number for number, _, _ in dialer.dialling.placed] == NUMBERS[:3]


def test_a_busy_or_unreachable_twilio_leaves_the_person_to_be_called_a_minute_later(dialer, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(campaigns.time, "monotonic", lambda: clock[0])
    dialer.dialling.refuse = PhoneCallError("Could not reach Twilio. Try again in a moment.", try_later=True)
    campaign = dialer.make(NUMBERS[:1])
    dialer.tick()
    (contact,) = dialer.store.campaign_contacts(campaign["id"])
    assert (contact["status"], contact["detail"]) == ("waiting", "")
    campaign = dialer.store.get_campaign(campaign["id"])
    assert campaign["status"] == "running"
    assert campaign["status_detail"].startswith(campaigns.TRYING_AGAIN)

    dialer.dialling.refuse = ""
    clock[0] += campaigns.REST_SECONDS - 1
    dialer.tick()
    assert dialer.dialling.placed == []  # not before the minute is up
    clock[0] += 1
    dialer.tick()
    assert [number for number, _, _ in dialer.dialling.placed] == NUMBERS[:1]
    assert dialer.store.get_campaign(campaign["id"])["status_detail"] == ""


def test_a_call_twilio_may_have_placed_is_settled_by_how_it_goes(dialer):
    # Twilio did not answer in time: the person is not failed (nor called again) until the call says.
    call = dialer.calls.create("phone_outbound", status="connecting", to_number=NUMBERS[0])
    dialer.dialling.refuse = PhoneCallError("Twilio did not answer in time.", call_id=call["id"])
    campaign = dialer.make(NUMBERS[:1])
    dialer.tick()
    (contact,) = dialer.store.campaign_contacts(campaign["id"])
    assert (contact["status"], contact["call_id"]) == ("calling", call["id"])
    dialer.dialling.refuse = ""
    dialer.tick()
    assert dialer.dialling.placed == []
    dialer.calls.finish(call["id"], "No one answered.")
    dialer.tick()
    (contact,) = dialer.store.campaign_contacts(campaign["id"])
    assert contact["status"] != "calling"


def test_a_list_paused_while_a_call_is_placed_makes_no_more_calls(dialer):
    campaign = dialer.make(NUMBERS[:3], at_once=3)
    placing = dialer.dialling.__call__

    async def pause_while_placing(*args, **kwargs):
        call = await placing(*args, **kwargs)
        dialer.store.update_campaign(campaign["id"], status="paused", status_detail="")
        return call

    dialer.dialling.__class__ = type("Pausing", (type(dialer.dialling),), {"__call__": lambda self, *a, **k: pause_while_placing(*a, **k)})
    dialer.tick()
    assert [number for number, _, _ in dialer.dialling.placed] == NUMBERS[:1]
    assert dialer.store.get_campaign(campaign["id"])["status"] == "paused"


def test_voicemails_and_unanswered_calls_can_be_called_again(dialer):
    campaign = dialer.make(NUMBERS[:2], at_once=2)
    dialer.tick()
    dialer.end(NUMBERS[0], answered_by="voicemail")
    dialer.end(NUMBERS[1], status="failed", error="No one answered.")
    dialer.tick()
    assert dialer.statuses(campaign) == ["voicemail", "no_answer"]
    assert dialer.store.get_campaign(campaign["id"])["status"] == "done"
    assert dialer.store.call_again(campaign["id"], campaigns.CALL_AGAIN_STATUSES) == 2
    assert dialer.statuses(campaign) == ["waiting", "waiting"]


def test_a_paused_list_makes_no_calls(dialer):
    dialer.make(NUMBERS, status="paused")
    dialer.tick()
    assert dialer.dialling.placed == []


def test_a_list_left_dialling_by_a_stopped_server_is_tidied(dialer):
    campaign = dialer.make(NUMBERS[:1])
    (contact,) = dialer.store.campaign_contacts(campaign["id"])
    dialer.store.update_contact(contact["id"], status="calling")  # but no call was made
    dialer.dialer.settle()
    (contact,) = dialer.store.campaign_contacts(campaign["id"])
    assert (contact["status"], contact["detail"]) == ("failed", "The server stopped as it was being dialled.")


# Asked not to be called again


def test_someone_who_asks_not_to_be_called_again_goes_on_the_do_not_call_list(tmp_path):
    store = Store(tmp_path / "oscar.db")
    calls = CallManager(store, EventBus())
    call = store.create_call("phone_outbound", status="ended", to_number="+61412345678")
    note_do_not_call(calls, call)
    note_do_not_call(calls, call)  # once is enough
    assert [entry["number"] for entry in store.get_do_not_call()] == ["61412345678"]
    notes = [m["text"] for m in store.get_messages(call["id"])]
    assert notes == ["Asked not to be called again: +61412345678 is on the do-not-call list."]
    store.close()


# Through the dashboard's API


def test_a_csv_is_previewed_before_it_becomes_a_list(client):
    preview = client.post(
        "/api/campaigns/preview",
        json={"contacts_csv": LIST_CSV + "Bad,abc,\n", "country_code": "+61", "templates": ["Hi {{first_name}} {{town}}"]},
    ).json()
    assert preview["count"] == 2
    assert preview["headings"] == ["First name", "Phone", "Company"]
    assert preview["problems"] == ["Row 4: abc is not a phone number."]
    assert preview["unknown_placeholders"] == ["town"]


def test_a_list_is_made_run_paused_and_deleted(client):
    made = client.post(
        "/api/campaigns", json={"name": "Quotes", "contacts_csv": LIST_CSV, "country_code": "+61", "start": False}
    )
    assert made.status_code == 200, made.text
    campaign = made.json()["campaign"]
    assert (campaign["status"], campaign["total"], campaign["counts"]) == ("paused", 2, {"waiting": 2})

    detail = client.get(f"/api/campaigns/{campaign['id']}").json()
    assert detail["campaign"]["calling_from"] == "09:00"
    assert [c["number"] for c in detail["contacts"]] == ["+61412345678", "+14155550123"]

    assert client.post(f"/api/campaigns/{campaign['id']}/resume").json()["campaign"]["status"] == "running"
    assert client.post(f"/api/campaigns/{campaign['id']}/pause").json()["campaign"]["status"] == "paused"
    assert client.post(f"/api/campaigns/{campaign['id']}/stop").json()["campaign"]["status"] == "stopped"
    assert client.get("/api/campaigns").json()["campaigns"][0]["id"] == campaign["id"]
    assert client.delete(f"/api/campaigns/{campaign['id']}").status_code == 200
    assert client.get(f"/api/campaigns/{campaign['id']}").status_code == 404


def test_a_list_without_numbers_is_refused(client):
    response = client.post("/api/campaigns", json={"name": "Quotes", "contacts_csv": "Phone\nabc\n"})
    assert response.status_code == 400
    assert response.json()["detail"] == "The list has no phone numbers Oscar can call."


def test_a_list_cannot_start_without_phone_calls_set_up(tmp_path):
    with local_client(make_config(tmp_path, public_url="")) as client:
        response = client.post("/api/campaigns", json={"name": "Quotes", "contacts_csv": LIST_CSV, "country_code": "+61"})
        assert response.status_code == 400
        assert "PUBLIC_URL" in response.json()["detail"]


def test_a_list_cannot_start_without_a_number_to_call_from(tmp_path):
    from app.phone import NO_NUMBER_TO_CALL_FROM

    with local_client(make_config(tmp_path, twilio_phone_number="")) as client:
        response = client.post("/api/campaigns", json={"name": "Quotes", "contacts_csv": LIST_CSV, "country_code": "+61"})
        assert response.status_code == 400
        assert response.json()["detail"] == NO_NUMBER_TO_CALL_FROM


def test_list_calls_come_from_the_number_chosen_in_the_dashboard(dialer):
    dialer.store.set_calls_from("+61255550199")
    dialer.make(NUMBERS[:1])
    dialer.tick()
    assert dialer.dialling.from_numbers == ["+61255550199"]


def test_a_list_pauses_without_a_number_to_call_from(tmp_path, monkeypatch):
    from app.phone import NO_NUMBER_TO_CALL_FROM

    cfg = make_config(tmp_path, twilio_phone_number="")
    store = Store(cfg.db_path)
    calls = CallManager(store, EventBus())
    dialling = Dialling(calls)
    monkeypatch.setattr(campaigns, "place_outbound_call", dialling)
    monkeypatch.setattr(campaigns, "in_calling_hours", lambda campaign, now=None: True)
    harness = CampaignDialerHarness(cfg, calls, store, dialling)
    campaign = harness.make(NUMBERS[:1])
    harness.tick()
    campaign = store.get_campaign(campaign["id"])
    assert (campaign["status"], campaign["status_detail"]) == ("paused", f"Paused. {NO_NUMBER_TO_CALL_FROM}")
    assert dialling.placed == []
    store.close()


def test_the_do_not_call_list_can_be_added_to_and_taken_from(client):
    assert client.post("/api/do-not-call", json={"number": "0412"}).status_code == 400
    listed = client.post("/api/do-not-call", json={"number": "+61 412 345 678"}).json()["do_not_call"]
    assert [entry["number"] for entry in listed] == ["61412345678"]
    assert client.get("/api/campaigns").json()["do_not_call"] == listed
    assert client.delete("/api/do-not-call/61412345678").json()["do_not_call"] == []


def test_calling_a_finished_list_again_starts_it_again(client):
    campaign = client.post(
        "/api/campaigns", json={"name": "Quotes", "contacts_csv": LIST_CSV, "country_code": "+61", "start": False}
    ).json()["campaign"]
    store = client.app.state.store
    for contact in store.campaign_contacts(campaign["id"]):
        store.update_contact(contact["id"], status="no_answer")
    store.update_campaign(campaign["id"], status="done")
    again = client.post(f"/api/campaigns/{campaign['id']}/call-again").json()
    assert again["again"] == 2
    assert again["campaign"]["status"] == "running"
    assert again["campaign"]["counts"] == {"waiting": 2}


def test_with_one_line_a_list_still_calls(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, max_calls=1)
    store = Store(cfg.db_path)
    calls = CallManager(store, EventBus())
    dialling = Dialling(calls)
    monkeypatch.setattr(campaigns, "place_outbound_call", dialling)
    monkeypatch.setattr(campaigns, "in_calling_hours", lambda campaign, now=None: True)
    harness = CampaignDialerHarness(cfg, calls, store, dialling)
    campaign = harness.make(NUMBERS[:2])
    harness.tick()
    assert harness.statuses(campaign) == ["calling", "waiting"]
    store.close()


def test_a_title_above_the_headings_is_left_out():
    text = "Customer contact list,,\nNovember 2026,,\n,,\nName,Phone,Quote\nSam Taylor,+61 412 345 678,Back fence\nLee Chen,+61 498 765 432,Gate\n"
    contacts, headings, problems = parse_contacts(text)
    assert headings == ["Name", "Phone", "Quote"]
    assert problems == []
    assert [contact["fields"]["Name"] for contact in contacts] == ["Sam Taylor", "Lee Chen"]


def test_a_contact_name_column_is_not_taken_for_the_numbers():
    text = "Contact name,Phone number\nSam Taylor,+61 412 345 678\nLee Chen,+61 498 765 432\n"
    contacts, headings, problems = parse_contacts(text)
    assert [contact["number"] for contact in contacts] == ["+61412345678", "+61498765432"]
    assert contacts[0]["fields"] == {"Contact name": "Sam Taylor"}
    assert problems == []


def test_headings_over_numbers_that_cannot_be_read_still_count_as_headings():
    contacts, headings, problems = parse_contacts("Name,Phone\nSam,n/a\n")
    assert (contacts, headings, problems) == ([], ["Name", "Phone"], ["Row 2: n/a is not a phone number."])


@pytest.mark.parametrize(
    ("text", "number"),
    [
        # A customer, order or account number is never taken for the phone numbers.
        ("Customer number,Name,Phone\n4155550199,Ann,+1 415 555 0123\n", "+14155550123"),
        ("Order number,Name,Mobile\n10023456,Ann,0412 345 678\n", "+61412345678"),
        ("Account,Contact number\n4155550199,+1 415 555 0123\n", "+14155550123"),
        # A plain heading of phone numbers comes before one that only may be.
        ("Number,Name,Phone\n4155550199,Ann,+1 415 555 0123\n", "+14155550123"),
        # "Tel" is a word, not three letters of "Hotel".
        ("Hotel,Tel.\n4155550199,+1 415 555 0123\n", "+14155550123"),
    ],
)
def test_the_column_of_phone_numbers_is_the_one_headed_for_them(text, number):
    code = "+61" if "0412" in text else "+1"
    contacts, _, problems = parse_contacts(text, code)
    assert [contact["number"] for contact in contacts] == [number]
    assert problems == []


def test_a_heading_with_a_few_numbers_below_it_is_not_enough():
    # "Contact" over names, one of which is a number: most of the column has to be numbers.
    text = "Contact,Phone\nAnn,+1 415 555 0123\n4155550100,+1 415 555 0124\nLee,+1 415 555 0125\n"
    contacts, _, _ = parse_contacts(text)
    assert [contact["number"] for contact in contacts] == ["+14155550123", "+14155550124", "+14155550125"]


@pytest.mark.parametrize(
    ("raw", "number"),
    [
        ("+44 (0)20 7946 0958", "+442079460958"),
        ("+61 (0)412 345 678", "+61412345678"),
        ("0044 (0)20 7946 0958", "+442079460958"),
    ],
)
def test_a_trunk_zero_written_in_brackets_comes_off(raw, number):
    assert dialable(raw) == number


def test_a_file_with_old_mac_line_endings_is_read():
    contacts, headings, problems = parse_contacts("Name,Phone\rSam,+61 412 345 678\rLee,+61 498 765 432\r")
    assert (len(contacts), headings, problems) == (2, ["Name", "Phone"], [])


def test_a_file_that_cannot_be_read_as_rows_says_so(client):
    huge = "Name,Phone\nSam," + "1" * 200_000 + "\n"
    response = client.post("/api/campaigns/preview", json={"contacts_csv": huge})
    assert response.status_code == 200
    assert response.json()["problems"][0].startswith("The file could not be read as rows")
    response = client.post("/api/campaigns", json={"name": "Huge", "contacts_csv": huge})
    assert response.status_code == 400
