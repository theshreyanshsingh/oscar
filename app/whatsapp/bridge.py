"""The WhatsApp side: links Oscar's number, watches its chats, and answers @oscar.

Uses neonize (Python bindings for whatsmeow, the library behind the long-running
WhatsApp bridges). This is an unofficial WhatsApp Web connection: it breaks
WhatsApp's terms, and the number Oscar uses can be banned.
"""

import asyncio
import functools
import io
import logging
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import segno
from neonize.aioze.client import NewAClient
from neonize.events import (
    ClientOutdatedEv,
    ConnectedEv,
    ConnectFailureEv,
    DisconnectedEv,
    LoggedOutEv,
    MessageEv,
    PairStatusEv,
    StreamReplacedEv,
    TemporaryBanEv,
)
from neonize.proto.waCompanionReg.WAWebProtobufsCompanionReg_pb2 import DeviceProps
from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import (
    ContextInfo,
    ExtendedTextMessage,
    Message,
    ProtocolMessage,
)
from neonize.utils.enum import ChatPresence, ChatPresenceMedia, Presence

from app.config import Config
from app.store import WHATSAPP_HEARTBEAT_SECONDS, Store
from app.whatsapp import brain
from app.whatsapp.chat import (
    ChatMemory,
    IncomingMessage,
    build_request_text,
    build_system_prompt,
    clean_reply,
    is_addressed_to_oscar,
    is_labelled_as_oscars,
    is_too_old_to_answer,
    name_handle,
    oscar_label,
)

logger = logging.getLogger("oscar.whatsapp")

MAX_IMAGE_BYTES = 5 * 1024 * 1024
# WhatsApp shows the first linking code for 60 seconds and each later one for
# 20. After a few minutes it stops issuing them, without saying so.
FIRST_CODE_SECONDS = 60
LATER_CODE_SECONDS = 20
# A code still showing this long after its time was the last of its round.
CODE_GRACE_SECONDS = 8
# How long Oscar keeps asking for new rounds of codes before it gives up.
LINK_WINDOW_SECONDS = 60 * 60
# How long a connection gets to close after it is told to.
CLOSE_PATIENCE_SECONDS = 10
# Right after linking, WhatsApp will not show Oscar as online until it has sent
# the account's name, which takes a few seconds. Oscar asks again this often,
# this many times.
ONLINE_RETRY_SECONDS = 5
ONLINE_ATTEMPTS = 12
# How long Oscar trusts what it learned about who is in a group.
GROUP_MEMBERS_SECONDS = 10 * 60
# How many recent messages Oscar knows by id, to notice one WhatsApp delivers twice.
SEEN_MESSAGES = 500
# How long unlinking waits for WhatsApp to answer.
UNLINK_PATIENCE_SECONDS = 30
# When WhatsApp signs Oscar out, its library deletes the saved login, which can
# take a few seconds. Stopping the library, or deleting its files, before it has
# finished cuts that short (and has crashed it). So Oscar waits this long.
LOGIN_CLEARING_PATIENCE_SECONDS = 30
UNLINK_ON_THE_PHONE = (
    "Unlink it on the phone instead: WhatsApp → Settings → Linked devices → Oscar → Log out."
)
LINK_INSTRUCTIONS = (
    "On the phone that has Oscar's WhatsApp number (not your own phone), open WhatsApp → "
    "Settings → Linked devices → Link a device, and scan this code."
)
SORRY_REPLY = "Sorry, I can't answer right now. Please try again in a bit."

_MEDIA_FIELDS = (
    ("imageMessage", "image"),
    ("videoMessage", "video"),
    ("audioMessage", "audio"),
    ("stickerMessage", "sticker"),
    ("documentMessage", "document"),
)


def _jid_user(jid) -> str:
    return jid.User if jid is not None and jid.User else ""


def _jid_string(jid) -> str:
    return f"{jid.User}@{jid.Server}"


def message_content(message: Message) -> tuple[str, str, ContextInfo | None]:
    """(kind, text, context info) for a WhatsApp message. kind is '' for ones to ignore."""
    if message.conversation:
        return "text", message.conversation, None
    if message.HasField("extendedTextMessage"):
        body = message.extendedTextMessage
        return "text", body.text, body.contextInfo if body.HasField("contextInfo") else None
    for field_name, kind in _MEDIA_FIELDS:
        if message.HasField(field_name):
            body = getattr(message, field_name)
            caption = getattr(body, "caption", "") or ""
            context = body.contextInfo if body.HasField("contextInfo") else None
            return kind, caption, context
    # Reactions, deletions, polls, and other protocol messages.
    return "", "", None


def parse_message(event: MessageEv) -> IncomingMessage | None:
    """Reduce a neonize message event to an IncomingMessage, or None to ignore it."""
    if event.IsEdit:
        return None
    source = event.Info.MessageSource
    if source.Chat.Server in ("broadcast", "newsletter"):  # status updates and channels
        return None
    kind, text, context = message_content(event.Message)
    if not kind:
        return None

    sender_ids = {_jid_user(source.Sender)}
    if source.HasField("SenderAlt"):
        sender_ids.add(_jid_user(source.SenderAlt))

    mentioned: set[str] = set()
    quoted_sender = quoted_text = quoted_kind = ""
    expiration = 0
    if context is not None:
        expiration = context.expiration
        mentioned = {jid.split("@", 1)[0].split(":", 1)[0] for jid in context.mentionedJID}
        if context.participant:
            quoted_sender = context.participant.split("@", 1)[0].split(":", 1)[0]
        if context.HasField("quotedMessage"):
            quoted_kind, quoted_text, _ = message_content(context.quotedMessage)
            quoted_kind = quoted_kind or "other"

    timestamp = float(event.Info.Timestamp)
    if timestamp > 1e11:  # milliseconds
        timestamp /= 1000

    return IncomingMessage(
        chat_id=_jid_string(source.Chat),
        is_group=source.IsGroup,
        message_id=event.Info.ID,
        sender_ids=frozenset(filter(None, sender_ids)),
        sender_name=event.Info.Pushname,
        timestamp=timestamp,
        text=text,
        kind=kind,
        mentioned_ids=frozenset(mentioned),
        quoted_sender_id=quoted_sender,
        quoted_text=quoted_text,
        quoted_kind=quoted_kind,
        from_me=source.IsFromMe,
        view_once=event.IsViewOnce or event.IsViewOnceV2 or event.IsViewOnceV2Extension,
        expiration=expiration,
    )


def _image_sources(event: MessageEv) -> list[Message]:
    """The message's own photo and/or the photo it replies to."""
    sources = []
    if event.Message.HasField("imageMessage"):
        sources.append(event.Message)
    _, _, context = message_content(event.Message)
    if context is not None and context.HasField("quotedMessage"):
        if context.quotedMessage.HasField("imageMessage"):
            sources.append(context.quotedMessage)
    return sources


def _safe(handler):
    """neonize runs handlers in the background and drops their errors; log them instead."""

    @functools.wraps(handler)
    async def wrapper(*args):
        try:
            await handler(*args)
        except Exception:
            logger.exception("Error in WhatsApp event handler %s", handler.__name__)

    return wrapper


class WhatsAppBridge:
    def __init__(self, cfg: Config, store: Store, client: NewAClient | None = None):
        self.cfg = cfg
        self.store = store
        self.memory = ChatMemory()
        self.my_ids: frozenset[str] = frozenset()
        self.session_path = cfg.db_path.parent / "whatsapp-session.sqlite3"
        self.failed = False  # set when the connection ended with an error
        self._chat_locks: dict[str, asyncio.Lock] = {}
        self._seen: dict[tuple[str, str], None] = {}  # (chat, message id), oldest first
        self._group_names: dict[str, str] = {}
        self._group_members: dict[str, tuple[float, dict]] = {}  # chat -> (when learned, id -> phone id)
        self._owner_name = ""  # the linked account's WhatsApp name
        self._last_status = ""
        self._phone = ""
        self._qr_expiry: asyncio.TimerHandle | None = None
        self._codes_this_round = 0
        self._link_deadline: float | None = None  # set when the first code arrives
        self._renew = asyncio.Event()  # the round of codes ran out: connect again for more
        self._going_online: asyncio.Future | None = None
        self._login_cleared = False  # after a logout: the library finished deleting the login
        if client is None:
            self.session_path.parent.mkdir(parents=True, exist_ok=True)
            client = NewAClient(
                str(self.session_path),
                props=DeviceProps(os="Oscar", platformType=DeviceProps.DESKTOP),
            )
        self.client = client
        self._register_handlers()

    # Connection state

    def _set_state(self, status: str, detail: str = "", **fields) -> None:
        self._last_status = status
        self.store.set_whatsapp_state(status, detail=detail, **fields)

    def _register_handlers(self) -> None:
        self.client.event.qr(_safe(self.on_qr))
        self.client.event(ConnectedEv)(_safe(self.on_connected))
        self.client.event(PairStatusEv)(_safe(self.on_pair_status))
        self.client.event(LoggedOutEv)(_safe(self.on_logged_out))
        self.client.event(TemporaryBanEv)(_safe(self.on_temporary_ban))
        self.client.event(StreamReplacedEv)(_safe(self.on_stream_replaced))
        self.client.event(ConnectFailureEv)(_safe(self.on_connect_failure))
        self.client.event(ClientOutdatedEv)(_safe(self.on_client_outdated))
        self.client.event(DisconnectedEv)(_safe(self.on_disconnected))
        self.client.event(MessageEv)(_safe(self.on_message))

    async def on_qr(self, _client, data: bytes) -> None:
        qr = segno.make_qr(data)
        # A standalone SVG document (with its namespace), served to the dashboard as an image.
        buffer = io.BytesIO()
        qr.save(buffer, kind="svg", scale=5, border=2, dark="#000000", light="#ffffff", xmldecl=False)
        self._set_state("waiting_for_scan", LINK_INSTRUCTIONS, qr_svg=buffer.getvalue().decode())
        self._protect_session_file()
        print("\n" + LINK_INSTRUCTIONS + " (The same code is shown on the dashboard.)")
        qr.terminal(compact=True)

        # When no new code replaces this one in time, it was the last of its round.
        self._codes_this_round += 1
        if self._link_deadline is None:
            self._link_deadline = time.monotonic() + LINK_WINDOW_SECONDS
        lifetime = FIRST_CODE_SECONDS if self._codes_this_round == 1 else LATER_CODE_SECONDS
        self._cancel_code_expiry()
        self._qr_expiry = asyncio.get_running_loop().call_later(
            lifetime + CODE_GRACE_SECONDS,
            lambda: asyncio.ensure_future(_safe(self._codes_ran_out)()),
        )

    def _cancel_code_expiry(self) -> None:
        if self._qr_expiry:
            self._qr_expiry.cancel()
            self._qr_expiry = None

    async def _fail(self, detail: str) -> None:
        """Record a problem WhatsApp will not recover from by itself, and stop.

        After these, the library stays disconnected. Stopping lets the process
        exit with the reason, instead of sitting there looking alive.
        """
        self.failed = True
        self._set_state("error", detail)
        logger.error("Stopping the WhatsApp connection: %s", detail)
        await self.client.stop()

    async def _codes_ran_out(self) -> None:
        """The last code of a round went unscanned: get a new round, or give up after a while."""
        if self._last_status != "waiting_for_scan":
            return
        if time.monotonic() >= self._link_deadline:
            await self._fail(
                f"The linking code expired: nobody scanned one in {LINK_WINDOW_SECONDS // 60} "
                "minutes. Restart the WhatsApp connection to get a new code."
            )
            return
        # run() closes this connection and opens another, which starts a new round.
        self._set_state("starting", "Getting a new linking code…")
        logger.info("The linking codes ran out; asking WhatsApp for new ones")
        self._renew.set()

    async def on_connected(self, _client, _event: ConnectedEv) -> None:
        self._cancel_code_expiry()
        await self._load_my_ids()
        self._set_state("connected", "Oscar is online in WhatsApp.", phone=self._phone or None)
        self._protect_session_file()
        logger.info("Connected to WhatsApp as %s", self._phone or "(unknown number)")
        if not await self._go_online():
            self._stop_going_online()
            self._going_online = asyncio.ensure_future(_safe(self._keep_trying_to_go_online)())

    async def _go_online(self) -> bool:
        """Tell WhatsApp Oscar is online, so its name shows up properly to others."""
        try:
            await self.client.send_presence(Presence.AVAILABLE)
        except Exception as exc:
            logger.debug("Could not set Oscar as online yet: %s", exc)
            return False
        return True

    async def _keep_trying_to_go_online(self) -> None:
        for _ in range(ONLINE_ATTEMPTS - 1):
            await asyncio.sleep(ONLINE_RETRY_SECONDS)
            if self._last_status != "connected":
                return
            if await self._go_online():
                return
        logger.warning("Could not set Oscar as online. Its replies still work.")

    def _stop_going_online(self) -> None:
        if self._going_online:
            self._going_online.cancel()
            self._going_online = None

    async def _load_my_ids(self) -> None:
        """Learn Oscar's own phone number and LID, which mentions and replies refer to."""
        try:
            me = await self.client.get_me()
        except Exception as exc:
            logger.error("Could not read Oscar's own WhatsApp ids: %s", exc)
            self._phone = ""
            return
        self.my_ids = frozenset(filter(None, {_jid_user(me.JID), _jid_user(me.LID)}))
        self._owner_name = me.PushName.strip()
        phone = _jid_user(me.JID)
        self._phone = f"+{phone}" if phone else ""

    def _protect_session_file(self) -> None:
        """The session file holds Oscar's WhatsApp login keys: owner-only access."""
        for path in self.session_path.parent.glob(self.session_path.name + "*"):
            try:
                os.chmod(path, 0o600)
            except FileNotFoundError:
                pass  # SQLite's journal, which lasts one write: gone again since it was listed

    async def on_pair_status(self, _client, event: PairStatusEv) -> None:
        if event.Error:
            self._set_state("error", f"Linking failed: {event.Error}. Restart the WhatsApp connection to get a new code.")
        elif event.Status == PairStatusEv.SUCCESS:
            # The code was scanned. WhatsApp now reconnects as Oscar's number, which
            # must not be mistaken for a code that nobody scanned.
            self._cancel_code_expiry()
            self._set_state("starting", "Linked. Connecting to WhatsApp…")

    async def on_logged_out(self, _client, event: LoggedOutEv) -> None:
        self._set_state(
            "logged_out",
            "WhatsApp logged Oscar out (the linked device was removed or expired). "
            "Restart the WhatsApp connection to link it again.",
        )
        logger.warning("Logged out of WhatsApp (reason %s)", event.Reason)
        self._login_cleared = await login_cleared(self.session_path)
        await self.client.stop()

    async def on_temporary_ban(self, _client, event: TemporaryBanEv) -> None:
        await self._fail(
            f"WhatsApp has temporarily banned Oscar's number (code {event.Code}, "
            f"expires {event.Expire}). Keep the WhatsApp connection stopped until the ban ends."
        )

    async def on_stream_replaced(self, _client, _event) -> None:
        await self._fail(
            "Oscar's WhatsApp session was opened by another copy of this program. "
            "Run only one WhatsApp connection at a time."
        )

    async def on_connect_failure(self, _client, event: ConnectFailureEv) -> None:
        await self._fail(
            f"WhatsApp refused the connection: {event.Reason} {event.Message}".strip()
        )

    async def on_client_outdated(self, _client, _event) -> None:
        await self._fail(
            "WhatsApp says this connection library is out of date. Update neonize "
            "(see Troubleshooting in the README), then restart."
        )

    async def on_disconnected(self, _client, _event) -> None:
        if self._last_status == "connected":
            self._set_state("starting", "Reconnecting to WhatsApp…")

    # Messages

    async def on_message(self, _client, event: MessageEv) -> None:
        await self.handle_message(event)

    async def handle_message(self, event: MessageEv) -> None:
        revoked = _revoked_message_id(event)
        if revoked:
            self.memory.forget(_jid_string(event.Info.MessageSource.Chat), revoked)
            return
        message = parse_message(event)
        if message is None:
            return
        if not self._first_delivery(message):
            return  # delivered again: it was remembered, and answered if it asked, the first time
        agent = self.store.get_agent()
        settings = self.store.get_whatsapp_settings()
        # Oscar may share its owner's own number instead of having one to itself.
        own_number = settings.own_number
        if len(self.my_ids) < 2 or (own_number and not self._owner_name):
            # Oscar's number, its other WhatsApp id (the LID, which group mentions
            # use) or the account's name is not known yet. Right after linking
            # that can take a moment.
            await self._load_my_ids()
        # Remember every message right away, in arrival order, so the context of
        # each answer includes whatever was said before it. A message typed on
        # Oscar's own phone counts as Oscar speaking, unless that phone is the
        # owner's: then it is the owner speaking.
        owner = self._owner_name or "The account's owner"
        if not message.from_me:
            self.memory.remember(message)
        elif own_number and not is_labelled_as_oscars(message.text, agent.name):
            self.memory.remember(message, speaker=owner, from_oscar=False)
        else:
            self.memory.remember(message, speaker=agent.name)

        # From here on, a chat's messages are handled one at a time, in the order
        # they arrived: a question that waits for a photo or a name is never
        # overtaken by a later one, and a quick second question sees the first answer.
        async with self._chat_locks.setdefault(message.chat_id, asyncio.Lock()):
            members_called_oscar: frozenset[str] = frozenset()
            if own_number:
                members_called_oscar = await self._mentioned_members_called(agent.name, event, message)
            addressed = is_addressed_to_oscar(
                message,
                my_ids=self.my_ids,
                agent_name=agent.name,
                reply_in_private_chats=settings.reply_in_private_chats,
                own_number=own_number,
                members_called_oscar=members_called_oscar,
            )
            # For --verbose: what arrived and what Oscar made of it, without its words.
            logger.debug(
                "Message in a %s: %s, from Oscar's own number: %s, mentions %d (the account among "
                "them: %s, members called %s among them: %d), replies to the account: %s, Oscar knows "
                "%d of its 2 ids, shares its owner's number: %s, addressed to Oscar: %s",
                "group" if message.is_group else "private chat",
                message.kind,
                message.from_me,
                len(message.mentioned_ids),
                bool(message.mentioned_ids & self.my_ids),
                agent.name,
                len(members_called_oscar),
                message.quoted_sender_id in self.my_ids,
                len(self.my_ids),
                own_number,
                addressed,
            )
            if not addressed or is_too_old_to_answer(message):
                return

            chat_label = await self._chat_label(event, message)
            asker = message.sender_name or "Someone"
            if own_number and message.from_me:
                asker = owner
            if not self.memory.allow_reply(message.chat_id):
                self.store.add_whatsapp_activity(
                    chat_name=chat_label,
                    asker=asker,
                    request=message.summary,
                    reply="",
                    error="Not answered: too many requests in the last few minutes.",
                )
                return

            # Collect everything from the event before replying: neonize's reply
            # helper clears the original message's context info in place.
            images = [] if message.view_once else await self._download_images(event)
            quoted = MessageEv()
            quoted.CopyFrom(event)
            chat_jid = event.Info.MessageSource.Chat

            # What came before this message, and Oscar's answers since.
            request_text = build_request_text(
                message,
                memory=self.memory,
                my_ids=self.my_ids,
                agent_name=agent.name,
                own_number=own_number,
                owner_name=self._owner_name,
                members_called_oscar=members_called_oscar,
            )
            now = datetime.now().astimezone()

            def system_prompt(web_search: bool) -> str:
                return build_system_prompt(
                    agent_name=agent.name,
                    chat_label=chat_label,
                    is_group=message.is_group,
                    instructions=settings.instructions,
                    web_search=web_search,
                    now=now,
                    own_number=own_number,
                    owner_name=self._owner_name,
                    knowledge=agent.knowledge,
                )

            # Searching is the owner's choice, and it needs a Firecrawl key.
            can_search = settings.web_search and self.cfg.search_enabled
            search_problems: list[str] = []
            await self._typing(chat_jid, True)
            error = ""
            try:
                raw = await brain.generate_reply(
                    api_key=self.cfg.openrouter_api_key,
                    base_url=self.cfg.openrouter_base_url,
                    model=agent.model,
                    system_prompt=system_prompt(can_search),
                    # Used if the model turns out not to support web search.
                    system_prompt_without_search=system_prompt(False),
                    request_text=request_text,
                    images=images,
                    web_search=can_search,
                    firecrawl_api_key=self.cfg.firecrawl_api_key,
                    firecrawl_base_url=self.cfg.firecrawl_base_url,
                    search_problems=search_problems,
                )
                reply = clean_reply(raw, agent.name) or SORRY_REPLY
                if search_problems:
                    # The reply went out, but the owner should know the search did not.
                    error = f"Web search failed: {search_problems[0]}"
            except brain.BrainError as exc:
                logger.warning("No reply for %s: %s", chat_label, exc)
                reply, error = SORRY_REPLY, str(exc)
            except Exception as exc:
                logger.exception("Unexpected error while asking the model")
                reply, error = SORRY_REPLY, f"Unexpected error: {exc}"
            finally:
                await self._typing(chat_jid, False)

            # From the owner's own number, a reply says in its first word that it is Oscar's.
            sent = f"{oscar_label(agent.name)} {reply}" if own_number else reply
            try:
                await self.client.reply_message(_reply_body(sent, message.expiration), quoted=quoted)
            except Exception as exc:
                logger.error("Could not send the reply in %s: %s", chat_label, exc)
                error = f"Could not send the reply: {exc}"
            else:
                self.memory.remember_reply(message.chat_id, agent.name, reply, time.time())
            self.store.add_whatsapp_activity(
                chat_name=chat_label, asker=asker, request=message.summary, reply=reply, error=error
            )

    def _first_delivery(self, message: IncomingMessage) -> bool:
        """False for a message WhatsApp has delivered already, so it is not remembered or answered twice."""
        if not message.message_id:
            return True
        key = (message.chat_id, message.message_id)
        if key in self._seen:
            return False
        self._seen[key] = None
        if len(self._seen) > SEEN_MESSAGES:
            del self._seen[next(iter(self._seen))]
        return True

    async def _mentioned_members_called(
        self, name: str, event: MessageEv, message: IncomingMessage
    ) -> frozenset[str]:
        """The members mentioned in a group message who go by `name` on WhatsApp.

        For when Oscar shares its owner's number: picking a member called Oscar
        from the @ list is then one of the ways people call Oscar.
        """
        others = message.mentioned_ids - self.my_ids
        if not others or not message.is_group or is_too_old_to_answer(message):
            return frozenset()
        names = {name.strip().casefold(), name_handle(name).casefold()}
        called = set()
        try:
            phones = await self._member_phones(event.Info.MessageSource.Chat, message.chat_id)
            for member in others:
                if member not in phones:
                    continue
                contact = await self.client.contact.get_contact(phones[member])
                if contact.Found and contact.PushName.strip().casefold() in names:
                    called.add(member)
        except Exception as exc:
            logger.debug("Could not look up who was mentioned: %s", exc)
        return frozenset(called)

    async def _member_phones(self, chat_jid, chat_id: str) -> dict:
        """Every id a group's members are known by, with the phone-number id names are kept under."""
        learned = self._group_members.get(chat_id)
        if learned and time.monotonic() - learned[0] < GROUP_MEMBERS_SECONDS:
            return learned[1]
        info = await self.client.get_group_info(chat_jid)
        phones = {}
        for member in info.Participants:
            phone = member.PhoneNumber if member.PhoneNumber.User else member.JID
            if phone.Server != "s.whatsapp.net":
                continue
            for known_as in (member.JID, member.LID, member.PhoneNumber):
                if known_as.User:
                    phones[known_as.User] = phone
        self._group_members[chat_id] = (time.monotonic(), phones)
        return phones

    async def _chat_label(self, event: MessageEv, message: IncomingMessage) -> str:
        if not message.is_group:
            if message.from_me:
                # Typed on the account's own phone (Oscar shares its owner's number): the
                # chat is with the other person, known by what they wrote in it.
                return next(iter(self.memory.names_in(message.chat_id).values()), "someone")
            return message.sender_name or "someone"
        if message.chat_id not in self._group_names:
            try:
                info = await self.client.get_group_info(event.Info.MessageSource.Chat)
                self._group_names[message.chat_id] = info.GroupName.Name or "this group"
            except Exception:
                logger.warning("Could not look up the group name for %s", message.chat_id)
                return "this group"
        return self._group_names[message.chat_id]

    async def _download_images(self, event: MessageEv) -> list[brain.Image]:
        images = []
        for source in _image_sources(event)[:2]:
            image = source.imageMessage
            if image.fileLength and image.fileLength > MAX_IMAGE_BYTES:
                continue
            try:
                data = await self.client.download_any(source)
            except Exception as exc:
                logger.warning("Could not download a photo: %s", exc)
                continue
            if data and len(data) <= MAX_IMAGE_BYTES:
                images.append(brain.Image(data=data, mime_type=image.mimetype or "image/jpeg"))
        return images

    async def _typing(self, chat_jid, typing: bool) -> None:
        state = ChatPresence.CHAT_PRESENCE_COMPOSING if typing else ChatPresence.CHAT_PRESENCE_PAUSED
        try:
            await self.client.send_chat_presence(
                chat_jid, state, ChatPresenceMedia.CHAT_PRESENCE_MEDIA_TEXT
            )
        except Exception as exc:
            logger.debug("Could not update typing status: %s", exc)

    # Running

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(WHATSAPP_HEARTBEAT_SECONDS)
            try:
                self.store.touch_whatsapp_heartbeat()
            except Exception:
                logger.exception("Could not write the WhatsApp heartbeat")

    async def run(self, stop: asyncio.Event | None = None) -> None:
        """Stay connected until WhatsApp ends the session or `stop` is set (Ctrl+C)."""
        stop = stop or asyncio.Event()
        self._set_state("starting", "Connecting to WhatsApp…")
        heartbeat = asyncio.create_task(self._heartbeat())
        stopping = asyncio.ensure_future(stop.wait())
        try:
            while True:
                self._renew.clear()
                self._codes_this_round = 0
                await self.client.connect()
                session = asyncio.ensure_future(self.client.idle())
                renewing = asyncio.ensure_future(self._renew.wait())
                await asyncio.wait({session, stopping, renewing}, return_when=asyncio.FIRST_COMPLETED)
                renewing.cancel()
                if not session.done():
                    # Tell the WhatsApp library to shut down; its worker thread
                    # would otherwise keep the process alive.
                    await self.client.stop()
                    await asyncio.wait({session}, timeout=CLOSE_PATIENCE_SECONDS)
                if session.done() and not session.cancelled() and session.exception():
                    raise session.exception()
                if stop.is_set() or not self._renew.is_set():
                    break
                if not session.done():
                    raise RuntimeError("the last connection did not close")
        except Exception as exc:
            logger.error("The WhatsApp connection failed: %s", exc)
            self.failed = True
            self._set_state("error", f"Could not connect to WhatsApp: {exc}")
        finally:
            stopping.cancel()
            heartbeat.cancel()
            self._cancel_code_expiry()
            self._stop_going_online()
            if self._last_status in ("logged_out", "error"):
                # Keep the reason on the dashboard, but show right away that
                # nothing is running any more.
                self.store.mark_whatsapp_exited()
            else:
                self._set_state("stopped", "The WhatsApp connection is not running.")
            if self._last_status == "logged_out" and self._login_cleared:
                forget_login(self.session_path)


def _login_is_cleared(session_path: Path) -> bool:
    if not session_path.exists():
        return True
    try:
        db = sqlite3.connect(f"{session_path.as_uri()}?mode=ro", uri=True, timeout=0.2)
        try:
            return db.execute("SELECT count(*) FROM whatsmeow_device").fetchone()[0] == 0
        finally:
            db.close()
    except sqlite3.Error:
        return False  # the library is in the middle of writing it


async def login_cleared(session_path: Path) -> bool:
    """Wait for the library to finish deleting a login WhatsApp has signed out. False if it did not."""
    deadline = time.monotonic() + LOGIN_CLEARING_PATIENCE_SECONDS
    while not _login_is_cleared(session_path):
        if time.monotonic() >= deadline:
            logger.warning("The saved WhatsApp login was not cleared in time")
            return False
        await asyncio.sleep(0.25)
    return True


def forget_login(session_path: Path) -> None:
    """Delete what is left of a saved login once the library has cleared it.

    What is left is not a login any more, but it still holds the numbers of
    that account's contacts. Only for use once login_cleared() has said yes.
    """
    for path in session_path.parent.glob(session_path.name + "*"):
        path.unlink(missing_ok=True)


async def unlink(cfg: Config, store: Store, client: NewAClient | None = None) -> tuple[bool, str]:
    """Sign Oscar out of the WhatsApp account it is linked to, so that another can be linked.

    Returns whether Oscar is now unlinked, and what to tell the owner. Oscar
    connects only to sign out: it reads no messages and answers nothing.
    """
    if store.get_whatsapp_state()["running"]:
        return False, "Stop the WhatsApp connection first (Ctrl+C in its terminal), then unlink."
    session_path = cfg.db_path.parent / "whatsapp-session.sqlite3"
    if client is None:
        if not session_path.exists():
            return True, "Oscar is not linked to a WhatsApp account."
        client = NewAClient(
            str(session_path), props=DeviceProps(os="Oscar", platformType=DeviceProps.DESKTOP)
        )

    heard: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    def hears(what: str):
        async def handler(*_args) -> None:
            if not heard.done():
                heard.set_result(what)

        return handler

    client.event.qr(hears("not linked"))  # WhatsApp offers a linking code: nothing is linked
    client.event(ConnectedEv)(hears("connected"))
    client.event(LoggedOutEv)(hears("logged out"))
    client.event(ConnectFailureEv)(hears("refused"))

    unlinked, message = False, f"WhatsApp did not answer, so Oscar is still linked. {UNLINK_ON_THE_PHONE}"
    cleared = False
    await client.connect()
    session = asyncio.ensure_future(client.idle())
    try:
        await asyncio.wait(
            {heard, session}, timeout=UNLINK_PATIENCE_SECONDS, return_when=asyncio.FIRST_COMPLETED
        )
        what = heard.result() if heard.done() else ""
        if what == "connected":
            me = await client.get_me()
            number = _jid_user(me.JID)
            await client.logout()
            unlinked = True
            message = (
                f"Oscar is unlinked from {'+' + number if number else 'that account'}. "
                "Start the WhatsApp connection to link another number."
            )
        elif what == "not linked":
            unlinked, message = True, "Oscar is not linked to a WhatsApp account."
        elif what == "logged out":
            unlinked, message = True, "Oscar was already unlinked: it had been removed on the phone."
        elif what == "refused":
            message = f"WhatsApp refused the connection, so Oscar is still linked. {UNLINK_ON_THE_PHONE}"
        if unlinked:
            cleared = await login_cleared(session_path)
    except Exception as exc:
        unlinked, message = False, f"Could not unlink Oscar ({exc}). {UNLINK_ON_THE_PHONE}"
    finally:
        heard.cancel()
        await client.stop()
        await asyncio.wait({session}, timeout=CLOSE_PATIENCE_SECONDS)
        if session.done() and not session.cancelled():
            session.exception()  # looked at, so that it is not reported as never retrieved
    if unlinked:
        if cleared:
            forget_login(session_path)
        else:
            message += " Its saved login is still being cleared: run this once more before linking again."
        # The dashboard must stop showing the old number as Oscar's.
        store.set_whatsapp_state("stopped", detail="The WhatsApp connection is not running.", phone="")
    return unlinked, message


def _revoked_message_id(event: MessageEv) -> str:
    """The id of the message someone deleted for everyone, if this event is such a deletion."""
    message = event.Message
    if message.HasField("protocolMessage"):
        protocol = message.protocolMessage
        if protocol.type == ProtocolMessage.REVOKE:
            return protocol.key.ID
    return ""


def _reply_body(text: str, expiration: int) -> str | ExtendedTextMessage:
    """Plain text, or (in a disappearing-messages chat) text that disappears too."""
    if not expiration:
        return text
    return ExtendedTextMessage(text=text, contextInfo=ContextInfo(expiration=expiration))
