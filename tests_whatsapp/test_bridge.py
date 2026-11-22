"""The WhatsApp bridge, with real neonize message objects and a fake WhatsApp connection.

Run with the WhatsApp environment: .venv-whatsapp/bin/python -m pytest tests_whatsapp
"""

import asyncio
import base64
import dataclasses
import json
import logging
import sqlite3
import time
from pathlib import Path

import httpx
import pytest
from neonize.events import (
    ClientOutdatedEv,
    ConnectedEv,
    ConnectFailureEv,
    LoggedOutEv,
    MessageEv,
    PairStatusEv,
    StreamReplacedEv,
    TemporaryBanEv,
)
from neonize.proto.Neonize_pb2 import ContactInfo, Device, GroupParticipant
from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import ExtendedTextMessage, Message, ProtocolMessage

from app.config import Config
from app.store import Store, WhatsAppSettings
from app.whatsapp import bridge as bridge_module
from app.whatsapp import brain
from app.whatsapp.bridge import SORRY_REPLY, WhatsAppBridge, parse_message
from app.whatsapp.chat import RATE_LIMIT_REPLIES

OSCAR_PHONE = "15005550006"
OSCAR_LID = "201234567890123"
JAY_PHONE, JAY_LID = "13055550100", "98765432101234"
ROMAN_PHONE = "15705550111"
GROUP = "120363000000000001"


# Building WhatsApp messages the way neonize delivers them


def group_event(
    text: str = "",
    *,
    sender: str = JAY_LID,
    sender_server: str = "lid",
    sender_alt: str = JAY_PHONE,
    name: str = "Jay",
    mentions: list[str] = (),
    quoted: Message | None = None,
    quoted_from: str = "",
    image: bool = False,
    timestamp: float | None = None,
    message_id: str = "MSG1",
    chat: str = GROUP,
    chat_server: str = "g.us",
    is_group: bool = True,
) -> MessageEv:
    event = MessageEv()
    source = event.Info.MessageSource
    source.Chat.User, source.Chat.Server = chat, chat_server
    source.Sender.User, source.Sender.Server = sender, sender_server
    if sender_alt:
        source.SenderAlt.User, source.SenderAlt.Server = sender_alt, "s.whatsapp.net"
    source.IsGroup = is_group
    source.IsFromMe = False
    event.Info.ID = message_id
    event.Info.Pushname = name
    event.Info.Timestamp = int(timestamp if timestamp is not None else time.time())

    if image:
        body = event.Message.imageMessage
        body.caption = text
        body.mimetype = "image/jpeg"
        body.fileLength = 1234
    elif mentions or quoted is not None:
        body = event.Message.extendedTextMessage
        body.text = text
    else:
        event.Message.conversation = text
        return event

    context = body.contextInfo
    context.mentionedJID.extend(mentions)
    if quoted is not None:
        context.quotedMessage.CopyFrom(quoted)
        context.participant = quoted_from
        context.stanzaID = "QUOTED1"
    return event


def text_message(text: str) -> Message:
    message = Message()
    message.conversation = text
    return message


def image_message(caption: str = "") -> Message:
    message = Message()
    message.imageMessage.caption = caption
    message.imageMessage.mimetype = "image/png"
    message.imageMessage.fileLength = 2048
    return message


# Parsing


def test_parse_group_text_with_lid_sender_and_mention():
    event = group_event(
        f"@{OSCAR_LID} can you get this",
        mentions=[f"{OSCAR_LID}@lid"],
        quoted=text_message("what time do they close tonight?"),
        quoted_from=f"{ROMAN_PHONE}@s.whatsapp.net",
        timestamp=1_800_000_000_000,  # milliseconds
    )
    message = parse_message(event)
    assert message.chat_id == f"{GROUP}@g.us"
    assert message.is_group
    assert message.sender_ids == frozenset({JAY_LID, JAY_PHONE})
    assert message.sender_name == "Jay"
    assert message.text == f"@{OSCAR_LID} can you get this"
    assert message.kind == "text"
    assert message.mentioned_ids == frozenset({OSCAR_LID})
    assert message.quoted_sender_id == ROMAN_PHONE
    assert (message.quoted_kind, message.quoted_text) == ("text", "what time do they close tonight?")
    assert message.timestamp == 1_800_000_000.0


def test_parse_plain_text_photo_and_device_suffixes():
    plain = parse_message(group_event("hello all", sender=JAY_PHONE, sender_server="s.whatsapp.net", sender_alt=""))
    assert (plain.kind, plain.text, plain.sender_ids) == ("text", "hello all", frozenset({JAY_PHONE}))

    photo = parse_message(group_event("new logo", image=True))
    assert (photo.kind, photo.text) == ("image", "new logo")

    device = parse_message(
        group_event("@oscar", mentions=[f"{OSCAR_PHONE}:12@s.whatsapp.net"], quoted=image_message("x"),
                    quoted_from=f"{OSCAR_PHONE}:3@s.whatsapp.net")
    )
    assert device.mentioned_ids == frozenset({OSCAR_PHONE})
    assert device.quoted_sender_id == OSCAR_PHONE
    assert device.quoted_kind == "image"


def test_parse_ignores_reactions_edits_status_and_channels():
    reaction = group_event("x")
    reaction.Message.Clear()
    reaction.Message.reactionMessage.text = "👍"
    assert parse_message(reaction) is None

    edit = group_event("@oscar edited")
    edit.IsEdit = True
    assert parse_message(edit) is None

    assert parse_message(group_event("status", chat="status", chat_server="broadcast")) is None
    assert parse_message(group_event("post", chat="12345", chat_server="newsletter")) is None


# The bridge, end to end, with a fake WhatsApp connection


class FakeEvents:
    def __init__(self):
        self.handlers = {}
        self.qr_handler = None

    def qr(self, func):
        self.qr_handler = func

    def __call__(self, event_type):
        def register(func):
            self.handlers[event_type] = func

        return register


class FakeContacts:
    """WhatsApp names by phone number, as the library keeps them."""

    def __init__(self):
        self.names: dict[str, str] = {}
        self.error: Exception | None = None
        self.lookups = 0

    async def get_contact(self, jid):
        self.lookups += 1
        if self.error:
            raise self.error
        known = jid.Server == "s.whatsapp.net" and jid.User in self.names
        return ContactInfo(Found=known, PushName=self.names.get(jid.User, "") if known else "")


def member(lid: str, phone: str) -> GroupParticipant:
    """A group member the way WhatsApp lists them now: by LID, with the phone number beside it."""
    listed = GroupParticipant()
    listed.JID.User, listed.JID.Server = lid, "lid"
    listed.LID.User, listed.LID.Server = lid, "lid"
    listed.PhoneNumber.User, listed.PhoneNumber.Server = phone, "s.whatsapp.net"
    return listed


class FakeClient:
    def __init__(self):
        self.event = FakeEvents()
        self.contact = FakeContacts()
        self.account_name = "Shrey"  # the linked account's WhatsApp name
        self.members: list[GroupParticipant] = []
        self.group_lookups = 0
        self.replies: list[tuple[str, MessageEv]] = []
        self.presence: list[int] = []
        self.downloads: list[Message] = []
        self.presence_sent: list[str] = []
        self.stopped = False
        self.connections = 0
        self.stops = 0
        self.on_stop = lambda: None
        self.stop_closes_the_connection = True
        self.idle_forever = asyncio.Event()
        self.idle_error: Exception | None = None
        self.reply_error: Exception | None = None
        self.presence_refusals = 0  # how many times WhatsApp will refuse to show Oscar online
        self.lid_known = True
        self.whatsapp_says = ""  # what arrives once connected: see connect()
        self.logged_out = False
        self.logout_error: Exception | None = None
        # The saved login the library deletes when Oscar is signed out, and how long it takes.
        self.login_file: Path | None = None
        self.clearing_takes: float | None = 0.3
        self.library_cleared_the_login = False

    async def get_me(self):
        me = Device()
        me.JID.User, me.JID.Server = OSCAR_PHONE, "s.whatsapp.net"
        me.PushName = self.account_name
        if self.lid_known:
            me.LID.User, me.LID.Server = OSCAR_LID, "lid"
        return me

    async def reply_message(self, text, quoted):
        if self.reply_error:
            raise self.reply_error
        self.replies.append((text, quoted))

    async def send_chat_presence(self, jid, state, media):
        self.presence.append(state)

    async def send_presence(self, presence):
        if self.presence_refusals:
            self.presence_refusals -= 1
            raise RuntimeError("can't send presence without PushName set")
        self.presence_sent.append(presence.name)

    async def get_group_info(self, jid):
        self.group_lookups += 1
        info = type("GroupInfo", (), {})()
        info.GroupName = type("GroupName", (), {"Name": "Weekend plans"})()
        info.Participants = self.members
        return info

    async def download_any(self, message):
        self.downloads.append(message)
        return b"\x89PNG fake image"

    async def connect(self):
        self.connections += 1
        self.idle_forever.clear()
        if self.whatsapp_says == "logged out":
            self.clear_login_soon()  # the library starts on it as it reports the logout
        arrival = {
            "connected": lambda: self.event.handlers[ConnectedEv](self, ConnectedEv()),
            "logged out": lambda: self.event.handlers[LoggedOutEv](self, LoggedOutEv()),
            "refused": lambda: self.event.handlers[ConnectFailureEv](self, ConnectFailureEv()),
            "a linking code": lambda: self.event.qr_handler(self, b"2@code"),
        }.get(self.whatsapp_says)
        if arrival:
            asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(arrival()))

    async def logout(self):
        if self.logout_error:
            raise self.logout_error
        self.logged_out = True
        self.clear_login()

    def clear_login(self):
        if self.login_file and not self.stopped:  # stopping cuts the library's work short
            with sqlite3.connect(self.login_file) as db:
                db.execute("DELETE FROM whatsmeow_device")
            self.library_cleared_the_login = True

    def clear_login_soon(self):
        if self.clearing_takes is not None:
            asyncio.get_running_loop().call_later(self.clearing_takes, self.clear_login)

    async def idle(self):
        if self.idle_error:
            raise self.idle_error
        await self.idle_forever.wait()

    async def stop(self):
        self.stopped = True
        self.stops += 1
        self.on_stop()
        if self.stop_closes_the_connection:
            self.idle_forever.set()


@pytest.fixture
def setup(tmp_path, monkeypatch, request):
    cfg = Config(
        openrouter_api_key="sk-or-test",
        twilio_account_sid="",
        twilio_auth_token="",
        twilio_phone_number="",
        public_url="",
        host="127.0.0.1",
        port=0,
        db_path=tmp_path / "oscar.db",
        firecrawl_api_key=getattr(request, "param", "fc-test"),
        firecrawl_base_url="https://firecrawl.test/v2",
    )
    store = Store(cfg.db_path)
    client = FakeClient()
    asked: list[dict] = []
    answers: list[object] = []

    async def fake_generate_reply(**kwargs):
        asked.append(kwargs)
        result = answers.pop(0) if answers else "Oscar: They close at **10pm**."
        if isinstance(result, Exception):
            raise result
        if isinstance(result, tuple):  # (the reply, why its web search failed)
            result, problem = result
            kwargs["search_problems"].append(problem)
        return result

    monkeypatch.setattr(bridge_module.brain, "generate_reply", fake_generate_reply)
    oscar = WhatsAppBridge(cfg, store, client=client)
    yield dataclasses.make_dataclass("Setup", ["bridge", "client", "store", "asked", "answers"])(
        oscar, client, store, asked, answers
    )
    store.close()


async def connect(setup):
    await setup.bridge.on_connected(setup.client, ConnectedEv())


async def test_connecting_records_oscars_number(setup):
    await connect(setup)
    assert setup.bridge.my_ids == frozenset({OSCAR_PHONE, OSCAR_LID})
    state = setup.store.get_whatsapp_state()
    assert (state["status"], state["phone"], state["running"]) == ("connected", f"+{OSCAR_PHONE}", True)


async def test_qr_code_is_stored_as_a_standalone_svg(setup):
    await setup.bridge.on_qr(setup.client, b"2@abc,def,ghi")
    state = setup.store.get_whatsapp_state()
    assert state["status"] == "waiting_for_scan" and state["has_qr"]
    assert setup.store.get_whatsapp_qr_svg().startswith('<svg xmlns="http://www.w3.org/2000/svg"')


async def test_ordinary_messages_are_only_remembered(setup):
    await connect(setup)
    await setup.bridge.handle_message(group_event("anyone up for ramen tonight?", name="Roman"))
    assert setup.client.replies == [] and setup.asked == []
    assert setup.store.list_whatsapp_activity() == []


async def test_at_oscar_gets_a_quoted_reply_with_context(setup):
    await connect(setup)
    await setup.bridge.handle_message(group_event("anyone up for ramen tonight?", name="Roman",
                                                  sender=ROMAN_PHONE, sender_server="s.whatsapp.net",
                                                  sender_alt="", message_id="M0"))
    event = group_event("@oscar when does Ippudo close?", message_id="M1")
    await setup.bridge.handle_message(event)

    (reply, quoted), = setup.client.replies
    assert reply == "They close at *10pm*."  # cleaned for WhatsApp
    assert quoted.Info.ID == "M1"
    assert quoted is not event  # a copy: neonize's reply helper mutates what it is given

    (call,) = setup.asked
    assert call["model"] == "anthropic/claude-haiku-5.5"
    assert call["web_search"] is True
    # Searches go through Firecrawl, with the key from .env.
    assert (call["firecrawl_api_key"], call["firecrawl_base_url"]) == ("fc-test", "https://firecrawl.test/v2")
    assert "You can search the web" in call["system_prompt"]
    assert call["images"] == []
    assert 'member of the WhatsApp group "Weekend plans"' in call["system_prompt"]
    assert "Roman: anyone up for ramen tonight?" in call["request_text"]
    assert call["request_text"].endswith("Jay: @oscar when does Ippudo close?")

    # Typing shown while thinking, then cleared.
    assert len(setup.client.presence) == 2 and setup.client.presence[0] != setup.client.presence[1]

    (activity,) = setup.store.list_whatsapp_activity()
    assert (activity["chat_name"], activity["asker"], activity["error"]) == ("Weekend plans", "Jay", "")
    assert activity["request"] == "@oscar when does Ippudo close?"
    assert activity["reply"] == "They close at *10pm*."


async def test_follow_up_sees_oscars_own_earlier_reply(setup):
    await connect(setup)
    await setup.bridge.handle_message(group_event("@oscar best ramen near Union Square?", message_id="A"))
    oscar_reply = setup.client.replies[0][0]
    await setup.bridge.handle_message(group_event("@oscar and is it open late?", message_id="B"))
    second_request = setup.asked[1]["request_text"]
    assert f"Oscar (you): {oscar_reply}" in second_request


async def test_real_mention_and_reply_to_oscar_both_trigger(setup):
    await connect(setup)
    await setup.bridge.handle_message(
        group_event(f"@{OSCAR_LID} thoughts?", mentions=[f"{OSCAR_LID}@lid"], message_id="A")
    )
    await setup.bridge.handle_message(
        group_event("why though?", quoted=text_message("Go with blue."),
                    quoted_from=f"{OSCAR_LID}@lid", message_id="B")
    )
    assert len(setup.client.replies) == 2
    assert "@Oscar thoughts?" in setup.asked[0]["request_text"]  # number shown as a name
    assert 'replying to you: "Go with blue."' in setup.asked[1]["request_text"]


async def test_photo_being_replied_to_is_sent_to_the_model(setup):
    await connect(setup)
    event = group_event("@oscar what are your thoughts on this?",
                        quoted=image_message("new logo"), quoted_from=f"{ROMAN_PHONE}@s.whatsapp.net")
    await setup.bridge.handle_message(event)
    (downloaded,) = setup.client.downloads
    assert downloaded.imageMessage.caption == "new logo"
    (image,) = setup.asked[0]["images"]
    assert image == brain.Image(data=b"\x89PNG fake image", mime_type="image/png")
    assert '"[photo] new logo"' in setup.asked[0]["request_text"]


async def test_photo_sent_with_the_mention_is_included(setup):
    await connect(setup)
    await setup.bridge.handle_message(group_event("@oscar is this ripe?", image=True))
    assert len(setup.client.downloads) == 1
    assert setup.asked[0]["images"][0].mime_type == "image/jpeg"


async def test_model_failure_gets_an_apology_and_is_logged(setup):
    await connect(setup)
    setup.answers.append(brain.BrainError("The OpenRouter account is out of credits."))
    await setup.bridge.handle_message(group_event("@oscar hi"))
    assert setup.client.replies[0][0] == SORRY_REPLY
    (activity,) = setup.store.list_whatsapp_activity()
    assert activity["error"] == "The OpenRouter account is out of credits."


async def test_rate_limit_stops_replies_in_a_busy_chat(setup):
    await connect(setup)
    for i in range(RATE_LIMIT_REPLIES + 1):
        await setup.bridge.handle_message(group_event(f"@oscar question {i}", message_id=f"Q{i}"))
    assert len(setup.client.replies) == RATE_LIMIT_REPLIES
    latest = setup.store.list_whatsapp_activity()[0]
    assert latest["reply"] == "" and "too many requests" in latest["error"]


async def test_old_mentions_are_not_answered(setup):
    await connect(setup)
    await setup.bridge.handle_message(group_event("@oscar hi", timestamp=time.time() - 3600))
    assert setup.client.replies == []


async def test_private_messages_follow_the_setting(setup):
    await connect(setup)
    private = dict(chat=JAY_PHONE, chat_server="s.whatsapp.net", is_group=False)
    await setup.bridge.handle_message(group_event("hey oscar", message_id="P1", **private))
    assert setup.client.replies == []

    setup.store.save_whatsapp_settings(WhatsAppSettings(reply_in_private_chats=True, web_search=False))
    await setup.bridge.handle_message(group_event("hey oscar", message_id="P2", **private))
    assert len(setup.client.replies) == 1
    assert "private WhatsApp chat with Jay" in setup.asked[0]["system_prompt"]
    assert setup.asked[0]["web_search"] is False


async def test_settings_and_name_come_from_the_dashboard(setup):
    await connect(setup)
    agent = setup.store.get_agent().model_copy(update={"name": "Ava", "model": "anthropic/claude-sonnet-5.5"})
    setup.store.save_agent(agent)
    setup.store.save_whatsapp_settings(WhatsAppSettings(instructions="Answer in French."))
    await setup.bridge.handle_message(group_event("@oscar hi", message_id="X"))
    assert setup.client.replies == []  # Oscar is now called Ava
    await setup.bridge.handle_message(group_event("@ava hi", message_id="Y"))
    assert setup.asked[0]["model"] == "anthropic/claude-sonnet-5.5"
    assert setup.asked[0]["system_prompt"].endswith("Answer in French.")


async def test_what_oscar_knows_reaches_its_whatsapp_replies(setup):
    await connect(setup)
    setup.store.save_agent(setup.store.get_agent().model_copy(update={"knowledge": "A men's haircut is $45."}))
    await setup.bridge.handle_message(group_event("@oscar how much is a haircut?"))
    (call,) = setup.asked
    assert "A men's haircut is $45." in call["system_prompt"]
    # Also in the instructions kept for when the model turns out not to search.
    assert "A men's haircut is $45." in call["system_prompt_without_search"]


async def test_a_crash_in_any_handler_is_logged_not_lost(setup, monkeypatch, caplog):
    await connect(setup)

    async def broken(event):
        raise RuntimeError("boom")

    monkeypatch.setattr(setup.bridge, "handle_message", broken)
    registered = setup.client.event.handlers[MessageEv]  # what neonize actually calls
    await registered(setup.client, group_event("@oscar hi"))  # must not raise
    assert "boom" in caplog.text


async def test_logged_out_stops_and_explains(setup):
    await setup.bridge.on_logged_out(setup.client, LoggedOutEv())
    assert setup.client.stopped
    state = setup.store.get_whatsapp_state()
    assert state["last_status"] == "logged_out" and "link it again" in state["detail"]


async def test_run_stops_cleanly_on_ctrl_c(setup):
    stop = asyncio.Event()
    task = asyncio.create_task(setup.bridge.run(stop))
    await asyncio.sleep(0.05)
    assert setup.store.get_whatsapp_state()["status"] == "starting"
    stop.set()
    await asyncio.wait_for(task, 5)
    assert setup.client.stopped
    assert setup.store.get_whatsapp_state()["status"] == "stopped"


def test_every_handler_is_registered(setup):
    registered = setup.client.event.handlers
    assert setup.client.event.qr_handler.__wrapped__ == setup.bridge.on_qr
    expected = {
        MessageEv: setup.bridge.on_message,
        ConnectedEv: setup.bridge.on_connected,
        LoggedOutEv: setup.bridge.on_logged_out,
        ClientOutdatedEv: setup.bridge.on_client_outdated,
    }
    for event_type, handler in expected.items():
        assert registered[event_type].__wrapped__ == handler


# Fixes from review


def code_lifetimes(monkeypatch, first: float, later: float) -> None:
    monkeypatch.setattr(bridge_module, "FIRST_CODE_SECONDS", first)
    monkeypatch.setattr(bridge_module, "LATER_CODE_SECONDS", later)
    monkeypatch.setattr(bridge_module, "CODE_GRACE_SECONDS", 0)


async def test_when_the_codes_run_out_oscar_connects_again_for_new_ones(setup, monkeypatch):
    code_lifetimes(monkeypatch, first=0.3, later=0.05)
    stop = asyncio.Event()
    task = asyncio.create_task(setup.bridge.run(stop))
    await asyncio.sleep(0.02)
    await setup.bridge.on_qr(setup.client, b"2@first")
    await asyncio.sleep(0.1)
    assert setup.store.get_whatsapp_state()["has_qr"]  # the first code lasts longer
    await setup.bridge.on_qr(setup.client, b"2@second")
    await asyncio.sleep(0.15)  # nothing replaced the second code: the round is over

    state = setup.store.get_whatsapp_state()
    assert (state["status"], state["has_qr"]) == ("starting", False)  # no dead code on show
    assert "new linking code" in state["detail"]
    assert (setup.client.stops, setup.client.connections) == (1, 2)
    assert not setup.bridge.failed and not task.done()

    # The new round starts over: its first code gets the longer time again.
    await setup.bridge.on_qr(setup.client, b"2@third")
    await asyncio.sleep(0.15)
    assert setup.store.get_whatsapp_state()["has_qr"]
    assert setup.client.connections == 2
    await connect(setup)
    await asyncio.sleep(0.3)
    assert setup.store.get_whatsapp_state()["status"] == "connected"
    assert (setup.client.stops, setup.client.connections) == (1, 2)
    stop.set()
    await asyncio.wait_for(task, 5)


async def test_linking_gives_up_after_a_while_and_says_so(setup, monkeypatch):
    code_lifetimes(monkeypatch, first=0.05, later=0.05)
    monkeypatch.setattr(bridge_module, "LINK_WINDOW_SECONDS", 0.2)
    task = asyncio.create_task(setup.bridge.run(asyncio.Event()))
    for _ in range(40):  # a new code whenever Oscar has connected again
        if task.done():
            break
        if setup.store.get_whatsapp_state()["status"] == "starting":
            await setup.bridge.on_qr(setup.client, b"2@code")
        await asyncio.sleep(0.02)
    await asyncio.wait_for(task, 5)
    state = setup.store.get_whatsapp_state()
    assert state["last_status"] == "error" and "expired" in state["detail"]
    assert state["has_qr"] is False and state["running"] is False
    assert setup.bridge.failed
    assert setup.client.connections > 1  # it did ask again before giving up


async def test_a_scanned_code_is_not_taken_for_one_that_ran_out(setup, monkeypatch):
    code_lifetimes(monkeypatch, first=0.05, later=0.05)
    task = asyncio.create_task(setup.bridge.run(asyncio.Event()))
    await asyncio.sleep(0.02)
    await setup.bridge.on_qr(setup.client, b"2@first")
    await setup.bridge.on_pair_status(setup.client, PairStatusEv(Status=PairStatusEv.SUCCESS))
    await asyncio.sleep(0.2)  # WhatsApp is reconnecting as Oscar's number
    state = setup.store.get_whatsapp_state()
    assert state["status"] == "starting" and "Linked" in state["detail"]
    assert (setup.client.stops, setup.client.connections) == (0, 1)
    await setup.bridge.on_logged_out(setup.client, LoggedOutEv())
    await asyncio.wait_for(task, 5)


async def test_a_failed_linking_is_not_papered_over_with_a_new_code(setup, monkeypatch):
    code_lifetimes(monkeypatch, first=0.05, later=0.05)
    task = asyncio.create_task(setup.bridge.run(asyncio.Event()))
    await asyncio.sleep(0.02)
    await setup.bridge.on_qr(setup.client, b"2@first")
    await setup.bridge.on_pair_status(setup.client, PairStatusEv(Error="the phone refused"))
    await asyncio.sleep(0.2)  # the code's time passes
    state = setup.store.get_whatsapp_state()
    assert state["status"] == "error" and "the phone refused" in state["detail"]
    assert (setup.client.stops, setup.client.connections) == (0, 1)
    await setup.bridge.on_logged_out(setup.client, LoggedOutEv())
    await asyncio.wait_for(task, 5)


async def test_ctrl_c_while_getting_new_codes_does_not_connect_again(setup, monkeypatch):
    code_lifetimes(monkeypatch, first=0.05, later=0.05)
    stop = asyncio.Event()
    setup.client.on_stop = stop.set  # Ctrl+C lands while the old connection is closing
    task = asyncio.create_task(setup.bridge.run(stop))
    await asyncio.sleep(0.02)
    await setup.bridge.on_qr(setup.client, b"2@first")
    await asyncio.wait_for(task, 5)
    assert setup.client.connections == 1
    assert setup.store.get_whatsapp_state()["status"] == "stopped"
    assert not setup.bridge.failed


async def test_oscar_never_opens_a_second_connection_beside_one_that_will_not_close(setup, monkeypatch):
    code_lifetimes(monkeypatch, first=0.05, later=0.05)
    monkeypatch.setattr(bridge_module, "CLOSE_PATIENCE_SECONDS", 0.05)
    setup.client.stop_closes_the_connection = False
    task = asyncio.create_task(setup.bridge.run(asyncio.Event()))
    await asyncio.sleep(0.02)
    await setup.bridge.on_qr(setup.client, b"2@first")
    await asyncio.wait_for(task, 5)
    state = setup.store.get_whatsapp_state()
    assert state["last_status"] == "error" and "did not close" in state["detail"]
    assert setup.client.connections == 1 and setup.bridge.failed
    setup.client.idle_forever.set()  # let the stand-in connection finish
    await asyncio.sleep(0)


async def test_new_codes_and_linking_cancel_the_expiry(setup, monkeypatch):
    code_lifetimes(monkeypatch, first=0.15, later=0.15)
    await setup.bridge.on_qr(setup.client, b"2@first")
    await asyncio.sleep(0.1)
    await setup.bridge.on_qr(setup.client, b"2@second")  # resets the timer
    await asyncio.sleep(0.1)
    assert setup.store.get_whatsapp_state()["status"] == "waiting_for_scan"
    await connect(setup)
    await asyncio.sleep(0.2)
    assert setup.store.get_whatsapp_state()["status"] == "connected"
    assert not setup.client.stopped
    assert setup.client.presence_sent == ["AVAILABLE"]


def online_retries(monkeypatch, attempts: int) -> None:
    monkeypatch.setattr(bridge_module, "ONLINE_RETRY_SECONDS", 0.02)
    monkeypatch.setattr(bridge_module, "ONLINE_ATTEMPTS", attempts)


async def test_oscar_goes_online_once_whatsapp_allows_it(setup, monkeypatch, caplog):
    # Right after linking, WhatsApp refuses until it has sent the account's name.
    online_retries(monkeypatch, attempts=5)
    setup.client.presence_refusals = 2
    with caplog.at_level(logging.WARNING, logger="oscar.whatsapp"):
        await connect(setup)
        assert setup.client.presence_sent == []
        assert setup.store.get_whatsapp_state()["status"] == "connected"  # not held up by it
        await asyncio.sleep(0.2)
    assert setup.client.presence_sent == ["AVAILABLE"]  # and it does not keep asking after that
    assert not caplog.records


async def test_oscar_stops_asking_to_go_online_after_a_while(setup, monkeypatch, caplog):
    online_retries(monkeypatch, attempts=3)
    setup.client.presence_refusals = 50
    with caplog.at_level(logging.WARNING, logger="oscar.whatsapp"):
        await connect(setup)
        await asyncio.sleep(0.2)
    assert setup.client.presence_refusals == 47  # three tries, then no more
    assert [r.getMessage() for r in caplog.records] == [
        "Could not set Oscar as online. Its replies still work."
    ]


async def test_oscar_does_not_ask_to_go_online_once_it_is_disconnected(setup, monkeypatch):
    online_retries(monkeypatch, attempts=5)
    setup.client.presence_refusals = 1
    await connect(setup)
    await setup.bridge.on_logged_out(setup.client, LoggedOutEv())
    await asyncio.sleep(0.1)
    assert setup.client.presence_sent == [] and setup.client.presence_refusals == 0


async def test_stopping_ends_the_wait_to_go_online(setup, monkeypatch):
    online_retries(monkeypatch, attempts=5)
    monkeypatch.setattr(bridge_module, "ONLINE_RETRY_SECONDS", 0.1)
    setup.client.presence_refusals = 1
    stop = asyncio.Event()
    task = asyncio.create_task(setup.bridge.run(stop))
    await asyncio.sleep(0.02)
    await connect(setup)
    stop.set()
    await asyncio.wait_for(task, 5)
    await asyncio.sleep(0.2)
    assert setup.client.presence_sent == []


async def test_outdated_client_is_reported(setup):
    await setup.bridge.on_client_outdated(setup.client, ClientOutdatedEv())
    state = setup.store.get_whatsapp_state()
    assert state["last_status"] == "error" and "Update neonize" in state["detail"]
    assert setup.client.stopped


async def test_failed_connection_is_an_error_not_a_normal_stop(setup):
    setup.client.idle_error = RuntimeError("dial tcp: no route to host")
    await setup.bridge.run(asyncio.Event())
    state = setup.store.get_whatsapp_state()
    assert state["last_status"] == "error"
    assert "no route to host" in state["detail"]
    assert setup.bridge.failed


async def test_deleted_messages_are_forgotten(setup):
    await connect(setup)
    await setup.bridge.handle_message(group_event("my address is 1 Main St", message_id="SECRET"))
    deletion = group_event("")
    deletion.Message.Clear()
    deletion.Message.protocolMessage.type = ProtocolMessage.REVOKE
    deletion.Message.protocolMessage.key.ID = "SECRET"
    await setup.bridge.handle_message(deletion)
    await setup.bridge.handle_message(group_event("@oscar what did I just say?", message_id="Q"))
    assert "1 Main St" not in setup.asked[0]["request_text"]


async def test_view_once_media_is_never_downloaded_or_shown(setup):
    await connect(setup)
    event = group_event("@oscar look", image=True)
    event.IsViewOnceV2 = True
    await setup.bridge.handle_message(event)
    assert setup.client.downloads == []
    assert setup.asked[0]["images"] == []
    assert "[view-once media]" in setup.asked[0]["request_text"]


async def test_replies_disappear_in_disappearing_chats(setup):
    await connect(setup)
    event = group_event("@oscar hi", mentions=["0@s.whatsapp.net"])
    event.Message.extendedTextMessage.contextInfo.expiration = 604800
    await setup.bridge.handle_message(event)
    body, _ = setup.client.replies[0]
    assert isinstance(body, ExtendedTextMessage)
    assert body.text == "They close at *10pm*."
    assert body.contextInfo.expiration == 604800

    await setup.bridge.handle_message(group_event("@oscar again", message_id="M2"))
    assert setup.client.replies[1][0] == "They close at *10pm*."  # normal chats: plain text


async def test_send_failure_is_recorded(setup):
    await connect(setup)
    setup.client.reply_error = RuntimeError("not allowed to send in this group")
    await setup.bridge.handle_message(group_event("@oscar hi"))
    (activity,) = setup.store.list_whatsapp_activity()
    assert "not allowed to send" in activity["error"]


async def test_unexpected_model_errors_still_get_an_apology(setup):
    await connect(setup)
    setup.answers.append(ValueError("weird"))
    await setup.bridge.handle_message(group_event("@oscar hi"))
    assert setup.client.replies[0][0] == SORRY_REPLY
    assert "weird" in setup.store.list_whatsapp_activity()[0]["error"]


async def test_quick_second_question_sees_the_first_answer(setup, monkeypatch):
    await connect(setup)

    async def slow_reply(**kwargs):
        setup.asked.append(kwargs)
        await asyncio.sleep(0.05)
        return f"Answer {len(setup.asked)}"

    monkeypatch.setattr(bridge_module.brain, "generate_reply", slow_reply)
    await asyncio.gather(
        setup.bridge.handle_message(group_event("@oscar first?", message_id="F1")),
        setup.bridge.handle_message(group_event("@oscar second?", message_id="F2")),
    )
    assert [text for text, _ in setup.client.replies] == ["Answer 1", "Answer 2"]
    second = setup.asked[1]["request_text"]
    assert "Jay: @oscar first?" in second and "Oscar (you): Answer 1" in second


async def test_own_ids_are_loaded_late_if_needed(setup):
    # Connected event lost: the first message still learns Oscar's ids.
    await setup.bridge.handle_message(
        group_event(f"@{OSCAR_LID} hi", mentions=[f"{OSCAR_LID}@lid"])
    )
    assert setup.bridge.my_ids == frozenset({OSCAR_PHONE, OSCAR_LID})
    assert len(setup.client.replies) == 1


async def test_an_id_oscar_learns_after_connecting_still_counts(setup):
    # Right after linking, WhatsApp may know Oscar's number but not yet its LID,
    # which is what a mention in a group carries.
    setup.client.lid_known = False
    await connect(setup)
    assert setup.bridge.my_ids == frozenset({OSCAR_PHONE})
    setup.client.lid_known = True
    await setup.bridge.handle_message(
        group_event(f"@{OSCAR_LID} hi", mentions=[f"{OSCAR_LID}@lid"])
    )
    assert setup.bridge.my_ids == frozenset({OSCAR_PHONE, OSCAR_LID})
    assert len(setup.client.replies) == 1


async def test_verbose_logging_says_what_arrived_but_never_its_words(setup, caplog):
    await connect(setup)
    with caplog.at_level(logging.DEBUG, logger="oscar.whatsapp"):
        await setup.bridge.handle_message(group_event("the gate code is 4471", message_id="G1"))
        await setup.bridge.handle_message(
            group_event(f"@{OSCAR_LID} what is the gate code?", mentions=[f"{OSCAR_LID}@lid"], message_id="G2")
        )
    arrived = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Message in")]
    assert len(arrived) == 2
    assert "mentions 0" in arrived[0] and arrived[0].endswith("addressed to Oscar: False")
    assert "mentions 1 (the account among them: True" in arrived[1]
    assert arrived[1].endswith("addressed to Oscar: True")
    logged = caplog.text
    assert "4471" not in logged and "gate code" not in logged
    assert OSCAR_LID not in logged and OSCAR_PHONE not in logged


# Oscar on its owner's own number

OTHER_OSCAR_LID, OTHER_OSCAR_PHONE = "209999999999999", "15005550177"


def from_the_owner(event: MessageEv) -> MessageEv:
    """Typed on the phone whose WhatsApp account Oscar shares."""
    event.Info.MessageSource.IsFromMe = True
    event.Info.MessageSource.Sender.User = OSCAR_LID
    event.Info.MessageSource.SenderAlt.User = OSCAR_PHONE
    event.Info.Pushname = ""
    return event


@pytest.fixture
def own(setup):
    """Oscar connected on its owner's own number, in a group with another member called Oscar."""
    setup.store.save_whatsapp_settings(WhatsAppSettings(own_number=True))
    setup.client.members = [member(JAY_LID, JAY_PHONE), member(OTHER_OSCAR_LID, OTHER_OSCAR_PHONE)]
    setup.client.contact.names = {JAY_PHONE: "Jay", OTHER_OSCAR_PHONE: "Oscar"}
    return setup


async def test_on_the_owners_number_oscar_answers_to_its_name_and_signs_the_reply(own):
    await connect(own)
    await own.bridge.handle_message(group_event("@oscar when does Ippudo close?", message_id="M1"))
    (reply, quoted), = own.client.replies
    assert reply == "*Oscar:* They close at *10pm*."  # nobody takes it for the owner's own words
    assert quoted.Info.ID == "M1"
    (call,) = own.asked
    assert "an AI assistant that Shrey runs from their own WhatsApp account" in call["system_prompt"]
    (activity,) = own.store.list_whatsapp_activity()
    assert activity["reply"] == "They close at *10pm*."


async def test_on_the_owners_number_what_is_meant_for_the_owner_is_left_alone(own):
    await connect(own)
    # A mention of the account, a reply to one of its messages, and ordinary talk.
    await own.bridge.handle_message(group_event(f"@{OSCAR_LID} are you coming?", mentions=[f"{OSCAR_LID}@lid"]))
    await own.bridge.handle_message(
        group_event("sounds good", quoted=text_message("see you at 8"), quoted_from=f"{OSCAR_LID}@lid", message_id="M2")
    )
    await own.bridge.handle_message(group_event("morning all", message_id="M3"))
    assert own.client.replies == [] and own.asked == []
    assert own.store.list_whatsapp_activity() == []


async def test_the_owner_can_ask_oscar_from_their_own_phone(own):
    await connect(own)
    await own.bridge.handle_message(from_the_owner(group_event("on the shelf", message_id="M1")))
    assert own.client.replies == []
    await own.bridge.handle_message(from_the_owner(group_event("@oscar what is 2+2", message_id="M2")))
    (reply, _), = own.client.replies
    assert reply.startswith("*Oscar:* ")
    (call,) = own.asked
    # What the owner typed earlier is theirs, not Oscar's.
    assert "] Shrey: on the shelf" in call["request_text"] and "(you)" not in call["request_text"]
    assert call["request_text"].endswith("] Shrey: @oscar what is 2+2")
    assert own.store.list_whatsapp_activity()[0]["asker"] == "Shrey"


async def test_oscars_earlier_answer_is_its_own_in_the_next_question(own):
    await connect(own)
    await own.bridge.handle_message(group_event("@oscar when does Ippudo close?", message_id="M1"))
    await own.bridge.handle_message(group_event("@oscar and Ichiran?", message_id="M2"))
    assert "] Oscar (you): They close at *10pm*." in own.asked[1]["request_text"]


async def test_picking_a_member_called_oscar_from_the_list_calls_oscar(own):
    await connect(own)
    await own.bridge.handle_message(
        group_event(f"@{OTHER_OSCAR_LID} what is the plan?", mentions=[f"{OTHER_OSCAR_LID}@lid"])
    )
    (reply, _), = own.client.replies
    assert reply.startswith("*Oscar:* ")
    assert own.asked[0]["request_text"].endswith("Jay: @Oscar what is the plan?")


async def test_picking_a_member_with_another_name_does_not_call_oscar(own):
    await connect(own)
    await own.bridge.handle_message(group_event(f"@{JAY_LID} you there?", mentions=[f"{JAY_LID}@lid"],
                                                sender=ROMAN_PHONE, sender_server="s.whatsapp.net",
                                                sender_alt="", name="Roman"))
    assert own.client.replies == []
    assert own.client.contact.lookups == 1  # it did look


async def test_with_a_number_to_itself_oscar_is_never_another_member(setup):
    setup.client.members = [member(OTHER_OSCAR_LID, OTHER_OSCAR_PHONE)]
    setup.client.contact.names = {OTHER_OSCAR_PHONE: "Oscar"}
    await connect(setup)
    await setup.bridge.handle_message(
        group_event(f"@{OTHER_OSCAR_LID} what is the plan?", mentions=[f"{OTHER_OSCAR_LID}@lid"])
    )
    assert setup.client.replies == []
    assert (setup.client.group_lookups, setup.client.contact.lookups) == (0, 0)  # nor does it look


async def test_a_reply_to_one_of_oscars_signed_answers_is_answered(own):
    await connect(own)
    await own.bridge.handle_message(
        group_event(
            "and on Sundays?",
            quoted=text_message("*Oscar:* They close at *10pm*."),
            quoted_from=f"{OSCAR_LID}@lid",
        )
    )
    (reply, _), = own.client.replies
    assert reply.startswith("*Oscar:* ")
    assert 'replying to you: "They close at *10pm*."' in own.asked[0]["request_text"]


async def test_who_is_in_a_group_is_asked_once_and_kept_for_a_while(own, monkeypatch):
    await connect(own)
    pick = lambda n: group_event(f"@{OTHER_OSCAR_LID} hi", mentions=[f"{OTHER_OSCAR_LID}@lid"], message_id=f"M{n}")
    await own.bridge.handle_message(pick(1))
    await own.bridge.handle_message(pick(2))
    assert len(own.client.replies) == 2
    assert own.client.group_lookups == 2  # once for the members, once for the group's name
    monkeypatch.setattr(bridge_module, "GROUP_MEMBERS_SECONDS", 0)
    await own.bridge.handle_message(pick(3))
    assert own.client.group_lookups == 3


async def test_a_name_that_cannot_be_looked_up_calls_nobody(own, caplog):
    own.client.contact.error = RuntimeError("the contact store is closed")
    await connect(own)
    with caplog.at_level(logging.WARNING, logger="oscar.whatsapp"):
        await own.bridge.handle_message(
            group_event(f"@{OTHER_OSCAR_LID} hi", mentions=[f"{OTHER_OSCAR_LID}@lid"])
        )
        # Oscar's name typed out still works.
        await own.bridge.handle_message(group_event("@oscar hi", message_id="M2"))
    assert len(own.client.replies) == 1
    assert not caplog.records


async def test_old_messages_are_not_worth_looking_names_up_for(own):
    await connect(own)
    await own.bridge.handle_message(
        group_event(f"@{OTHER_OSCAR_LID} hi", mentions=[f"{OTHER_OSCAR_LID}@lid"], timestamp=time.time() - 3600)
    )
    assert own.client.replies == []
    assert (own.client.group_lookups, own.client.contact.lookups) == (0, 0)


async def test_the_owners_name_is_learned_late_if_whatsapp_had_not_sent_it(own):
    own.client.account_name = ""  # right after linking
    await connect(own)
    own.client.account_name = "Shrey"
    await own.bridge.handle_message(from_the_owner(group_event("@oscar hi")))
    assert own.asked[0]["request_text"].endswith("] Shrey: @oscar hi")


async def test_an_apology_from_the_owners_number_is_signed_too(own):
    own.answers.append(brain.BrainError("out of credits"))
    await connect(own)
    await own.bridge.handle_message(group_event("@oscar hi"))
    (reply, _), = own.client.replies
    assert reply == f"*Oscar:* {SORRY_REPLY}"


async def test_private_messages_on_the_owners_number_are_the_owners(own):
    own.store.save_whatsapp_settings(WhatsAppSettings(own_number=True, reply_in_private_chats=True))
    await connect(own)
    dm = dict(chat=JAY_PHONE, chat_server="s.whatsapp.net", is_group=False)
    await own.bridge.handle_message(group_event("are you free tonight?", **dm))
    assert own.client.replies == []
    await own.bridge.handle_message(group_event("@oscar what is 2+2", message_id="M2", **dm))
    assert len(own.client.replies) == 1


# Unlinking


def saved_login(setup, client=None, linked: bool = True) -> list[Path]:
    """A saved WhatsApp login like the library's: a database with one row per linked account."""
    session = setup.bridge.session_path
    with sqlite3.connect(session) as db:
        db.execute("CREATE TABLE whatsmeow_device (jid TEXT)")
        if linked:
            db.execute("INSERT INTO whatsmeow_device VALUES (?)", (f"{OSCAR_PHONE}@s.whatsapp.net",))
    beside = session.with_name(session.name + "-journal")
    beside.write_bytes(b"")
    (client or setup.client).login_file = session
    return [session, beside]


def is_still_linked(session: Path) -> bool:
    with sqlite3.connect(session) as db:
        return db.execute("SELECT count(*) FROM whatsmeow_device").fetchone()[0] == 1


def linked_to_the_wrong_number(setup, whatsapp_says: str) -> FakeClient:
    """A stopped Oscar whose dashboard still shows the number it was linked to."""
    setup.store.set_whatsapp_state("connected", phone=f"+{OSCAR_PHONE}")
    setup.store.set_whatsapp_state("stopped")
    client = FakeClient()
    client.whatsapp_says = whatsapp_says
    return client


@pytest.mark.parametrize("whatsapp_says", ["connected", "logged out", "a linking code"])
async def test_unlinking_deletes_the_saved_login_once_the_library_has_cleared_it(setup, whatsapp_says):
    client = linked_to_the_wrong_number(setup, whatsapp_says)
    files = saved_login(setup, client, linked=whatsapp_says != "a linking code")
    other = setup.bridge.session_path.with_name("oscar.db")
    unlinked, message = await bridge_module.unlink(setup.bridge.cfg, setup.store, client=client)
    assert unlinked and "still being cleared" not in message
    # The library was left to finish (stopping it first cuts that short) ...
    assert client.library_cleared_the_login is (whatsapp_says != "a linking code")
    # ... and only then were the files deleted.
    assert not any(file.exists() for file in files)
    assert other.exists()  # Oscar's own records are untouched


async def test_unlinking_never_deletes_a_login_the_library_is_still_clearing(setup, monkeypatch):
    # Deleting the files under the library while it works has crashed it.
    monkeypatch.setattr(bridge_module, "LOGIN_CLEARING_PATIENCE_SECONDS", 0.3)
    client = linked_to_the_wrong_number(setup, "logged out")
    client.clearing_takes = None  # it never finishes
    files = saved_login(setup, client)
    unlinked, message = await bridge_module.unlink(setup.bridge.cfg, setup.store, client=client)
    assert unlinked and "run this once more" in message
    assert all(file.exists() for file in files)


@pytest.mark.parametrize(
    ("contents", "cleared"),
    [
        (None, True),  # no file: nothing is saved
        ("linked", False),
        ("unlinked", True),
        ("no tables yet", False),
        ("not a database", False),  # unreadable: never taken for cleared
    ],
)
def test_a_login_counts_as_cleared_only_when_no_account_is_left_in_it(tmp_path, contents, cleared):
    session = tmp_path / "whatsapp-session.sqlite3"
    if contents == "not a database":
        session.write_bytes(b"x" * 4096)
    elif contents:
        with sqlite3.connect(session) as db:
            if contents != "no tables yet":
                db.execute("CREATE TABLE whatsmeow_device (jid TEXT)")
            if contents == "linked":
                db.execute("INSERT INTO whatsmeow_device VALUES ('1@s.whatsapp.net')")
    assert bridge_module._login_is_cleared(session) is cleared


def test_a_login_being_written_does_not_count_as_cleared(tmp_path):
    session = tmp_path / "whatsapp-session.sqlite3"
    with sqlite3.connect(session) as db:
        db.execute("CREATE TABLE whatsmeow_device (jid TEXT)")
    writer = sqlite3.connect(session, isolation_level=None)
    try:
        writer.execute("BEGIN EXCLUSIVE")  # the library, part-way through deleting
        assert bridge_module._login_is_cleared(session) is False
        writer.execute("COMMIT")
        assert bridge_module._login_is_cleared(session) is True
    finally:
        writer.close()


async def test_a_failed_unlink_keeps_the_saved_login(setup, monkeypatch):
    monkeypatch.setattr(bridge_module, "UNLINK_PATIENCE_SECONDS", 0.05)
    client = linked_to_the_wrong_number(setup, "")
    files = saved_login(setup, client)
    unlinked, _ = await bridge_module.unlink(setup.bridge.cfg, setup.store, client=client)
    assert not unlinked and all(file.exists() for file in files)
    assert is_still_linked(files[0])


async def test_stopping_oscar_keeps_its_login(setup):
    files = saved_login(setup)
    stop = asyncio.Event()
    task = asyncio.create_task(setup.bridge.run(stop))
    await asyncio.sleep(0.02)
    await connect(setup)
    stop.set()  # Ctrl+C: Oscar stays linked for next time
    await asyncio.wait_for(task, 5)
    assert all(file.exists() for file in files) and is_still_linked(files[0])


async def test_a_logged_out_oscar_lets_the_library_clear_the_login_before_stopping(setup):
    # Stopping straight away cut the clearing short: the dead login came back on
    # the next start, to be refused again.
    files = saved_login(setup)
    task = asyncio.create_task(setup.bridge.run(asyncio.Event()))
    await asyncio.sleep(0.02)
    setup.client.clear_login_soon()
    await setup.bridge.on_logged_out(setup.client, LoggedOutEv())
    await asyncio.wait_for(task, 5)
    assert setup.client.library_cleared_the_login
    assert not any(file.exists() for file in files)
    assert setup.store.get_whatsapp_state()["last_status"] == "logged_out"


async def test_a_logged_out_oscar_leaves_a_login_that_is_still_being_cleared(setup, monkeypatch):
    monkeypatch.setattr(bridge_module, "LOGIN_CLEARING_PATIENCE_SECONDS", 0.3)
    files = saved_login(setup)
    task = asyncio.create_task(setup.bridge.run(asyncio.Event()))
    await asyncio.sleep(0.02)
    await setup.bridge.on_logged_out(setup.client, LoggedOutEv())  # and the library never finishes
    await asyncio.wait_for(task, 5)
    assert all(file.exists() for file in files)
    assert setup.client.stopped


async def test_a_problem_that_is_not_a_logout_keeps_the_saved_login(setup):
    files = saved_login(setup)
    task = asyncio.create_task(setup.bridge.run(asyncio.Event()))
    await asyncio.sleep(0.02)
    await setup.bridge.on_stream_replaced(setup.client, StreamReplacedEv())
    await asyncio.wait_for(task, 5)
    assert setup.store.get_whatsapp_state()["last_status"] == "error"
    assert all(file.exists() for file in files)


async def test_unlinking_signs_oscar_out_and_forgets_the_number(setup):
    client = linked_to_the_wrong_number(setup, "connected")
    unlinked, message = await bridge_module.unlink(setup.bridge.cfg, setup.store, client=client)
    assert unlinked and client.logged_out
    assert f"unlinked from +{OSCAR_PHONE}" in message
    state = setup.store.get_whatsapp_state()
    assert (state["running"], state["status"], state["phone"]) == (False, "stopped", "")
    assert client.stopped  # the connection is closed again


async def test_unlinking_reads_and_answers_nothing(setup):
    client = linked_to_the_wrong_number(setup, "connected")
    await bridge_module.unlink(setup.bridge.cfg, setup.store, client=client)
    assert MessageEv not in client.event.handlers
    assert client.presence_sent == [] and client.replies == []


async def test_unlinking_refuses_while_oscar_is_connected(setup):
    await connect(setup)
    client = FakeClient()
    unlinked, message = await bridge_module.unlink(setup.bridge.cfg, setup.store, client=client)
    assert not unlinked and "Stop the WhatsApp connection first" in message
    assert client.connections == 0 and not client.logged_out
    assert setup.store.get_whatsapp_state()["phone"] == f"+{OSCAR_PHONE}"


@pytest.mark.parametrize(
    ("whatsapp_says", "expected"),
    [("a linking code", "not linked"), ("logged out", "already unlinked")],
)
async def test_unlinking_when_nothing_is_linked_says_so(setup, whatsapp_says, expected):
    client = linked_to_the_wrong_number(setup, whatsapp_says)
    unlinked, message = await bridge_module.unlink(setup.bridge.cfg, setup.store, client=client)
    assert unlinked and expected in message
    assert not client.logged_out and client.stopped
    state = setup.store.get_whatsapp_state()
    assert state["phone"] == "" and state["has_qr"] is False  # no code is put on show


@pytest.mark.parametrize(
    ("whatsapp_says", "logout_error", "expected"),
    [
        ("connected", RuntimeError("iq timed out"), "Could not unlink Oscar (iq timed out)"),
        ("refused", None, "WhatsApp refused the connection"),
        ("", None, "WhatsApp did not answer"),
    ],
)
async def test_a_failed_unlink_says_oscar_is_still_linked(
    setup, monkeypatch, whatsapp_says, logout_error, expected
):
    monkeypatch.setattr(bridge_module, "UNLINK_PATIENCE_SECONDS", 0.05)
    client = linked_to_the_wrong_number(setup, whatsapp_says)
    client.logout_error = logout_error
    unlinked, message = await bridge_module.unlink(setup.bridge.cfg, setup.store, client=client)
    assert not unlinked and expected in message
    assert "Linked devices" in message  # the way that always works
    assert setup.store.get_whatsapp_state()["phone"] == f"+{OSCAR_PHONE}"  # still shown as linked
    assert client.stopped


async def test_unlinking_with_no_saved_login_never_connects(setup):
    # No client is handed in, and no session file exists: nothing to sign out of.
    unlinked, message = await bridge_module.unlink(setup.bridge.cfg, setup.store)
    assert unlinked and "not linked" in message
    assert not (setup.bridge.cfg.db_path.parent / "whatsapp-session.sqlite3").exists()


# Fixes from the bug hunt


@pytest.mark.parametrize(
    ("handler", "event", "expected"),
    [
        ("on_temporary_ban", TemporaryBanEv(), "temporarily banned"),
        ("on_stream_replaced", StreamReplacedEv(), "another copy"),
        ("on_connect_failure", ConnectFailureEv(Message="bad request"), "refused the connection"),
    ],
)
async def test_unrecoverable_problems_stop_instead_of_idling(setup, handler, event, expected):
    # After these, WhatsApp's library stays disconnected for good. The process
    # must stop and say why, not sit there looking alive.
    await connect(setup)
    await getattr(setup.bridge, handler)(setup.client, event)
    state = setup.store.get_whatsapp_state()
    assert state["last_status"] == "error" and expected in state["detail"]
    assert setup.client.stopped and setup.bridge.failed


async def test_dashboard_sees_the_process_is_gone_as_soon_as_it_exits(setup):
    setup.client.idle_error = RuntimeError("dial tcp: no route to host")
    await setup.bridge.run(asyncio.Event())
    state = setup.store.get_whatsapp_state()
    # Not "running" for another half minute on the strength of the last heartbeat.
    assert state["running"] is False
    assert state["status"] == "stopped" and state["last_status"] == "error"
    assert "no route to host" in state["detail"]


async def test_logout_ends_the_run_and_keeps_the_reason(setup):
    task = asyncio.create_task(setup.bridge.run(asyncio.Event()))
    await asyncio.sleep(0.05)
    await setup.bridge.on_logged_out(setup.client, LoggedOutEv())
    await asyncio.wait_for(task, 5)
    state = setup.store.get_whatsapp_state()
    assert state["running"] is False and state["last_status"] == "logged_out"
    assert "link it again" in state["detail"]
    assert not setup.bridge.failed  # being logged out is not a crash


async def test_model_gets_honest_instructions_for_the_no_search_retry(setup):
    await connect(setup)
    await setup.bridge.handle_message(group_event("@oscar what's the score?"))
    (call,) = setup.asked
    assert "You can search the web" in call["system_prompt"]
    assert "cannot browse the web" in call["system_prompt_without_search"]
    assert "You can search the web" not in call["system_prompt_without_search"]


async def test_message_typed_on_oscars_phone_counts_as_oscar_speaking(setup):
    await connect(setup)
    own = group_event("give me five minutes", sender=OSCAR_LID, sender_alt=OSCAR_PHONE,
                      name="", message_id="OWN")
    own.Info.MessageSource.IsFromMe = True
    await setup.bridge.handle_message(own)
    assert setup.client.replies == []  # Oscar never answers itself
    await setup.bridge.handle_message(group_event("@oscar what did you say?", message_id="Q"))
    assert "Oscar (you): give me five minutes" in setup.asked[0]["request_text"]


@pytest.mark.parametrize("setup", [""], indirect=True)
async def test_without_a_firecrawl_key_oscar_does_not_claim_it_can_search(setup):
    await connect(setup)
    await setup.bridge.handle_message(group_event("@oscar what's the score?"))
    (call,) = setup.asked
    assert call["web_search"] is False  # the owner's setting is on, but there is nothing to search with
    assert "cannot browse the web" in call["system_prompt"]
    assert "You can search the web" not in call["system_prompt"]


async def test_a_failed_web_search_is_shown_on_the_dashboard_though_oscar_replied(setup):
    await connect(setup)
    setup.answers.append(("I couldn't search just now.", "The Firecrawl account is out of credits."))
    await setup.bridge.handle_message(group_event("@oscar what's the score?"))
    assert setup.client.replies[0][0] == "I couldn't search just now."
    (activity,) = setup.store.list_whatsapp_activity()
    assert activity["reply"] == "I couldn't search just now."
    assert activity["error"] == "Web search failed: The Firecrawl account is out of credits."


# Fixes from the second review


async def test_a_question_waiting_for_its_photo_is_not_overtaken_by_a_later_one(setup, monkeypatch):
    await connect(setup)

    async def slow_download(message):
        await asyncio.sleep(0.05)
        return b"\x89PNG fake image"

    async def answer(**kwargs):
        setup.asked.append(kwargs)
        return f"Answer {len(setup.asked)}"

    setup.client.download_any = slow_download
    monkeypatch.setattr(bridge_module.brain, "generate_reply", answer)
    # neonize hands each message over as a task of its own, in the order they arrived.
    await asyncio.gather(
        setup.bridge.handle_message(group_event("@oscar is this ripe?", image=True, message_id="Q1")),
        setup.bridge.handle_message(group_event("@oscar how long to boil an egg?", name="Roman", message_id="Q2")),
    )
    assert [quoted.Info.ID for _, quoted in setup.client.replies] == ["Q1", "Q2"]
    first, second = (call["request_text"] for call in setup.asked)
    assert "boil an egg" not in first.split("The message to answer:")[0]  # it came later
    assert "Jay: [photo] @oscar is this ripe?" in second and "Oscar (you): Answer 1" in second


async def test_a_message_whatsapp_delivers_twice_is_answered_once(setup):
    await connect(setup)
    event = group_event("@oscar hi", message_id="TWICE")
    await setup.bridge.handle_message(event)
    await setup.bridge.handle_message(event)
    assert len(setup.client.replies) == 1 and len(setup.asked) == 1
    await setup.bridge.handle_message(group_event("@oscar and again?", message_id="NEXT"))
    assert setup.asked[1]["request_text"].count("@oscar hi") == 1


async def test_a_journal_file_gone_by_the_time_it_is_protected_changes_nothing(setup, monkeypatch):
    # The library's database keeps a journal beside it for each write, so one
    # listed a moment ago may be gone already.
    setup.bridge.session_path.write_bytes(b"")
    journal = setup.bridge.session_path.with_name(setup.bridge.session_path.name + "-journal")
    journal.write_bytes(b"")
    chmod = bridge_module.os.chmod

    def journal_gone(path, mode):
        if str(path) == str(journal):
            raise FileNotFoundError(2, "No such file or directory", str(path))
        chmod(path, mode)

    monkeypatch.setattr(bridge_module.os, "chmod", journal_gone)
    code_lifetimes(monkeypatch, first=60, later=20)
    await setup.client.event.qr_handler(setup.client, b"2@code")  # as neonize calls it
    assert setup.bridge._qr_expiry is not None  # the next code will still be asked for
    await setup.client.event.handlers[ConnectedEv](setup.client, ConnectedEv())
    assert setup.client.presence_sent == ["AVAILABLE"]
    assert setup.bridge.session_path.stat().st_mode & 0o777 == 0o600


async def test_a_private_chat_the_owner_asks_in_is_named_after_the_other_person(own):
    own.store.save_whatsapp_settings(WhatsAppSettings(own_number=True, reply_in_private_chats=True))
    await connect(own)
    dm = dict(chat=JAY_PHONE, chat_server="s.whatsapp.net", is_group=False)
    await own.bridge.handle_message(group_event("are you free tonight?", message_id="J1", **dm))
    owners = from_the_owner(group_event("@oscar what is on tonight?", message_id="O1", **dm))
    owners.Info.Pushname = "Shrey"  # the account's own name, which is not who the chat is with
    await own.bridge.handle_message(owners)
    assert "a private WhatsApp chat with Jay" in own.asked[0]["system_prompt"]
    assert own.store.list_whatsapp_activity()[0]["chat_name"] == "Jay"


# Voice notes


def voice_message(seconds: int = 6) -> Message:
    """A voice note as WhatsApp records one: Opus audio in an Ogg file."""
    message = Message()
    note = message.audioMessage
    note.mimetype = "audio/ogg; codecs=opus"
    note.PTT = True
    note.seconds = seconds
    note.fileLength = 4000 * seconds
    return message


def voice_note(seconds: int = 6, *, quoted: Message | None = None, quoted_from: str = "", **options) -> MessageEv:
    event = group_event("", **options)
    event.Message.Clear()
    event.Message.CopyFrom(voice_message(seconds))
    if quoted is not None:
        context = event.Message.audioMessage.contextInfo
        context.quotedMessage.CopyFrom(quoted)
        context.participant = quoted_from
    return event


DM = dict(chat=JAY_PHONE, chat_server="s.whatsapp.net", is_group=False)
HEARD = "what time do you open tomorrow?"


@pytest.fixture
def transcriber(monkeypatch):
    """OpenRouter's transcription endpoint, faked: what it was sent, and what it answers."""
    fake = dataclasses.make_dataclass("Transcriber", ["sent", "status", "body"])([], 200, {"text": HEARD})

    def answer(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/audio/transcriptions")
        fake.sent.append(json.loads(request.content))
        return httpx.Response(fake.status, json=fake.body)

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **options: real(transport=httpx.MockTransport(answer), **options))
    return fake


async def test_a_voice_note_sent_to_oscar_is_written_down_and_answered(setup, transcriber):
    setup.store.save_whatsapp_settings(WhatsAppSettings(reply_in_private_chats=True))
    await connect(setup)
    await setup.bridge.handle_message(voice_note(message_id="V1", **DM))

    (sent,) = transcriber.sent
    assert (sent["model"], sent["language"], sent["input_audio"]["format"]) == ("deepgram/nova-3", "multi", "ogg")
    assert base64.b64decode(sent["input_audio"]["data"]) == b"\x89PNG fake image"  # what was downloaded
    (call,) = setup.asked
    assert call["request_text"].endswith(f"Jay: [voice message] {HEARD}")  # as if it had been typed
    assert len(setup.client.replies) == 1
    (activity,) = setup.store.list_whatsapp_activity()
    assert activity["request"] == f"[voice message] {HEARD}" and activity["error"] == ""

    # Remembered by its words, and not listened to again.
    await setup.bridge.handle_message(group_event("and on Sundays?", message_id="T2", **DM))
    assert f"Jay: [voice message] {HEARD}" in setup.asked[1]["request_text"]
    assert len(transcriber.sent) == 1


async def test_only_a_voice_note_meant_for_oscar_is_listened_to(setup, transcriber):
    await connect(setup)
    await setup.bridge.handle_message(voice_note(message_id="V1"))  # to the group, not to Oscar
    assert transcriber.sent == [] and setup.client.downloads == [] and setup.asked == []

    # A voice note that replies to one of Oscar's messages is meant for it.
    await setup.bridge.handle_message(
        voice_note(quoted=text_message("We open at 9."), quoted_from=f"{OSCAR_LID}@lid", message_id="V2")
    )
    assert len(transcriber.sent) == 1
    (call,) = setup.asked
    assert "Jay: [voice message]\n" in call["request_text"]  # the first one, never listened to
    assert call["request_text"].endswith(f'replying to you: "We open at 9.": [voice message] {HEARD}')


async def test_a_voice_note_a_question_replies_to_is_written_down(setup, transcriber):
    await connect(setup)
    transcriber.body = {"text": "I'll be there at eight"}
    await setup.bridge.handle_message(
        group_event("@oscar what did she say?", quoted=voice_message(), quoted_from=f"{ROMAN_PHONE}@s.whatsapp.net")
    )
    assert len(transcriber.sent) == 1
    assert "\"[voice message] I'll be there at eight\": @oscar what did she say?" in setup.asked[0]["request_text"]


async def test_a_voice_note_over_two_minutes_is_not_listened_to(setup, transcriber):
    setup.store.save_whatsapp_settings(WhatsAppSettings(reply_in_private_chats=True))
    await connect(setup)
    await setup.bridge.handle_message(voice_note(seconds=150, **DM))
    assert transcriber.sent == [] and setup.client.downloads == [] and setup.asked == []
    assert setup.client.replies[0][0] == bridge_module.VOICE_TOO_LONG_REPLY
    (activity,) = setup.store.list_whatsapp_activity()
    assert activity["request"] == "[voice message]" and "2:30 long" in activity["error"]


@pytest.mark.parametrize(
    ("status", "body", "why"),
    [
        (402, {"error": {"message": "Insufficient credits"}}, "out of credits"),
        (200, {"text": "  "}, "No words could be made out"),
    ],
)
async def test_a_voice_note_that_cannot_be_made_out_is_asked_for_in_writing(setup, transcriber, status, body, why):
    setup.store.save_whatsapp_settings(WhatsAppSettings(reply_in_private_chats=True))
    transcriber.status, transcriber.body = status, body
    await connect(setup)
    await setup.bridge.handle_message(voice_note(**DM))
    assert setup.asked == []  # nothing for the model to guess from
    assert setup.client.replies[0][0] == bridge_module.VOICE_UNCLEAR_REPLY
    (activity,) = setup.store.list_whatsapp_activity()
    assert why in activity["error"]
