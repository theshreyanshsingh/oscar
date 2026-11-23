"""SQLite storage for the agent's settings and the call log."""

import hashlib
import json
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# The voices of the speech-to-speech model calls use (openai/gpt-audio). Each
# one was accepted by the real service through OpenRouter on 2026-11-11.
# OpenAI does not say which are men's or women's voices: the labels are how
# listening models judged real samples of each (see ACCENT_NOTE in app/agent.py).
VOICES: dict[str, str] = {
    "cedar": "Cedar (male)",
    "alloy": "Alloy (male)",
    "ash": "Ash (male)",
    "ballad": "Ballad (male)",
    "echo": "Echo (male)",
    "fable": "Fable (male)",
    "onyx": "Onyx (male)",
    "verse": "Verse (male)",
    "marin": "Marin (female)",
    "coral": "Coral (female)",
    "nova": "Nova (female)",
    "sage": "Sage (female)",
    "shimmer": "Shimmer (female)",
}

VoiceId = Literal[
    "cedar",
    "alloy",
    "ash",
    "ballad",
    "echo",
    "fable",
    "onyx",
    "verse",
    "marin",
    "coral",
    "nova",
    "sage",
    "shimmer",
]

# The accent Oscar speaks with on calls. The voices have no accents of their
# own to pick from (nearly all sound American), so the model is asked for one
# (app/agent.py). "none" asks for nothing and leaves the voice as it is. The
# label is also how the accent is named to the model ("a ... accent"). Scottish
# to German were added on 2026-11-22, after real replies were judged (see
# ACCENT_NOTE in app/agent.py); accents that did not hold were left out.
ACCENTS: dict[str, str] = {
    "indian": "Indian",
    "australian": "Australian",
    "british": "British",
    "scottish": "Scottish",
    "american": "American",
    "southern_american": "Southern American",
    "singaporean": "Singaporean",
    "nigerian": "Nigerian",
    "french": "French",
    "german": "German",
    "none": "The voice's own",
}

AccentId = Literal[
    "indian",
    "australian",
    "british",
    "scottish",
    "american",
    "southern_american",
    "singaporean",
    "nigerian",
    "french",
    "german",
    "none",
]

CallKind = Literal["browser", "phone_inbound", "phone_outbound"]
# Who a transcript line is from. A "note" is not speech: it records something
# Oscar did during the call, such as a web search.
MessageRole = Literal["user", "agent", "note"]
CallStatus = Literal["connecting", "ringing", "live", "ended", "failed"]
OPEN_STATUSES = ("connecting", "ringing", "live")
# Where a call's summary has got to (app/summary.py). "" is a call without one:
# nothing was said on it, or it ended before Oscar wrote summaries.
SummaryStatus = Literal["", "writing", "ready", "failed"]


# The longest fact sheet Oscar takes. It is sent with every turn of a call.
KNOWLEDGE_MAX_CHARACTERS = 12000
# The longest script: the steps Oscar works through on a call (app/script.py).
SCRIPT_MAX_CHARACTERS = 4000
# What else the owner can have noted from each call ("Budget", "Address"): how
# many things, and how long each one's name can be.
SUMMARY_FIELDS_MAX = 8
SUMMARY_FIELD_CHARS = 40
# The most minutes a call can be let run.
MAX_CALL_MINUTES = 120
# The longest message Oscar leaves when a voicemail answers a call it makes.
VOICEMAIL_MESSAGE_CHARS = 500


def phone_number(value: str) -> str:
    """A number Oscar can dial, as +<country code><number> (E.164). Raises ValueError otherwise."""
    digits = re.sub(r"\D", "", value)
    if not value.strip().startswith("+") or not 8 <= len(digits) <= 15 or digits[0] == "0":
        raise ValueError("Write the number with its country code, like +61412345678.")
    if re.search(r"[^\d\s()+.\-]", value):
        raise ValueError("A phone number has only digits, spaces and + ( ) . -")
    return "+" + digits


class AgentSettings(BaseModel):
    """What the agent is called, says first, knows, and sounds like."""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=60)
    greeting: str = Field(default="", max_length=500)
    system_prompt: str = Field(min_length=1, max_length=8000)
    # Facts about whoever Oscar answers for: hours, prices, address and the like.
    knowledge: str = Field(default="", max_length=KNOWLEDGE_MAX_CHARACTERS)
    # The steps of a call, one a line, in the order Oscar is to take them.
    script: str = Field(default="", max_length=SCRIPT_MAX_CHARACTERS)
    model: str = Field(min_length=1, max_length=200)
    voice: VoiceId
    accent: AccentId = "indian"
    # What else to note from each call, filled in with its summary (app/summary.py).
    summary_fields: list[str] = Field(default_factory=list)
    # The longest a call runs: Oscar then says goodbye and hangs up (app/agent.py).
    max_call_minutes: int = Field(default=30, ge=1, le=MAX_CALL_MINUTES)
    # Each call's audio is kept, both sides, to listen to in Call history (app/recording.py).
    record_calls: bool = True
    # Where a phone caller who asks for a person is put through to; "" when nobody is.
    transfer_number: str = Field(default="", max_length=40)
    # What Oscar says when a voicemail answers a call it makes; "" hangs up instead.
    voicemail_message: str = Field(default="", max_length=VOICEMAIL_MESSAGE_CHARS)

    @field_validator("transfer_number")
    @classmethod
    def transfer_number_can_be_dialled(cls, value: str) -> str:
        return phone_number(value) if value else ""

    @field_validator("summary_fields")
    @classmethod
    def summary_fields_are_short_names(cls, value: list[str]) -> list[str]:
        names: list[str] = []
        for name in value:
            name = re.sub(r"\s+", " ", name).strip()
            if not name or name.casefold() in (kept.casefold() for kept in names):
                continue  # a blank line, or the same thing twice
            if len(name) > SUMMARY_FIELD_CHARS:
                raise ValueError(f"Keep each one to {SUMMARY_FIELD_CHARS} characters or fewer.")
            names.append(name)
        if len(names) > SUMMARY_FIELDS_MAX:
            raise ValueError(f"Keep it to {SUMMARY_FIELDS_MAX} or fewer.")
        return names

    @field_validator("model")
    @classmethod
    def model_looks_like_an_openrouter_id(cls, value: str) -> str:
        if not re.fullmatch(r"[\w.\-/:]+", value):
            raise ValueError("Use an OpenRouter model ID, like anthropic/claude-haiku-5.5.")
        return value


DEFAULT_AGENT = AgentSettings(
    name="Oscar",
    greeting="Hi, this is Oscar. How can I help you today?",
    system_prompt=(
        "You are a friendly and helpful voice assistant on a live call.\n"
        "Your replies are spoken aloud, so:\n"
        "- Keep answers short: one to three sentences.\n"
        "- Use plain conversational language. No lists, markdown, emojis or symbols.\n"
        "- Write numbers, dates and times the way a person would say them.\n"
        "- If you did not catch something, ask the caller to repeat it."
    ),
    model="anthropic/claude-haiku-5.5",
    # A man's voice, as the owner asked for, with the accent they chose: first
    # Australian, Indian since 2026-11-12. This voice took the Indian accent on
    # every reply of 80 (ACCENT_NOTE in app/agent.py).
    voice="cedar",
    accent="indian",
)


class WhatsAppSettings(BaseModel):
    """How Oscar behaves in WhatsApp chats (its name and model come from AgentSettings)."""

    model_config = ConfigDict(str_strip_whitespace=True)

    instructions: str = Field(default="", max_length=4000)
    web_search: bool = True
    reply_in_private_chats: bool = False
    # Oscar shares its owner's own WhatsApp number instead of having one to itself.
    own_number: bool = False


DEFAULT_WHATSAPP_SETTINGS = WhatsAppSettings(
    instructions=(
        "Be helpful, friendly and direct, like a smart friend in the group. "
        "Give your honest opinion when asked for one."
    ),
)

WhatsAppStatus = Literal["starting", "waiting_for_scan", "connected", "logged_out", "error", "stopped"]

# The WhatsApp process writes a heartbeat this often, and counts as not running
# when the last one is older than WHATSAPP_HEARTBEAT_STALE_SECONDS.
WHATSAPP_HEARTBEAT_SECONDS = 10
WHATSAPP_HEARTBEAT_STALE_SECONDS = 35
WHATSAPP_ACTIVITY_KEPT = 200
_LONG_AGO = "1970-01-01T00:00:00+00:00"


# What Call history can show (Store.find_calls): a condition on the calls table for each.
SHOW_FILTERS = {
    "all": "",
    "todo": "follow_up != '' AND follow_up_done_at = ''",
    "incoming": "kind = 'phone_inbound'",
    "outgoing": "kind = 'phone_outbound'",
    "browser": "kind = 'browser'",
    "failed": "status = 'failed'",
}

_SUMMARY_COLUMNS = (
    "summary_status",
    "summary",
    "caller_name",
    "callback_number",
    "follow_up",
    "summary_error",
    # What the call's searches found, written with the summary (app/research.py):
    # the notes as JSON, or why they could not be written.
    "research",
    "research_error",
    # What else the owner asked to have noted (AgentSettings.summary_fields), as a
    # JSON object of what was found ("" when nothing).
    "details",
)


def _json_list(text: str) -> list[str]:
    """A list of names kept as JSON; [] for anything else."""
    try:
        value = json.loads(text or "[]")
    except ValueError:
        return []
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def without_excerpts(search: dict) -> dict:
    """A search as the dashboard lists it: its pages by title and address, without what they said."""
    pages = [{key: value for key, value in page.items() if key != "excerpt"} for page in search["sources"]]
    return {**search, "sources": pages}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _duration_seconds(started_at: str | None, ended_at: str | None) -> int | None:
    if not started_at or not ended_at:
        return None
    delta = datetime.fromisoformat(ended_at) - datetime.fromisoformat(started_at)
    return max(0, int(delta.total_seconds()))


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        # The web server and the WhatsApp process share this file; wait for each other's writes.
        self._db = sqlite3.connect(
            path, check_same_thread=False, isolation_level=None, timeout=10
        )
        # Transcripts and chat messages live here: owner-only access.
        for file in path.parent.glob(path.name + "*"):
            try:
                os.chmod(file, 0o600)
            except OSError:
                pass  # not ours to change (e.g. a shared test directory)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    name TEXT NOT NULL,
                    greeting TEXT NOT NULL,
                    system_prompt TEXT NOT NULL,
                    model TEXT NOT NULL,
                    voice TEXT NOT NULL,
                    accent TEXT NOT NULL DEFAULT 'australian',
                    knowledge TEXT NOT NULL DEFAULT '',
                    script TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS calls (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    from_number TEXT NOT NULL DEFAULT '',
                    to_number TEXT NOT NULL DEFAULT '',
                    twilio_sid TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    summary_status TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    caller_name TEXT NOT NULL DEFAULT '',
                    callback_number TEXT NOT NULL DEFAULT '',
                    follow_up TEXT NOT NULL DEFAULT '',
                    summary_error TEXT NOT NULL DEFAULT '',
                    research TEXT NOT NULL DEFAULT '',
                    research_error TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS calls_started_at ON calls (started_at);
                CREATE INDEX IF NOT EXISTS calls_twilio_sid ON calls (twilio_sid);
                CREATE INDEX IF NOT EXISTS calls_from_number ON calls (from_number);
                CREATE INDEX IF NOT EXISTS calls_to_number ON calls (to_number);
                CREATE INDEX IF NOT EXISTS calls_status ON calls (status);
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    call_id TEXT NOT NULL REFERENCES calls (id),
                    role TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS messages_call_id ON messages (call_id);
                CREATE TABLE IF NOT EXISTS searches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    call_id TEXT NOT NULL REFERENCES calls (id),
                    query TEXT NOT NULL,
                    sources TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS searches_call_id ON searches (call_id);
                CREATE TABLE IF NOT EXISTS whatsapp_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    status TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    phone TEXT NOT NULL DEFAULT '',
                    qr_svg TEXT NOT NULL DEFAULT '',
                    heartbeat_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS whatsapp_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    instructions TEXT NOT NULL,
                    web_search INTEGER NOT NULL,
                    reply_in_private_chats INTEGER NOT NULL,
                    own_number INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS summary_webhook (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    url TEXT NOT NULL DEFAULT '',
                    last_result TEXT NOT NULL DEFAULT '',
                    last_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS blocked_numbers (
                    number TEXT PRIMARY KEY,
                    added_at TEXT NOT NULL
                );
                -- People who asked not to be called again: call lists skip them (app/campaigns.py).
                CREATE TABLE IF NOT EXISTS do_not_call (
                    number TEXT PRIMARY KEY,
                    added_at TEXT NOT NULL,
                    call_id TEXT NOT NULL DEFAULT ''
                );
                -- Lists of numbers Oscar calls one after another (app/campaigns.py).
                CREATE TABLE IF NOT EXISTS campaigns (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    status_detail TEXT NOT NULL DEFAULT '',
                    instructions TEXT NOT NULL DEFAULT '',
                    greeting TEXT NOT NULL DEFAULT '',
                    voicemail_message TEXT NOT NULL DEFAULT '',
                    calling_from INTEGER NOT NULL,
                    calling_until INTEGER NOT NULL,
                    timezone TEXT NOT NULL,
                    at_once INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS campaign_contacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    campaign_id TEXT NOT NULL REFERENCES campaigns (id),
                    number TEXT NOT NULL,
                    fields TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'waiting',
                    detail TEXT NOT NULL DEFAULT '',
                    call_id TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS campaign_contacts_campaign
                    ON campaign_contacts (campaign_id, status);
                CREATE TABLE IF NOT EXISTS whatsapp_activity (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_name TEXT NOT NULL,
                    asker TEXT NOT NULL,
                    request TEXT NOT NULL,
                    reply TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                """
            )
            self._add_missing_columns()
        # Reads that can take a while (searching a long call history, a CSV of
        # it) are made on worker threads, each with a connection of its own: they
        # never wait for Oscar's own writes, nor for each other, nor hold anything
        # up (the database is in WAL mode). Searching 50,000 calls took most of a
        # second; on the event loop it held up every call's audio for that long,
        # and on one shared connection a plain page of the history waited behind
        # other people's searches (stress tests on 2026-11-22).
        self._path = path
        self._readers: list[sqlite3.Connection] = []
        self._readers_lock = threading.Lock()
        self._thread_reader = threading.local()

    def _add_missing_columns(self) -> None:
        """Bring a database made by an earlier version of Oscar up to date."""
        for table, column, kind in (
            ("agent", "accent", "TEXT NOT NULL DEFAULT 'australian'"),
            ("agent", "knowledge", "TEXT NOT NULL DEFAULT ''"),
            ("agent", "script", "TEXT NOT NULL DEFAULT ''"),
            ("agent", "summary_fields", "TEXT NOT NULL DEFAULT '[]'"),
            ("agent", "max_call_minutes", "INTEGER NOT NULL DEFAULT 30"),
            ("agent", "record_calls", "INTEGER NOT NULL DEFAULT 1"),
            ("agent", "transfer_number", "TEXT NOT NULL DEFAULT ''"),
            ("agent", "voicemail_message", "TEXT NOT NULL DEFAULT ''"),
            # Missed calls and a key running low are sent to the webhook too (app/notify.py).
            ("summary_webhook", "alerts", "INTEGER NOT NULL DEFAULT 1"),
            ("summary_webhook", "low_credit_alerted_at", "TEXT NOT NULL DEFAULT ''"),
            ("whatsapp_settings", "own_number", "INTEGER NOT NULL DEFAULT 0"),
            # What the call cost on OpenRouter, in dollars (app/costs.py).
            ("calls", "cost", "REAL NOT NULL DEFAULT 0"),
            # When the owner ticked the call's to-do off ("" while it is still to do).
            ("calls", "follow_up_done_at", "TEXT NOT NULL DEFAULT ''"),
            # Seconds of the call's audio kept in its recording; 0 when there is none (app/recording.py).
            ("calls", "recording_seconds", "REAL NOT NULL DEFAULT 0"),
            # "voicemail" or "fax" when a machine answered a call Oscar made ("" otherwise).
            ("calls", "answered_by", "TEXT NOT NULL DEFAULT ''"),
            # The number a caller was put through to, and how that went (app/phone.py).
            ("calls", "transferred_to", "TEXT NOT NULL DEFAULT ''"),
            ("calls", "transfer_result", "TEXT NOT NULL DEFAULT ''"),
            # The call list it was made for (app/campaigns.py).
            ("calls", "campaign_id", "TEXT NOT NULL DEFAULT ''"),
            *(("calls", column, "TEXT NOT NULL DEFAULT ''") for column in _SUMMARY_COLUMNS),
        ):
            columns = {row["name"] for row in self._db.execute(f"PRAGMA table_info({table})")}
            if column in columns:
                continue
            try:
                self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
            except sqlite3.OperationalError:
                # The WhatsApp process shares this file and added it a moment ago.
                columns = {row["name"] for row in self._db.execute(f"PRAGMA table_info({table})")}
                if column not in columns:
                    raise

    def _reader(self) -> sqlite3.Connection:
        """This thread's own connection for reading."""
        reader = getattr(self._thread_reader, "connection", None)
        if reader is None:
            reader = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None, timeout=10)
            reader.row_factory = sqlite3.Row
            self._thread_reader.connection = reader
            with self._readers_lock:
                self._readers.append(reader)
        return reader

    def close(self) -> None:
        with self._lock:
            self._db.close()
        with self._readers_lock:
            for reader in self._readers:
                reader.close()
            self._readers.clear()

    # Agent settings

    def get_agent(self) -> AgentSettings:
        with self._lock:
            row = self._db.execute("SELECT * FROM agent WHERE id = 1").fetchone()
        if row is None:
            return DEFAULT_AGENT
        return AgentSettings(
            name=row["name"],
            greeting=row["greeting"],
            system_prompt=row["system_prompt"],
            model=row["model"],
            # A voice saved before calls moved to another speech model no
            # longer exists; the default takes its place until one is chosen.
            voice=row["voice"] if row["voice"] in VOICES else DEFAULT_AGENT.voice,
            accent=row["accent"] if row["accent"] in ACCENTS else DEFAULT_AGENT.accent,
            knowledge=row["knowledge"],
            script=row["script"],
            summary_fields=_json_list(row["summary_fields"]),
            max_call_minutes=row["max_call_minutes"],
            record_calls=bool(row["record_calls"]),
            transfer_number=row["transfer_number"],
            voicemail_message=row["voicemail_message"],
        )

    def save_agent(self, agent: AgentSettings) -> AgentSettings:
        with self._lock:
            self._db.execute(
                """
                INSERT INTO agent
                    (id, name, greeting, system_prompt, model, voice, accent, knowledge, script,
                     summary_fields, max_call_minutes, record_calls, transfer_number,
                     voicemail_message, updated_at)
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    name = excluded.name,
                    greeting = excluded.greeting,
                    system_prompt = excluded.system_prompt,
                    model = excluded.model,
                    voice = excluded.voice,
                    accent = excluded.accent,
                    knowledge = excluded.knowledge,
                    script = excluded.script,
                    summary_fields = excluded.summary_fields,
                    max_call_minutes = excluded.max_call_minutes,
                    record_calls = excluded.record_calls,
                    transfer_number = excluded.transfer_number,
                    voicemail_message = excluded.voicemail_message,
                    updated_at = excluded.updated_at
                """,
                (
                    agent.name,
                    agent.greeting,
                    agent.system_prompt,
                    agent.model,
                    agent.voice,
                    agent.accent,
                    agent.knowledge,
                    agent.script,
                    json.dumps(agent.summary_fields),
                    agent.max_call_minutes,
                    int(agent.record_calls),
                    agent.transfer_number,
                    agent.voicemail_message,
                    utc_now(),
                ),
            )
        return agent

    # Calls

    def create_call(
        self,
        kind: CallKind,
        *,
        status: CallStatus,
        from_number: str = "",
        to_number: str = "",
        campaign_id: str = "",
    ) -> dict:
        call_id = uuid.uuid4().hex
        with self._lock:
            self._db.execute(
                """
                INSERT INTO calls (id, kind, status, from_number, to_number, campaign_id, started_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (call_id, kind, status, from_number, to_number, campaign_id, utc_now()),
            )
        return self.get_call(call_id)

    _UPDATABLE = {
        "status",
        "twilio_sid",
        "error",
        "ended_at",
        "follow_up_done_at",
        "recording_seconds",
        "answered_by",
        "transferred_to",
        "transfer_result",
        *_SUMMARY_COLUMNS,
    }

    def update_call(self, call_id: str, **fields) -> dict | None:
        unknown = set(fields) - self._UPDATABLE
        if unknown:
            raise ValueError(f"Cannot update call fields: {sorted(unknown)}")
        if fields:
            assignments = ", ".join(f"{name} = ?" for name in fields)
            with self._lock:
                self._db.execute(
                    f"UPDATE calls SET {assignments} WHERE id = ?", (*fields.values(), call_id)
                )
        return self.get_call(call_id)

    def add_call_cost(self, call_id: str, dollars: float) -> None:
        """Add what one of the call's requests cost to what the call has cost so far."""
        with self._lock:
            self._db.execute("UPDATE calls SET cost = cost + ? WHERE id = ?", (dollars, call_id))

    def calls_cost_since(self, since: datetime) -> float:
        """What the calls started since then cost, in dollars."""
        stamp = since.astimezone(timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            row = self._db.execute(
                "SELECT COALESCE(SUM(cost), 0) AS total FROM calls WHERE started_at >= ?", (stamp,)
            ).fetchone()
        return float(row["total"])

    def get_call(self, call_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
        return self._call_dict(row) if row else None

    def find_call_by_twilio_sid(self, twilio_sid: str) -> dict | None:
        if not twilio_sid:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM calls WHERE twilio_sid = ?", (twilio_sid,)
            ).fetchone()
        return self._call_dict(row) if row else None

    def list_calls(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM calls ORDER BY started_at DESC, rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._call_dict(row) for row in rows]

    def find_calls(
        self,
        *,
        limit: int = 50,
        before: str = "",
        before_id: str = "",
        query: str = "",
        show: str = "all",
    ) -> tuple[list[dict], bool]:
        """Calls for Call history, newest first: (the calls, whether there are older ones).

        The next page is the calls after the call `before_id` in that order. Calls
        are timed to the second, so several can share a started_at: the call's own
        place among them is kept too. `before` (a started_at alone) is what older
        dashboards send, and skips the rest of that second.
        `query` is looked for in the summary, the to-do, the caller's name, the
        numbers, the error and what was said; a number is found whatever spaces
        or dashes it was typed with. `show` is one of SHOW_FILTERS.
        """
        conditions, values = [], []
        after = (
            self._reader().execute("SELECT started_at, rowid FROM calls WHERE id = ?", (before_id,)).fetchone()
            if before_id
            else None
        )
        if after is not None:
            conditions.append("(started_at < ? OR (started_at = ? AND rowid < ?))")
            values.extend([after["started_at"], after["started_at"], after["rowid"]])
        elif before:
            conditions.append("started_at < ?")
            values.append(before)
        if SHOW_FILTERS.get(show):
            conditions.append(SHOW_FILTERS[show])
        query = query.strip()
        if query:
            like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            searched = [
                f"{column} LIKE ? ESCAPE '\\'"
                for column in ("summary", "follow_up", "caller_name", "error", "from_number", "to_number", "callback_number")
            ]
            searched.append(
                "EXISTS (SELECT 1 FROM messages WHERE messages.call_id = calls.id AND text LIKE ? ESCAPE '\\')"
            )
            values.extend([like] * len(searched))
            digits = re.sub(r"[\s()\-.]", "", query)
            if len(digits) >= 3 and re.fullmatch(r"\+?\d+", digits):
                for column in ("from_number", "to_number", "callback_number"):
                    searched.append(f"{column} LIKE ?")
                    values.append(f"%{digits.lstrip('+')}%")
            conditions.append("(" + " OR ".join(searched) + ")")
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self._reader().execute(
            f"SELECT * FROM calls {where} ORDER BY started_at DESC, rowid DESC LIMIT ?",
            (*values, limit + 1),
        ).fetchall()
        return [self._call_dict(row) for row in rows[:limit]], len(rows) > limit

    def delete_call(self, call_id: str) -> bool:
        """Forget a call for good: what was said, what its searches found, and its summary."""
        with self._lock:
            # All of it or none of it: never a call without its words, or words without a call.
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute("DELETE FROM messages WHERE call_id = ?", (call_id,))
                self._db.execute("DELETE FROM searches WHERE call_id = ?", (call_id,))
                # A call list that made it keeps the number, without the call.
                self._db.execute("UPDATE campaign_contacts SET call_id = '' WHERE call_id = ?", (call_id,))
                cursor = self._db.execute("DELETE FROM calls WHERE id = ?", (call_id,))
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")
        return cursor.rowcount > 0

    def call_costs_since(self, since: datetime) -> list[tuple[str, float]]:
        """(started_at, cost) of each call started since then that cost something, oldest first."""
        stamp = since.astimezone(timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            rows = self._db.execute(
                "SELECT started_at, cost FROM calls WHERE started_at >= ? AND cost > 0 ORDER BY started_at",
                (stamp,),
            ).fetchall()
        return [(row["started_at"], float(row["cost"])) for row in rows]

    def earlier_calls(self, number: str, *, before_id: str, limit: int = 3) -> list[dict]:
        """The latest phone calls with this number (to it or from it) that have a summary,
        newest first, not counting the call `before_id`."""
        if not number:
            return []
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM calls WHERE (from_number = ? OR to_number = ?) AND id != ? "
                "AND kind IN ('phone_inbound', 'phone_outbound') AND summary_status = 'ready' "
                "AND summary != '' ORDER BY started_at DESC, rowid DESC LIMIT ?",
                (number, number, before_id, limit),
            ).fetchall()
        return [self._call_dict(row) for row in rows]

    def count_open_todos(self) -> int:
        row = self._reader().execute(
            f"SELECT COUNT(*) AS open FROM calls WHERE {SHOW_FILTERS['todo']}"
        ).fetchone()
        return int(row["open"])

    def count_open_calls(self) -> int:
        """Calls connecting, ringing or going on now."""
        placeholders = ", ".join("?" for _ in OPEN_STATUSES)
        with self._lock:
            row = self._db.execute(
                f"SELECT COUNT(*) AS open FROM calls WHERE status IN ({placeholders})", OPEN_STATUSES
            ).fetchone()
        return int(row["open"])

    def fail_open_calls(self, reason: str) -> int:
        """Close calls left open by a previous run of the server."""
        placeholders = ", ".join("?" for _ in OPEN_STATUSES)
        with self._lock:
            cursor = self._db.execute(
                f"UPDATE calls SET status = 'failed', error = ?, ended_at = ? "
                f"WHERE status IN ({placeholders})",
                (reason, utc_now(), *OPEN_STATUSES),
            )
        return cursor.rowcount

    def forget_unfinished_summaries(self) -> int:
        """Summaries a previous run of the server was still writing can be asked for again."""
        with self._lock:
            cursor = self._db.execute(
                "UPDATE calls SET summary_status = '' WHERE summary_status = 'writing'"
            )
        return cursor.rowcount

    # Transcript

    def add_message(self, call_id: str, role: MessageRole, text: str) -> dict:
        created_at = utc_now()
        with self._lock:
            cursor = self._db.execute(
                "INSERT INTO messages (call_id, role, text, created_at) VALUES (?, ?, ?, ?)",
                (call_id, role, text, created_at),
            )
        return {
            "id": cursor.lastrowid,
            "call_id": call_id,
            "role": role,
            "text": text,
            "created_at": created_at,
        }

    def caller_spoke(self, call_id: str) -> bool:
        """Whether anything the other person said on the call was written down."""
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM messages WHERE call_id = ? AND role = 'user' LIMIT 1", (call_id,)
            ).fetchone()
        return row is not None

    def get_messages(self, call_id: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM messages WHERE call_id = ? ORDER BY id", (call_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    # Searches

    def add_search(self, call_id: str, query: str, sources: list[dict]) -> dict:
        """Keep what a web search on a call found: its pages, each with a title, an
        address and the excerpt Oscar read."""
        created_at = utc_now()
        with self._lock:
            cursor = self._db.execute(
                "INSERT INTO searches (call_id, query, sources, created_at) VALUES (?, ?, ?, ?)",
                (call_id, query, json.dumps(sources), created_at),
            )
        return {
            "id": cursor.lastrowid,
            "call_id": call_id,
            "query": query,
            "sources": sources,
            "created_at": created_at,
        }

    def get_searches(self, call_id: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM searches WHERE call_id = ? ORDER BY id", (call_id,)
            ).fetchall()
        return [{**dict(row), "sources": json.loads(row["sources"])} for row in rows]

    @staticmethod
    def _call_dict(row: sqlite3.Row) -> dict:
        call = dict(row)
        call["duration_seconds"] = _duration_seconds(call["started_at"], call["ended_at"])
        call["research"] = json.loads(call["research"]) if call["research"] else []
        call["details"] = json.loads(call["details"]) if call.get("details") else {}
        return call

    # Where each call's summary is sent (app/notify.py)

    def get_summary_webhook(self) -> dict:
        """{"url", "alerts", "last_result" ("sent" or what went wrong), "last_at"}; url "" when none is set.

        `alerts`: missed calls and a key running low are sent too.
        """
        with self._lock:
            row = self._db.execute("SELECT * FROM summary_webhook WHERE id = 1").fetchone()
        if row is None:
            return {"url": "", "alerts": True, "last_result": "", "last_at": ""}
        return {
            "url": row["url"],
            "alerts": bool(row["alerts"]),
            "last_result": row["last_result"],
            "last_at": row["last_at"],
        }

    def save_summary_webhook_url(self, url: str, alerts: bool = True) -> dict:
        with self._lock:
            self._db.execute(
                """
                INSERT INTO summary_webhook (id, url, alerts) VALUES (1, ?, ?)
                ON CONFLICT (id) DO UPDATE SET url = excluded.url, alerts = excluded.alerts,
                    last_result = '', last_at = ''
                """,
                (url, int(alerts)),
            )
        return self.get_summary_webhook()

    def low_credit_alerted_at(self) -> str:
        with self._lock:
            row = self._db.execute("SELECT low_credit_alerted_at FROM summary_webhook WHERE id = 1").fetchone()
        return row["low_credit_alerted_at"] if row else ""

    def record_low_credit_alert(self) -> None:
        with self._lock:
            self._db.execute("UPDATE summary_webhook SET low_credit_alerted_at = ? WHERE id = 1", (utc_now(),))

    def record_summary_webhook_result(self, result: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE summary_webhook SET last_result = ?, last_at = ? WHERE id = 1",
                (result[:500], utc_now()),
            )

    # Numbers whose calls are refused (app/phone.py)

    def get_blocked_numbers(self) -> list[str]:
        """As the owner wrote them, in the order the list was saved in."""
        with self._lock:
            rows = self._db.execute("SELECT number FROM blocked_numbers ORDER BY rowid").fetchall()
        return [row["number"] for row in rows]

    def save_blocked_numbers(self, numbers: list[str]) -> list[str]:
        """Replace the list."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute("DELETE FROM blocked_numbers")
                for number in numbers:
                    self._db.execute(
                        "INSERT OR IGNORE INTO blocked_numbers (number, added_at) VALUES (?, ?)",
                        (number, utc_now()),
                    )
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")
        return self.get_blocked_numbers()

    # People who asked not to be called again (app/campaigns.py)

    def get_do_not_call(self) -> list[dict]:
        """[{"number", "added_at", "call_id"}], newest first. `number` is digits only."""
        with self._lock:
            rows = self._db.execute("SELECT * FROM do_not_call ORDER BY added_at DESC, rowid DESC").fetchall()
        return [dict(row) for row in rows]

    def add_do_not_call(self, number: str, call_id: str = "") -> bool:
        """Put a number (any format) on the list. Returns whether it was not on it already."""
        digits = re.sub(r"\D", "", number)
        if not digits:
            return False
        with self._lock:
            cursor = self._db.execute(
                "INSERT OR IGNORE INTO do_not_call (number, added_at, call_id) VALUES (?, ?, ?)",
                (digits, utc_now(), call_id),
            )
        return cursor.rowcount > 0

    def remove_do_not_call(self, number: str) -> bool:
        digits = re.sub(r"\D", "", number)
        with self._lock:
            cursor = self._db.execute("DELETE FROM do_not_call WHERE number = ?", (digits,))
        return cursor.rowcount > 0

    # Call lists (app/campaigns.py)

    _CAMPAIGN_UPDATABLE = {"name", "status", "status_detail", "instructions", "greeting",
                           "voicemail_message", "calling_from", "calling_until", "timezone", "at_once"}

    def create_campaign(self, fields: dict, contacts: list[dict]) -> dict:
        """A new call list with its numbers ({"number", "fields"}), in one go."""
        campaign_id = uuid.uuid4().hex
        now = utc_now()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    """
                    INSERT INTO campaigns (id, name, status, status_detail, instructions, greeting,
                        voicemail_message, calling_from, calling_until, timezone, at_once,
                        created_at, updated_at)
                    VALUES (?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        campaign_id, fields["name"], fields["status"], fields["instructions"],
                        fields["greeting"], fields["voicemail_message"], fields["calling_from"],
                        fields["calling_until"], fields["timezone"], fields["at_once"], now, now,
                    ),
                )
                self._db.executemany(
                    "INSERT INTO campaign_contacts (campaign_id, number, fields, updated_at) VALUES (?, ?, ?, ?)",
                    [
                        (campaign_id, contact["number"], json.dumps(contact["fields"], ensure_ascii=False), now)
                        for contact in contacts
                    ],
                )
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")
        return self.get_campaign(campaign_id)

    def get_campaign(self, campaign_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
            counts = self._db.execute(
                "SELECT status, COUNT(*) AS n FROM campaign_contacts WHERE campaign_id = ? GROUP BY status",
                (campaign_id,),
            ).fetchall() if row else []
        if row is None:
            return None
        campaign = dict(row)
        campaign["counts"] = {count["status"]: count["n"] for count in counts}
        campaign["total"] = sum(campaign["counts"].values())
        return campaign

    def list_campaigns(self) -> list[dict]:
        """Every call list, newest first, each with how many of its numbers are at each stage."""
        with self._lock:
            ids = [row["id"] for row in self._db.execute(
                "SELECT id FROM campaigns ORDER BY created_at DESC, rowid DESC"
            ).fetchall()]
        return [campaign for campaign in map(self.get_campaign, ids) if campaign]

    def campaigns_with_status(self, status: str) -> list[dict]:
        with self._lock:
            ids = [row["id"] for row in self._db.execute(
                "SELECT id FROM campaigns WHERE status = ? ORDER BY created_at, rowid", (status,)
            ).fetchall()]
        return [campaign for campaign in map(self.get_campaign, ids) if campaign]

    def update_campaign(self, campaign_id: str, **fields) -> dict | None:
        unknown = set(fields) - self._CAMPAIGN_UPDATABLE
        if unknown:
            raise ValueError(f"Cannot update call list fields: {sorted(unknown)}")
        if fields:
            assignments = ", ".join(f"{name} = ?" for name in fields)
            with self._lock:
                self._db.execute(
                    f"UPDATE campaigns SET {assignments}, updated_at = ? WHERE id = ?",
                    (*fields.values(), utc_now(), campaign_id),
                )
        return self.get_campaign(campaign_id)

    def delete_campaign(self, campaign_id: str) -> bool:
        """Forget a call list and its numbers. The calls it made stay in Call history."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute("DELETE FROM campaign_contacts WHERE campaign_id = ?", (campaign_id,))
                cursor = self._db.execute("DELETE FROM campaigns WHERE id = ?", (campaign_id,))
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")
        return cursor.rowcount > 0

    @staticmethod
    def _contact_dict(row: sqlite3.Row) -> dict:
        contact = dict(row)
        try:
            fields = json.loads(contact["fields"] or "{}")
        except ValueError:
            fields = {}
        contact["fields"] = fields if isinstance(fields, dict) else {}
        return contact

    def campaign_contacts(self, campaign_id: str, *, status: str = "", limit: int = 0) -> list[dict]:
        """The list's numbers in the order they were given (only those at `status`, when given)."""
        query = "SELECT * FROM campaign_contacts WHERE campaign_id = ?"
        params: list = [campaign_id]
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY id"
        if limit:
            query += " LIMIT ?"
            params.append(limit)
        with self._lock:
            rows = self._db.execute(query, params).fetchall()
        return [self._contact_dict(row) for row in rows]

    def update_contact(self, contact_id: int, **fields) -> None:
        unknown = set(fields) - {"status", "detail", "call_id"}
        if unknown:
            raise ValueError(f"Cannot update call list number fields: {sorted(unknown)}")
        assignments = ", ".join(f"{name} = ?" for name in fields)
        with self._lock:
            self._db.execute(
                f"UPDATE campaign_contacts SET {assignments}, updated_at = ? WHERE id = ?",
                (*fields.values(), utc_now(), contact_id),
            )

    def call_again(self, campaign_id: str, statuses: tuple[str, ...]) -> int:
        """Put the list's numbers at these stages back in line. Returns how many."""
        placeholders = ", ".join("?" for _ in statuses)
        with self._lock:
            cursor = self._db.execute(
                f"UPDATE campaign_contacts SET status = 'waiting', detail = '', updated_at = ? "
                f"WHERE campaign_id = ? AND status IN ({placeholders})",
                (utc_now(), campaign_id, *statuses),
            )
        return cursor.rowcount

    def contacts_calling(self) -> list[dict]:
        """Numbers of any call list whose call is under way (or was, when the server stopped)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM campaign_contacts WHERE status = 'calling' ORDER BY id"
            ).fetchall()
        return [self._contact_dict(row) for row in rows]

    # WhatsApp (written by the WhatsApp process, read by the dashboard)

    def get_whatsapp_settings(self) -> WhatsAppSettings:
        with self._lock:
            row = self._db.execute("SELECT * FROM whatsapp_settings WHERE id = 1").fetchone()
        if row is None:
            return DEFAULT_WHATSAPP_SETTINGS
        return WhatsAppSettings(
            instructions=row["instructions"],
            web_search=bool(row["web_search"]),
            reply_in_private_chats=bool(row["reply_in_private_chats"]),
            own_number=bool(row["own_number"]),
        )

    def save_whatsapp_settings(self, settings: WhatsAppSettings) -> WhatsAppSettings:
        with self._lock:
            self._db.execute(
                """
                INSERT INTO whatsapp_settings
                    (id, instructions, web_search, reply_in_private_chats, own_number)
                VALUES (1, ?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    instructions = excluded.instructions,
                    web_search = excluded.web_search,
                    reply_in_private_chats = excluded.reply_in_private_chats,
                    own_number = excluded.own_number
                """,
                (
                    settings.instructions,
                    int(settings.web_search),
                    int(settings.reply_in_private_chats),
                    int(settings.own_number),
                ),
            )
        return settings

    def set_whatsapp_state(
        self,
        status: WhatsAppStatus,
        *,
        detail: str = "",
        phone: str | None = None,
        qr_svg: str = "",
    ) -> None:
        """Record the WhatsApp connection state. phone=None keeps the stored number."""
        with self._lock:
            self._db.execute(
                """
                INSERT INTO whatsapp_state (id, status, detail, phone, qr_svg, heartbeat_at)
                VALUES (1, ?, ?, COALESCE(?, ''), ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    status = excluded.status,
                    detail = excluded.detail,
                    phone = COALESCE(?, whatsapp_state.phone),
                    qr_svg = excluded.qr_svg,
                    heartbeat_at = excluded.heartbeat_at
                """,
                (status, detail, phone, qr_svg, utc_now(), phone),
            )

    def touch_whatsapp_heartbeat(self) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE whatsapp_state SET heartbeat_at = ? WHERE id = 1", (utc_now(),)
            )

    def mark_whatsapp_exited(self) -> None:
        """The WhatsApp process is exiting: stop counting it as running, keep its last status."""
        with self._lock:
            self._db.execute(
                "UPDATE whatsapp_state SET heartbeat_at = ? WHERE id = 1", (_LONG_AGO,)
            )

    def get_whatsapp_state(self) -> dict:
        with self._lock:
            row = self._db.execute("SELECT * FROM whatsapp_state WHERE id = 1").fetchone()
        if row is None:
            return {
                "running": False,
                "status": "stopped",
                "last_status": "stopped",
                "detail": "",
                "phone": "",
                "has_qr": False,
                "qr_version": "",
            }
        state = dict(row)
        age = datetime.now(timezone.utc) - datetime.fromisoformat(state["heartbeat_at"])
        running = (
            state["status"] != "stopped"
            and age.total_seconds() < WHATSAPP_HEARTBEAT_STALE_SECONDS
        )
        has_qr = running and state["status"] == "waiting_for_scan" and bool(state["qr_svg"])
        return {
            "running": running,
            "status": state["status"] if running else "stopped",
            # What the process last reported, kept after it exits (e.g. "logged_out").
            "last_status": state["status"],
            "detail": state["detail"],
            "phone": state["phone"],
            "has_qr": has_qr,
            # Changes whenever WhatsApp issues a new code, so the dashboard knows to reload it.
            "qr_version": hashlib.sha1(state["qr_svg"].encode()).hexdigest()[:12] if has_qr else "",
        }

    def get_whatsapp_qr_svg(self) -> str:
        if not self.get_whatsapp_state()["has_qr"]:
            return ""
        with self._lock:
            row = self._db.execute("SELECT qr_svg FROM whatsapp_state WHERE id = 1").fetchone()
        return row["qr_svg"] if row else ""

    def add_whatsapp_activity(
        self, *, chat_name: str, asker: str, request: str, reply: str, error: str = ""
    ) -> None:
        with self._lock:
            self._db.execute(
                """
                INSERT INTO whatsapp_activity (chat_name, asker, request, reply, error, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (chat_name, asker, request, reply, error, utc_now()),
            )
            self._db.execute(
                "DELETE FROM whatsapp_activity WHERE id NOT IN "
                "(SELECT id FROM whatsapp_activity ORDER BY id DESC LIMIT ?)",
                (WHATSAPP_ACTIVITY_KEPT,),
            )

    def list_whatsapp_activity(self, limit: int = 30) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM whatsapp_activity ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]
