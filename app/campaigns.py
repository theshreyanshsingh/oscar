"""Call lists: Oscar rings a list of numbers, one after another, and notes how each call went.

The owner gives a CSV (a spreadsheet saved as CSV) with a column of phone
numbers and, if they like, more columns about each person (name, company, what
they ordered). Each call gets the list's own instructions on top of Agent
settings, with {{column}} filled in for that person, and Oscar is told the
whole row. Calls are only made in the list's calling hours, in its time zone,
and a few at a time at most, always leaving a line free for people calling in.

Numbers on the blocked list or the do-not-call list (people who asked on an
earlier call not to be called again, app/summary.py) are skipped.

The loop (CampaignDialer.tick) runs every few seconds in the web server. Where
each number has got to is kept in the database, so a restart carries on.
"""

import csv
import io
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.calls import CallManager, lines_busy
from app.config import Config
from app.costs import KeyCredit
from app.numbers import calls_from
from app.phone import NO_NUMBER_TO_CALL_FROM, PhoneCallError, is_blocked, place_outbound_call
from app.store import VOICEMAIL_MESSAGE_CHARS, AgentSettings, Store

# The most numbers one list takes, and how big its CSV can be.
CONTACTS_MOST = 5000
CSV_MOST_CHARACTERS = 2_000_000
# Calls at once for one list. One line is always left free for callers (MAX_CALLS).
AT_ONCE_MOST = 5
# How often the dialer looks for numbers to call.
TICK_SECONDS = 5
# Each person's details, as Oscar is told them: the most columns, and characters a value.
FIELDS_MOST = 20
FIELD_CHARS = 300

# Where each number of a list has got to.
CONTACT_STATUSES = ("waiting", "calling", "answered", "voicemail", "no_answer", "busy", "failed", "skipped")
# What "Call again" puts back in line.
CALL_AGAIN_STATUSES = ("voicemail", "no_answer", "busy", "failed")

# A column holding the numbers is found by its heading.
_NUMBER_HEADING = re.compile(r"phone|mobile|cell|number|tel|whatsapp|contact", re.IGNORECASE)
_PLACEHOLDER = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")

# How the note of a voicemail left begins (app/agent.py, leave_voicemail).
LEFT_A_MESSAGE = "A voicemail answered. Left"

CAMPAIGN_NOTE = (
    'This call is one of a list of calls you are making, called "{name}". You called them: '
    "they did not call you."
)
PERSON_NOTE = "What you know about the person you are calling:\n{lines}"


def field_key(heading: str) -> str:
    """A column's heading as a placeholder names it: "First name" is {{first_name}}."""
    return re.sub(r"[^a-z0-9]+", "_", heading.casefold()).strip("_")


def fill(template: str, fields: dict[str, str]) -> str:
    """The template with each {{column}} replaced by this person's value ("" when they have none)."""
    by_key = {field_key(heading): value for heading, value in fields.items()}
    return _PLACEHOLDER.sub(lambda match: by_key.get(field_key(match.group(1)), ""), template)


def placeholders(template: str) -> list[str]:
    return [field_key(name) for name in _PLACEHOLDER.findall(template)]


def dialable(raw: str, country_code: str = "") -> str | None:
    """A number as Twilio dials it (+61412345678), or None when it cannot be one.

    "0412 345 678" with country code +61 becomes +61412345678 (the 0 before the
    area code goes); "0061 412..." and "+61 412..." need no country code.
    """
    text = raw.strip()
    if not text or re.search(r"[^\d\s()+.\-/]", text):
        return None
    digits = re.sub(r"\D", "", text)
    if text.startswith("+"):
        number = digits
    elif digits.startswith("00"):
        number = digits[2:]
    elif country_code:
        number = re.sub(r"\D", "", country_code) + digits.lstrip("0")
    else:
        return None
    if not 8 <= len(number) <= 15 or number.startswith("0"):
        return None
    return "+" + number


def parse_contacts(text: str, country_code: str = "") -> tuple[list[dict], list[str], list[str]]:
    """The people in a CSV: ([{"number", "fields"}], the headings, problems as sentences).

    The first row is the headings when it has a heading for the numbers ("Phone",
    "Mobile", "Number"...). Without headings, the first column whose values look
    like phone numbers holds them, and the columns are "Column 1", "Column 2"...
    The same number twice is called once.
    """
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    # (Row number in the file, its cells), blank rows left out.
    numbered = [
        (line, row)
        for line, row in enumerate(csv.reader(io.StringIO(text), dialect), start=1)
        if any(cell.strip() for cell in row)
    ]
    rows = [row for _, row in numbered]
    if not rows:
        return [], [], ["The file has no rows."]

    first = [cell.strip() for cell in rows[0]]
    column = next((i for i, heading in enumerate(first) if _NUMBER_HEADING.search(heading)), None)
    if column is not None and dialable(first[column], "1") is None:
        headings, body = first, numbered[1:]
    else:
        width = max(len(row) for row in rows)
        sample = rows[:20]
        column = next(
            (
                i
                for i in range(width)
                if sum(1 for row in sample if i < len(row) and dialable(row[i], "1")) > len(sample) / 2
            ),
            None,
        )
        if column is None:
            return [], [], ['No column of phone numbers was found. Give it a heading such as "Phone".']
        headings, body = [f"Column {i + 1}" for i in range(width)], numbered

    headings = [heading or f"Column {i + 1}" for i, heading in enumerate(headings)]
    contacts: list[dict] = []
    problems: list[str] = []
    seen: set[str] = set()
    for line, row in body:
        raw = row[column].strip() if column < len(row) else ""
        number = dialable(raw, country_code)
        if number is None:
            if not raw:
                problems.append(f"Row {line}: no phone number.")
            elif not country_code and not raw.startswith(("+", "00")) and dialable(raw, "1"):
                problems.append(f"Row {line}: {raw} has no country code. Add one below, or write it as +….")
            else:
                problems.append(f"Row {line}: {raw} is not a phone number.")
            continue
        if number in seen:
            problems.append(f"Row {line}: {raw} is already on the list.")
            continue
        seen.add(number)
        fields = {
            heading[:60]: row[i].strip()[:FIELD_CHARS]
            for i, heading in enumerate(headings[:FIELDS_MOST + 1])
            if i != column and i < len(row) and row[i].strip()
        }
        contacts.append({"number": number, "fields": dict(list(fields.items())[:FIELDS_MOST])})
        if len(contacts) > CONTACTS_MOST:
            return [], headings, [f"A list can have {CONTACTS_MOST} numbers at most."]
    return contacts, headings, problems


def minutes_of(clock: str) -> int:
    """"09:30" as minutes after midnight. Raises ValueError for anything else."""
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", clock.strip())
    if not match:
        raise ValueError("Write the time as hours and minutes, like 09:00.")
    return int(match[1]) * 60 + int(match[2])


def clock_of(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def in_calling_hours(campaign: dict, now: datetime | None = None) -> bool:
    local = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(campaign["timezone"]))
    minute = local.hour * 60 + local.minute
    return campaign["calling_from"] <= minute < campaign["calling_until"]


def country_code(value: str) -> str:
    """"61" or "+61" as "+61"; "" stays "". Raises ValueError for anything else."""
    value = value.strip()
    if value and not re.fullmatch(r"\+?[1-9]\d{0,3}", value):
        raise ValueError("Write the country code as + and up to four digits, like +61.")
    return ("+" + value.lstrip("+")) if value else ""


class CampaignRequest(BaseModel):
    """A new call list, as the dashboard sends it."""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=80)
    contacts_csv: str = Field(min_length=1, max_length=CSV_MOST_CHARACTERS)
    # Put in front of numbers written without one ("0412 345 678"): "+61".
    country_code: str = Field(default="", max_length=6)
    instructions: str = Field(default="", max_length=4000)
    # "" keeps Agent settings' own.
    greeting: str = Field(default="", max_length=500)
    voicemail_message: str = Field(default="", max_length=VOICEMAIL_MESSAGE_CHARS)
    calling_from: str = "09:00"
    calling_until: str = "18:00"
    timezone: str = Field(default="UTC", max_length=60)
    at_once: int = Field(default=1, ge=1, le=AT_ONCE_MOST)
    start: bool = True

    @field_validator("country_code")
    @classmethod
    def country_code_is_digits(cls, value: str) -> str:
        return country_code(value)

    @field_validator("timezone")
    @classmethod
    def timezone_exists(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"{value} is not a time zone Oscar knows, such as Australia/Sydney.") from None
        return value

    @field_validator("calling_from", "calling_until")
    @classmethod
    def clock_reads(cls, value: str) -> str:
        return clock_of(minutes_of(value))

    @model_validator(mode="after")
    def hours_in_order(self) -> "CampaignRequest":
        if minutes_of(self.calling_from) >= minutes_of(self.calling_until):
            raise ValueError("The calling hours have to end after they start (on the same day).")
        return self


def time_zones() -> list[str]:
    return sorted(zone for zone in available_timezones() if "/" in zone and not zone.startswith(("Etc/", "SystemV/")))


def agent_for_contact(agent: AgentSettings, campaign: dict, contact: dict) -> AgentSettings:
    """Agent settings for one call of the list: the list's instructions and this person's details added."""
    fields = contact["fields"]
    parts = [agent.system_prompt, CAMPAIGN_NOTE.format(name=campaign["name"])]
    instructions = fill(campaign["instructions"], fields).strip()
    if instructions:
        parts.append(instructions)
    if fields:
        lines = "\n".join(f"- {heading}: {value}" for heading, value in fields.items())
        parts.append(PERSON_NOTE.format(lines=lines))
    changes: dict = {"system_prompt": "\n\n".join(parts)}
    if campaign["greeting"]:
        changes["greeting"] = fill(campaign["greeting"], fields).strip()
    if campaign["voicemail_message"]:
        changes["voicemail_message"] = fill(campaign["voicemail_message"], fields).strip()
    return agent.model_copy(update=changes)


def outcome(call: dict, caller_spoke: bool) -> tuple[str, str]:
    """How a finished call of the list went, as (status, detail)."""
    if call["answered_by"] == "voicemail":
        return "voicemail", "A voicemail answered."
    if call["answered_by"] == "fax":
        return "failed", "A fax machine answered."
    if call["status"] == "failed":
        reason = call["error"] or "The call failed."
        if reason.startswith("No one answered"):
            return "no_answer", reason
        if reason.startswith("The line was busy"):
            return "busy", reason
        return "failed", reason
    if call["transferred_to"]:
        return "answered", "Put through to a person."
    if caller_spoke:
        return "answered", ""
    return "no_answer", "Picked up, but no one spoke."


class CampaignDialer:
    """Makes the calls of every running list (tick), a few at a time, within their hours."""

    def __init__(self, cfg: Config, calls: CallManager, credit: KeyCredit | None = None):
        self._cfg = cfg
        self._calls = calls
        self._store: Store = calls.store
        self._credit = credit

    def _publish(self, campaign_id: str) -> None:
        campaign = self._store.get_campaign(campaign_id)
        if campaign:
            self._calls.bus.publish({"type": "campaign", "campaign": campaign})

    def _set_status(self, campaign: dict, status: str, detail: str = "") -> None:
        if campaign["status"] != status or campaign["status_detail"] != detail:
            self._store.update_campaign(campaign["id"], status=status, status_detail=detail)
            self._publish(campaign["id"])

    def settle(self) -> None:
        """Note how each list call that has ended went."""
        for contact in self._store.contacts_calling():
            call = self._store.get_call(contact["call_id"]) if contact["call_id"] else None
            if call is not None and call["status"] in ("connecting", "ringing", "live"):
                continue
            if call is None:
                status, detail = "failed", (
                    "The call was deleted." if contact["call_id"] else "The server stopped as it was being dialled."
                )
            else:
                status, detail = outcome(call, self._store.caller_spoke(call["id"]))
                if status == "voicemail" and any(
                    message["role"] == "note" and message["text"].startswith(LEFT_A_MESSAGE)
                    for message in self._store.get_messages(call["id"])
                ):
                    detail = "Left a message."
            self._store.update_contact(contact["id"], status=status, detail=detail)
            self._publish(contact["campaign_id"])

    async def tick(self) -> None:
        self.settle()
        for campaign in self._store.campaigns_with_status("running"):
            try:
                await self._run(campaign)
            except Exception:
                logger.exception(f"Call list {campaign['id']} could not go on")

    async def _run(self, campaign: dict) -> None:
        counts = campaign["counts"]
        if not counts.get("waiting") and not counts.get("calling"):
            self._set_status(campaign, "done")
            return
        missing = self._cfg.missing_phone_settings() or self._cfg.missing_agent_keys()
        if missing:
            self._set_status(campaign, "paused", f"Paused: add {', '.join(missing)} to the .env file.")
            return
        from_number = calls_from(self._cfg, self._store)
        if not from_number:
            self._set_status(campaign, "paused", f"Paused. {NO_NUMBER_TO_CALL_FROM}")
            return
        if not in_calling_hours(campaign):
            hours = f"{clock_of(campaign['calling_from'])} to {clock_of(campaign['calling_until'])}"
            self._set_status(campaign, "running", f"Waiting for calling hours ({hours}, {campaign['timezone']}).")
            return
        self._set_status(campaign, "running")
        free = campaign["at_once"] - counts.get("calling", 0)
        agent = self._store.get_agent()
        blocked = self._store.get_blocked_numbers()
        opted_out = {entry["number"] for entry in self._store.get_do_not_call()}
        # One line is left for people calling in (unless Oscar takes only one call at once).
        lines_for_lists = self._cfg.max_calls - 1 if self._cfg.max_calls > 1 else 1
        while free > 0:
            if lines_busy(self._store, self._cfg) or self._store.count_open_calls() >= lines_for_lists:
                return
            if self._credit is not None:
                too_little = await self._credit.too_little_for_a_call()
                if too_little:
                    self._set_status(campaign, "paused", f"Paused: {too_little}")
                    return
            waiting = self._store.campaign_contacts(campaign["id"], status="waiting", limit=1)
            if not waiting:
                return
            contact = waiting[0]
            digits = re.sub(r"\D", "", contact["number"])
            if digits in opted_out:
                self._store.update_contact(contact["id"], status="skipped", detail="Asked not to be called again.")
                self._publish(campaign["id"])
                continue
            if is_blocked(contact["number"], blocked):
                self._store.update_contact(contact["id"], status="skipped", detail="On the blocked list.")
                self._publish(campaign["id"])
                continue
            self._store.update_contact(contact["id"], status="calling", detail="")
            try:
                call = await place_outbound_call(
                    self._cfg,
                    self._calls,
                    contact["number"],
                    agent=agent_for_contact(agent, campaign, contact),
                    campaign_id=campaign["id"],
                    from_number=from_number,
                )
            except PhoneCallError as exc:
                self._store.update_contact(contact["id"], status="failed", detail=str(exc))
            else:
                self._store.update_contact(contact["id"], status="calling", call_id=call["id"])
            self._publish(campaign["id"])
            free -= 1
