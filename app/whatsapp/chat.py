"""Deciding when Oscar is being talked to, and what it sees when it answers.

Nothing here touches WhatsApp itself, so it can be tested on its own.
"""

import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

# How much of a chat Oscar remembers when it answers.
HISTORY_MESSAGES = 40
HISTORY_MAX_AGE_SECONDS = 6 * 3600

# Mentions older than this (for example, delivered in bulk after Oscar was
# offline) are remembered as context but not answered.
MAX_REPLY_DELAY_SECONDS = 10 * 60

# Per chat: at most this many replies in the window, so a runaway loop or an
# eager friend cannot run up the model bill or flood the group.
RATE_LIMIT_REPLIES = 8
RATE_LIMIT_WINDOW_SECONDS = 10 * 60

# Across all chats, since anyone can add Oscar's number to a new group.
GLOBAL_RATE_LIMIT_REPLIES = 60
GLOBAL_RATE_LIMIT_WINDOW_SECONDS = 60 * 60

MAX_REPLY_CHARACTERS = 3500

KIND_PLACEHOLDERS = {
    "image": "[photo]",
    "video": "[video]",
    "audio": "[voice message]",
    "sticker": "[sticker]",
    "document": "[document]",
    "other": "[message]",
}


@dataclass
class IncomingMessage:
    """A WhatsApp message, reduced to what Oscar needs."""

    chat_id: str
    is_group: bool
    message_id: str
    sender_ids: frozenset[str]  # every id this sender is known by (phone number and/or LID)
    sender_name: str
    timestamp: float  # unix seconds
    text: str = ""  # message text, or the caption of a photo/video/document
    kind: str = "text"  # text | image | video | audio | sticker | document | other
    mentioned_ids: frozenset[str] = frozenset()
    quoted_sender_id: str = ""
    quoted_text: str = ""
    quoted_kind: str = ""
    from_me: bool = False
    view_once: bool = False  # a view-once photo/video: never downloaded or described
    expiration: int = 0  # disappearing-messages timer of the chat, in seconds (0 = off)

    @property
    def summary(self) -> str:
        if self.view_once:
            return "[view-once media]"
        return describe(self.kind, self.text)


def describe(kind: str, text: str) -> str:
    """One line for the transcript: the text, with a tag for media."""
    if kind == "text":
        return text.strip()
    placeholder = KIND_PLACEHOLDERS.get(kind, "[message]")
    return f"{placeholder} {text.strip()}".strip()


def user_part(jid: str) -> str:
    """'123:4@s.whatsapp.net' -> '123' (drops the server and device)."""
    return jid.split("@", 1)[0].split(":", 1)[0]


def name_handle(agent_name: str) -> str:
    """What people type after @ to call Oscar: the first word of its name."""
    words = agent_name.split()
    return words[0] if words else "oscar"


def mentions_by_name(text: str, agent_name: str) -> bool:
    """True when the text contains '@Oscar' (any case), not as part of a longer word."""
    handle = re.escape(name_handle(agent_name))
    return re.search(rf"(?<![\w@])@{handle}(?![\w])", text, flags=re.IGNORECASE) is not None


def oscar_label(agent_name: str) -> str:
    """What marks a message as Oscar's when Oscar shares its owner's number."""
    return f"*{agent_name}:*"


def is_labelled_as_oscars(text: str, agent_name: str) -> bool:
    return text.lstrip().casefold().startswith(oscar_label(agent_name).casefold())


def without_label(text: str, agent_name: str) -> str:
    if not is_labelled_as_oscars(text, agent_name):
        return text
    return text.lstrip()[len(oscar_label(agent_name)) :].lstrip()


def is_addressed_to_oscar(
    message: IncomingMessage,
    *,
    my_ids: frozenset[str],
    agent_name: str,
    reply_in_private_chats: bool,
    own_number: bool = False,
    members_called_oscar: frozenset[str] = frozenset(),
) -> bool:
    """Whether Oscar should answer this message.

    With `own_number`, Oscar shares its owner's WhatsApp account. A mention of
    that account, or a reply to one of its messages, is then meant for the
    owner, so only Oscar's name calls Oscar: typed as @oscar, picked from the @
    list when a member goes by that name (`members_called_oscar`), or in a
    reply to one of Oscar's own answers. The owner can call Oscar too.
    """
    if own_number:
        if is_labelled_as_oscars(message.text, agent_name):
            return False  # one of Oscar's own answers
        if not message.is_group and not reply_in_private_chats:
            return False
        if mentions_by_name(message.text, agent_name):
            return True
        if message.mentioned_ids & members_called_oscar:
            return True
        return message.quoted_sender_id in my_ids and is_labelled_as_oscars(
            message.quoted_text, agent_name
        )
    if message.from_me:
        return False
    if not message.is_group:
        return reply_in_private_chats
    if message.mentioned_ids & my_ids:
        return True
    if message.quoted_sender_id and message.quoted_sender_id in my_ids:
        return True
    return mentions_by_name(message.text, agent_name)


def is_too_old_to_answer(message: IncomingMessage, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return now - message.timestamp > MAX_REPLY_DELAY_SECONDS


@dataclass
class _Entry:
    timestamp: float
    speaker: str
    text: str
    from_oscar: bool
    message_id: str = ""
    answer: bool = False  # one of Oscar's answers (not something typed on its phone)


@dataclass
class ChatMemory:
    """Recent messages and the names people go by, kept separately for each chat."""

    entries: dict[str, deque] = field(default_factory=dict)
    names: dict[str, dict[str, str]] = field(default_factory=dict)
    _replies: dict[str, deque] = field(default_factory=dict)
    _all_replies: deque = field(default_factory=deque)

    def names_in(self, chat_id: str) -> dict[str, str]:
        return self.names.get(chat_id, {})

    def remember(
        self, message: IncomingMessage, *, speaker: str | None = None, from_oscar: bool | None = None
    ) -> None:
        """Add a message to its chat's history. `speaker` overrides the sender's name.

        A message from Oscar's account counts as Oscar's unless `from_oscar` says otherwise.
        """
        if message.sender_name and not message.from_me:
            chat_names = self.names.setdefault(message.chat_id, {})
            for sender_id in message.sender_ids:
                chat_names[sender_id] = message.sender_name
        summary = message.summary
        if not summary:
            return
        log = self.entries.setdefault(message.chat_id, deque(maxlen=HISTORY_MESSAGES))
        speaker = speaker or message.sender_name or "Someone"
        from_oscar = message.from_me if from_oscar is None else from_oscar
        log.append(_Entry(message.timestamp, speaker, summary, from_oscar, message.message_id))

    def remember_reply(self, chat_id: str, agent_name: str, text: str, timestamp: float) -> None:
        log = self.entries.setdefault(chat_id, deque(maxlen=HISTORY_MESSAGES))
        log.append(_Entry(timestamp, agent_name, text, True, answer=True))

    def forget(self, chat_id: str, message_id: str) -> None:
        """Drop a message its sender deleted for everyone."""
        log = self.entries.get(chat_id)
        if log and message_id:
            kept = [entry for entry in log if entry.message_id != message_id]
            self.entries[chat_id] = deque(kept, maxlen=HISTORY_MESSAGES)

    def recent(self, chat_id: str, *, before_id: str = "", now: float | None = None) -> list[_Entry]:
        """Messages in arrival order. WhatsApp timestamps are whole seconds, so order
        (not time) decides what came first; only the age limit uses timestamps.

        With `before_id`, only what came before that message: what arrived while
        it waited for its answer is not part of it. Oscar's own answers sent
        since are kept, so it knows what it has already said.
        """
        now = time.time() if now is None else now
        kept, after_it = [], False
        for entry in self.entries.get(chat_id, ()):
            if before_id and entry.message_id == before_id:
                after_it = True
            elif (entry.answer or not after_it) and now - entry.timestamp <= HISTORY_MAX_AGE_SECONDS:
                kept.append(entry)
        return kept

    def allow_reply(self, chat_id: str, now: float | None = None) -> bool:
        """Count a reply against the chat's and the overall rate limit; False when either is reached."""
        now = time.time() if now is None else now
        replies = self._replies.setdefault(chat_id, deque())
        while replies and now - replies[0] > RATE_LIMIT_WINDOW_SECONDS:
            replies.popleft()
        while self._all_replies and now - self._all_replies[0] > GLOBAL_RATE_LIMIT_WINDOW_SECONDS:
            self._all_replies.popleft()
        if len(replies) >= RATE_LIMIT_REPLIES or len(self._all_replies) >= GLOBAL_RATE_LIMIT_REPLIES:
            return False
        replies.append(now)
        self._all_replies.append(now)
        return True


def readable_mentions(text: str, *, my_ids: frozenset[str], agent_name: str, names: dict[str, str]) -> str:
    """Turn '@4155550100' style mentions into '@Name' so the model knows who is meant."""

    def replace(match: re.Match) -> str:
        user = match.group(1)
        if user in my_ids:
            return f"@{agent_name}"
        if user in names:
            return f"@{names[user]}"
        return match.group(0)

    return re.sub(r"@(\d{5,20})\b", replace, text)


def _clock(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%H:%M")


def build_system_prompt(
    *,
    agent_name: str,
    chat_label: str,
    is_group: bool,
    instructions: str,
    web_search: bool,
    now: datetime,
    own_number: bool = False,
    owner_name: str = "",
    knowledge: str = "",
) -> str:
    handle = name_handle(agent_name)
    where = f'the WhatsApp group "{chat_label}"' if is_group else f"a private WhatsApp chat with {chat_label}"
    if own_number:
        owner = owner_name or "your owner"
        who = (
            f"You are {agent_name}, an AI assistant that {owner} runs from their own WhatsApp "
            f"account, here in {where}. People ask you things by writing @{handle}. Your replies "
            f"are sent from {owner}'s number, marked with your name. You are not {owner}: never "
            f"speak as them, promise anything for them, or say what they think. In the messages "
            f"below, {owner} is one of the people, like anyone else."
        )
    else:
        who = f"You are {agent_name}, a member of {where}. People ask you things by writing @{handle}."
    lines = [
        who,
        f"It is {now.strftime('%A %d %B %Y, %H:%M')} ({now.astimezone().tzname()}).",
        "",
        "How to reply:",
        "- Answer the newest message, which is addressed to you. Earlier messages are context.",
        "- Write like a person in a group chat: short and natural, usually one to four sentences. "
        "Go longer only when someone asks for detail.",
        "- Use WhatsApp formatting only: *bold*, _italic_, ~strikethrough~, ```code``` and simple "
        '"- " lists. No headings, tables or [text](link) links.',
        "- Reply in the language the person wrote in.",
        "- If something needs an action you cannot take from a chat (calls, payments, bookings, "
        "reminders), say so in a sentence and offer what you can do.",
        "- Never make things up. If you are not sure, say so.",
    ]
    if web_search:
        lines.append(
            "- You can search the web. Do it when the answer depends on current or specific facts, "
            "and say briefly where the information came from."
        )
    else:
        lines.append("- You cannot browse the web, so be clear when something may be out of date.")
    if knowledge.strip():
        # The same fact sheet Oscar answers callers from (see app/agent.py).
        lines += [
            "",
            "What you have been told about who you answer for:",
            '"""',
            knowledge.strip(),
            '"""',
            "Answer questions about them, such as hours, prices, address, services and bookings, "
            "only from these facts. If the facts do not cover such a question, say you don't have "
            "that information: do not guess, and do not search the web for it.",
        ]
    if instructions.strip():
        lines += ["", "Extra instructions from the group's owner:", instructions.strip()]
    return "\n".join(lines)


def build_request_text(
    message: IncomingMessage,
    *,
    memory: ChatMemory,
    my_ids: frozenset[str],
    agent_name: str,
    now: float | None = None,
    own_number: bool = False,
    owner_name: str = "",
    members_called_oscar: frozenset[str] = frozenset(),
) -> str:
    """The transcript the model reads: recent messages, then the one it must answer.

    With `own_number` the account's ids belong to the owner, and a mention means
    Oscar only when it names a member who goes by Oscar's name.
    """

    names = memory.names_in(message.chat_id)
    owner = owner_name or "The account's owner"
    ids_meaning_oscar = my_ids
    if own_number:
        names = {**names, **dict.fromkeys(my_ids, owner)}
        ids_meaning_oscar = members_called_oscar

    def tidy(text: str) -> str:
        return readable_mentions(text, my_ids=ids_meaning_oscar, agent_name=agent_name, names=names)

    lines = []
    earlier = memory.recent(message.chat_id, before_id=message.message_id, now=now)
    if earlier:
        lines.append("Recent messages, oldest first:")
        for entry in earlier:
            speaker = f"{entry.speaker} (you)" if entry.from_oscar else entry.speaker
            lines.append(f"[{_clock(entry.timestamp)}] {speaker}: {tidy(entry.text)}")
        lines.append("")

    asker = message.sender_name or "Someone"
    if own_number and message.from_me:
        asker = owner
    header = f"[{_clock(message.timestamp)}] {asker}"
    if message.quoted_kind:
        quoted_text = message.quoted_text
        if message.quoted_sender_id not in my_ids:
            quoted_from = names.get(message.quoted_sender_id, "someone")
        elif not own_number:
            quoted_from = "you"
        elif is_labelled_as_oscars(quoted_text, agent_name):
            quoted_from, quoted_text = "you", without_label(quoted_text, agent_name)
        else:
            quoted_from = owner
        quoted = describe(message.quoted_kind, quoted_text) or "[message]"
        header += f', replying to {quoted_from}: "{tidy(quoted)}"'
    lines.append("The message to answer:")
    lines.append(f"{header}: {tidy(message.summary) or '[message]'}")
    return "\n".join(lines)


def clean_reply(text: str, agent_name: str) -> str:
    """Make the model's answer fit a WhatsApp message."""
    text = text.strip()
    # Drop a leading "Oscar:", "Oscar (you):" or "[20:15] Oscar (you):" copied from the transcript.
    text = re.sub(
        rf"^\s*(\[\d{{1,2}}:\d{{2}}\]\s*)?\*?{re.escape(agent_name)}\*?(\s*\(you\))?\s*:\*?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    # Markdown bold/headings/links -> WhatsApp style.
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text, flags=re.DOTALL)
    text = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"\1 (\2)", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > MAX_REPLY_CHARACTERS:
        text = text[: MAX_REPLY_CHARACTERS - 1].rstrip() + "…"
    return text
