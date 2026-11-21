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
# (app/agent.py). "none" asks for nothing and leaves the voice as it is.
ACCENTS: dict[str, str] = {
    "indian": "Indian",
    "australian": "Australian",
    "british": "British",
    "american": "American",
    "none": "The voice's own",
}

AccentId = Literal["indian", "australian", "british", "american", "none"]

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
)


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

    def _add_missing_columns(self) -> None:
        """Bring a database made by an earlier version of Oscar up to date."""
        for table, column, kind in (
            ("agent", "accent", "TEXT NOT NULL DEFAULT 'australian'"),
            ("agent", "knowledge", "TEXT NOT NULL DEFAULT ''"),
            ("agent", "script", "TEXT NOT NULL DEFAULT ''"),
            ("whatsapp_settings", "own_number", "INTEGER NOT NULL DEFAULT 0"),
            # What the call cost on OpenRouter, in dollars (app/costs.py).
            ("calls", "cost", "REAL NOT NULL DEFAULT 0"),
            # When the owner ticked the call's to-do off ("" while it is still to do).
            ("calls", "follow_up_done_at", "TEXT NOT NULL DEFAULT ''"),
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

    def close(self) -> None:
        with self._lock:
            self._db.close()

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
        )

    def save_agent(self, agent: AgentSettings) -> AgentSettings:
        with self._lock:
            self._db.execute(
                """
                INSERT INTO agent
                    (id, name, greeting, system_prompt, model, voice, accent, knowledge, script,
                     updated_at)
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    name = excluded.name,
                    greeting = excluded.greeting,
                    system_prompt = excluded.system_prompt,
                    model = excluded.model,
                    voice = excluded.voice,
                    accent = excluded.accent,
                    knowledge = excluded.knowledge,
                    script = excluded.script,
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
                    utc_now(),
                ),
            )
        return agent

    # Calls

    def create_call(
        self, kind: CallKind, *, status: CallStatus, from_number: str = "", to_number: str = ""
    ) -> dict:
        call_id = uuid.uuid4().hex
        with self._lock:
            self._db.execute(
                """
                INSERT INTO calls (id, kind, status, from_number, to_number, started_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (call_id, kind, status, from_number, to_number, utc_now()),
            )
        return self.get_call(call_id)

    _UPDATABLE = {"status", "twilio_sid", "error", "ended_at", "follow_up_done_at", *_SUMMARY_COLUMNS}

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
        self, *, limit: int = 50, before: str = "", query: str = "", show: str = "all"
    ) -> tuple[list[dict], bool]:
        """Calls for Call history, newest first: (the calls, whether there are older ones).

        `before` is a started_at: only calls started before it (the next page).
        `query` is looked for in the summary, the to-do, the caller's name, the
        numbers, the error and what was said; a number is found whatever spaces
        or dashes it was typed with. `show` is one of SHOW_FILTERS.
        """
        conditions, values = [], []
        if before:
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
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM calls {where} ORDER BY started_at DESC, rowid DESC LIMIT ?",
                (*values, limit + 1),
            ).fetchall()
        return [self._call_dict(row) for row in rows[:limit]], len(rows) > limit

    def count_open_todos(self) -> int:
        with self._lock:
            row = self._db.execute(
                f"SELECT COUNT(*) AS open FROM calls WHERE {SHOW_FILTERS['todo']}"
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
        return call

    # Where each call's summary is sent (app/notify.py)

    def get_summary_webhook(self) -> dict:
        """{"url", "last_result" ("sent" or what went wrong), "last_at"}; url "" when none is set."""
        with self._lock:
            row = self._db.execute("SELECT * FROM summary_webhook WHERE id = 1").fetchone()
        if row is None:
            return {"url": "", "last_result": "", "last_at": ""}
        return {"url": row["url"], "last_result": row["last_result"], "last_at": row["last_at"]}

    def save_summary_webhook_url(self, url: str) -> dict:
        with self._lock:
            self._db.execute(
                """
                INSERT INTO summary_webhook (id, url) VALUES (1, ?)
                ON CONFLICT (id) DO UPDATE SET url = excluded.url, last_result = '', last_at = ''
                """,
                (url,),
            )
        return self.get_summary_webhook()

    def record_summary_webhook_result(self, result: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE summary_webhook SET last_result = ?, last_at = ? WHERE id = 1",
                (result[:500], utc_now()),
            )

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
