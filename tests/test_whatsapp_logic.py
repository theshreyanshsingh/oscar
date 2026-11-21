"""When Oscar answers in WhatsApp, what it reads, and how it asks the model."""

import json
import re
from datetime import datetime

import httpx
import pytest

from app.whatsapp import brain
from app.whatsapp.chat import (
    GLOBAL_RATE_LIMIT_REPLIES,
    MAX_REPLY_DELAY_SECONDS,
    RATE_LIMIT_REPLIES,
    ChatMemory,
    IncomingMessage,
    build_request_text,
    build_system_prompt,
    clean_reply,
    is_addressed_to_oscar,
    is_too_old_to_answer,
    mentions_by_name,
)

OSCAR_PHONE = "15005550006"
OSCAR_LID = "201234567890123"
MY_IDS = frozenset({OSCAR_PHONE, OSCAR_LID})
GROUP = "120363000000000001@g.us"
NOW = 1_800_000_000.0


def msg(text="", *, sender="Jay", sender_id="13055550100", t=NOW, **extra) -> IncomingMessage:
    fields = dict(
        chat_id=GROUP,
        is_group=True,
        message_id=f"id-{t}-{text[:5]}",
        sender_ids=frozenset({sender_id}),
        sender_name=sender,
        timestamp=t,
        text=text,
    )
    fields.update(extra)
    return IncomingMessage(**fields)


def addressed(message, *, private=False, name="Oscar"):
    return is_addressed_to_oscar(
        message, my_ids=MY_IDS, agent_name=name, reply_in_private_chats=private
    )


# When Oscar answers


@pytest.mark.parametrize(
    "text",
    [
        "@oscar can you get this",
        "@Oscar what are your thoughts on this?",
        "hey @OSCAR",
        "thoughts @oscar?",
        "(@oscar) help",
        "@oscar, quick one",
    ],
)
def test_typed_at_oscar_counts(text):
    assert addressed(msg(text))


@pytest.mark.parametrize(
    "text",
    [
        "oscar what do you think",  # no @
        "email me at jay@oscar.com",
        "@oscars are next week",
        "@oscarbot hi",
        "I told Oscar about it",
        "",
    ],
)
def test_other_text_does_not(text):
    assert not addressed(msg(text))


def test_real_mention_by_phone_or_lid_counts():
    # Picking Oscar in WhatsApp's @ list puts its id in the mentions, and "@<number>" in the text.
    assert addressed(msg(f"@{OSCAR_PHONE} thoughts?", mentioned_ids=frozenset({OSCAR_PHONE})))
    assert addressed(msg(f"@{OSCAR_LID} thoughts?", mentioned_ids=frozenset({OSCAR_LID})))
    assert not addressed(msg("@13055550199 hi", mentioned_ids=frozenset({"13055550199"})))


def test_replying_to_oscar_counts():
    assert addressed(msg("why?", quoted_sender_id=OSCAR_LID, quoted_kind="text", quoted_text="Maybe"))
    assert not addressed(msg("why?", quoted_sender_id="13055550199", quoted_kind="text"))


def test_oscar_never_answers_itself():
    assert not addressed(msg("@oscar hi", from_me=True))


def test_private_chats_follow_the_setting():
    private = msg("hello", is_group=False, chat_id="13055550100@s.whatsapp.net")
    assert not addressed(private, private=False)
    assert addressed(private, private=True)


def test_handle_is_first_word_of_the_agent_name():
    assert mentions_by_name("@ava can you help", "Ava Assistant")
    assert addressed(msg("@ava hi"), name="Ava Assistant")
    assert not addressed(msg("@oscar hi"), name="Ava Assistant")


def test_old_mentions_are_not_answered():
    assert not is_too_old_to_answer(msg(t=NOW - 60), now=NOW)
    assert is_too_old_to_answer(msg(t=NOW - MAX_REPLY_DELAY_SECONDS - 1), now=NOW)


def test_rate_limit_per_chat():
    memory = ChatMemory()
    for i in range(RATE_LIMIT_REPLIES):
        assert memory.allow_reply(GROUP, now=NOW + i)
    assert not memory.allow_reply(GROUP, now=NOW + RATE_LIMIT_REPLIES)
    assert memory.allow_reply("other@g.us", now=NOW)  # other chats are unaffected
    assert memory.allow_reply(GROUP, now=NOW + 11 * 60)  # the window moves on


# What Oscar reads


def test_request_text_has_context_quote_and_readable_mentions():
    memory = ChatMemory()
    memory.remember(msg("anyone know a good ramen place near Union Square?", sender="Roman",
                        sender_id="15705550111", t=NOW - 300))
    memory.remember(msg("", sender="Roman", sender_id="15705550111", t=NOW - 200, kind="image"))
    memory.remember_reply(GROUP, "Oscar", "Try Ippudo on 4th Ave.", NOW - 100)
    question = msg(
        f"@{OSCAR_PHONE} can you get this",
        mentioned_ids=frozenset({OSCAR_PHONE}),
        quoted_sender_id="15705550111",
        quoted_kind="text",
        quoted_text="what time do they close tonight?",
    )
    text = build_request_text(question, memory=memory, my_ids=MY_IDS, agent_name="Oscar", now=NOW)

    lines = text.splitlines()
    assert lines[0] == "Recent messages, oldest first:"
    assert "Roman: anyone know a good ramen place near Union Square?" in lines[1]
    assert "Roman: [photo]" in lines[2]
    assert "Oscar (you): Try Ippudo on 4th Ave." in lines[3]
    assert lines[-2] == "The message to answer:"
    assert 'Jay, replying to Roman: "what time do they close tonight?": @Oscar can you get this' in lines[-1]
    assert OSCAR_PHONE not in text  # the number became @Oscar


def test_request_text_skips_stale_history_and_the_message_itself():
    memory = ChatMemory()
    memory.remember(msg("from yesterday", t=NOW - 7 * 3600))
    question = msg("@oscar hi")
    memory.remember(question)  # already remembered must not appear twice
    text = build_request_text(question, memory=memory, my_ids=MY_IDS, agent_name="Oscar", now=NOW)
    assert "from yesterday" not in text
    assert text.count("@oscar hi") == 1
    assert text.startswith("The message to answer:")


def test_what_arrived_while_a_question_waited_is_left_out_but_oscars_answers_are_not():
    memory = ChatMemory()
    memory.remember(msg("@oscar best ramen nearby?", t=NOW - 20))
    question = msg("@oscar and is it open late?", t=NOW - 10)
    memory.remember(question)
    memory.remember(msg("I'm in, see you there", sender="Roman", sender_id="15705550111", t=NOW - 5))
    memory.remember_reply(GROUP, "Oscar", "Try Ippudo on 4th Ave.", NOW - 2)
    text = build_request_text(question, memory=memory, my_ids=MY_IDS, agent_name="Oscar", now=NOW)
    assert "Jay: @oscar best ramen nearby?" in text
    assert "Oscar (you): Try Ippudo on 4th Ave." in text  # what Oscar has said already
    assert "see you there" not in text  # said after the question


def test_replying_to_a_photo_is_described():
    question = msg("@oscar thoughts on this?", quoted_sender_id="15705550111", quoted_kind="image",
                   quoted_text="new logo draft")
    text = build_request_text(question, memory=ChatMemory(), my_ids=MY_IDS, agent_name="Oscar", now=NOW)
    assert 'replying to someone: "[photo] new logo draft"' in text


def test_system_prompt():
    prompt = build_system_prompt(
        agent_name="Oscar",
        chat_label="Weekend plans",
        is_group=True,
        instructions="Talk like a pirate.",
        web_search=True,
        now=datetime(2026, 11, 10, 20, 9).astimezone(),
    )
    assert 'member of the WhatsApp group "Weekend plans"' in prompt
    assert "writing @Oscar" in prompt
    assert "Tuesday 10 November 2026, 20:09" in prompt
    assert "You can search the web" in prompt
    assert prompt.endswith("Talk like a pirate.")

    no_search = build_system_prompt(
        agent_name="Oscar", chat_label="Jay", is_group=False, instructions="",
        web_search=False, now=datetime(2026, 11, 10, 20, 9).astimezone(),
    )
    assert "private WhatsApp chat with Jay" in no_search
    assert "cannot browse the web" in no_search
    assert "Extra instructions" not in no_search


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("Oscar: Sure thing!", "Sure thing!"),
        ("*Oscar*: Sure thing!", "Sure thing!"),
        ("This is **important**.", "This is *important*."),
        ("## Plan\n- one", "*Plan*\n- one"),
        ("See [the menu](https://example.com/menu).", "See the menu (https://example.com/menu)."),
        ("a\n\n\n\nb", "a\n\nb"),
    ],
)
def test_clean_reply(raw, clean):
    assert clean_reply(raw, "Oscar") == clean


def test_clean_reply_caps_length():
    assert len(clean_reply("x" * 10_000, "Oscar")) == 3500


@pytest.mark.parametrize(
    "raw", ["Oscar (you): Sure.", "[20:15] Oscar (you): Sure.", "[9:05] Oscar: Sure.", "oscar(you): Sure."]
)
def test_clean_reply_drops_copied_transcript_labels(raw):
    assert clean_reply(raw, "Oscar") == "Sure."


def test_overall_rate_limit_covers_many_groups():
    memory = ChatMemory()
    allowed = sum(memory.allow_reply(f"group-{i}@g.us", now=NOW + i) for i in range(200))
    assert allowed == GLOBAL_RATE_LIMIT_REPLIES
    assert memory.allow_reply("fresh@g.us", now=NOW + 3601 + 200)  # an hour later


def test_names_are_kept_per_chat():
    memory = ChatMemory()
    memory.remember(msg("hi", sender="Secret Sam", sender_id="15550001111"))
    other = msg("@15550001111 hey", chat_id="other@g.us", mentioned_ids=frozenset({"15550001111"}))
    text = build_request_text(other, memory=memory, my_ids=MY_IDS, agent_name="Oscar", now=NOW)
    assert "Secret Sam" not in text  # learned in another chat, so not used here


def test_forget_removes_a_deleted_message():
    memory = ChatMemory()
    memory.remember(msg("keep me", message_id="A"))
    memory.remember(msg("delete me", message_id="B"))
    memory.forget(GROUP, "B")
    assert [entry.text for entry in memory.recent(GROUP, now=NOW)] == ["keep me"]


# Asking the model


FIRECRAWL = "https://firecrawl.test/v2"
PAGES = {
    "web": [
        {"title": "Ippudo", "url": "https://ippudo.example/hours", "description": "Open **11am to 10pm** daily."}
    ]
}


def found(data: dict = PAGES) -> httpx.Response:
    return httpx.Response(200, json={"success": True, "data": data, "creditsUsed": 2})


def fake_openrouter(
    responses: list[httpx.Response],
    seen: list[dict],
    *,
    searches: list[dict] | None = None,
    firecrawl=found,
) -> httpx.AsyncClient:
    """Answers OpenRouter requests with `responses`, in order, and Firecrawl searches with `firecrawl()`.

    What was asked of each is recorded in `seen` and `searches`.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        asked = {"json": json.loads(request.content), "auth": request.headers["authorization"]}
        if request.url.host == "firecrawl.test":
            assert str(request.url) == f"{FIRECRAWL}/search"
            if searches is not None:
                searches.append(asked)
            return firecrawl()
        seen.append(asked)
        return responses.pop(0)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def answer(text: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


def wants_to_search(*queries: str, **extra) -> httpx.Response:
    """The model asking for searches instead of answering, as Claude was seen to through OpenRouter."""
    calls = [
        {
            "id": f"toolu_{number}",
            "type": "function",
            "function": {"name": "web_search", "arguments": json.dumps({"query": query})},
        }
        for number, query in enumerate(queries, 1)
    ]
    message = {"role": "assistant", "content": None, "refusal": None, "tool_calls": calls, **extra}
    return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": "tool_calls"}]})


async def ask(client, **overrides) -> str:
    args = dict(
        api_key="sk-or-test",
        model="anthropic/claude-haiku-5.5",
        system_prompt="You are Oscar.",
        request_text="The message to answer:\n[20:09] Jay: @oscar hi",
        images=[],
        web_search=True,
        firecrawl_api_key="fc-test",
        firecrawl_base_url=FIRECRAWL,
        client=client,
    )
    args.update(overrides)
    return await brain.generate_reply(**args)


async def test_request_shape_with_web_search_and_photo():
    seen: list[dict] = []
    client = fake_openrouter([answer("Looks great!")], seen)
    reply = await ask(client, images=[brain.Image(data=b"\xff\xd8jpeg", mime_type="image/jpeg")])
    assert reply == "Looks great!"

    (request,) = seen
    assert request["auth"] == "Bearer sk-or-test"
    body = request["json"]
    assert body["model"] == "anthropic/claude-haiku-5.5"
    # Searching is a tool the model may call; Oscar runs it through Firecrawl.
    assert body["tools"] == [brain.WEB_SEARCH_TOOL]
    assert "plugins" not in body and "tool_choice" not in body
    system, user = body["messages"]
    assert system == {"role": "system", "content": "You are Oscar."}
    assert user["content"][0] == {"type": "text", "text": "The message to answer:\n[20:09] Jay: @oscar hi"}
    assert user["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


async def test_no_search_when_web_search_is_off():
    seen: list[dict] = []
    await ask(fake_openrouter([answer("Hi!")], seen), web_search=False)
    assert "tools" not in seen[0]["json"]
    assert seen[0]["json"]["messages"][1]["content"] == "The message to answer:\n[20:09] Jay: @oscar hi"


async def test_retries_without_web_search_and_stops_claiming_it_can_search():
    seen: list[dict] = []
    refused = httpx.Response(400, json={"error": {"message": "web search not supported"}})
    reply = await ask(
        fake_openrouter([refused, answer("Hi!")], seen),
        system_prompt="You can search the web.",
        system_prompt_without_search="You cannot browse the web.",
    )
    assert reply == "Hi!"
    first, second = (request["json"] for request in seen)
    assert "tools" in first and "tools" not in second
    assert first["messages"][0]["content"] == "You can search the web."
    # The retry must not tell the model it can search when it no longer can.
    assert second["messages"][0]["content"] == "You cannot browse the web."


async def test_model_that_cannot_see_photos_still_answers():
    seen: list[dict] = []
    refused = httpx.Response(404, json={"error": {"message": "No endpoints found that support image input"}})
    photo = brain.Image(data=b"\xff\xd8jpeg", mime_type="image/jpeg")
    reply = await ask(fake_openrouter([refused, refused, answer("I can't see photos.")], seen), images=[photo])
    assert reply == "I can't see photos."
    with_search, without_search, without_photo = (request["json"] for request in seen)
    assert "tools" in with_search and isinstance(with_search["messages"][1]["content"], list)
    assert "tools" not in without_search and isinstance(without_search["messages"][1]["content"], list)
    # Last try: text only, and the model is told a photo was there.
    text = without_photo["messages"][1]["content"]
    assert isinstance(text, str) and text.endswith(brain.NO_IMAGES_NOTE)


async def test_gives_up_after_the_plainest_request_fails():
    seen: list[dict] = []
    unknown = httpx.Response(404, json={"error": {"message": "No such model"}})
    with pytest.raises(brain.BrainError, match="does not know that model"):
        await ask(fake_openrouter([unknown, unknown], seen))
    assert len(seen) == 2  # with search, then without; nothing more to drop


async def test_other_errors_are_not_retried():
    seen: list[dict] = []
    with pytest.raises(brain.BrainError, match="out of credits"):
        await ask(fake_openrouter([httpx.Response(402, json={})], seen))
    assert len(seen) == 1


async def test_error_inside_a_200_response_is_reported():
    body = {"error": {"message": "Provider returned error: overloaded", "code": 529}}
    with pytest.raises(brain.BrainError, match="provider reported an error. Provider returned error: overloaded"):
        await ask(fake_openrouter([httpx.Response(200, json=body)], []), web_search=False)


async def test_content_parts_are_joined():
    parts = httpx.Response(200, json={"choices": [{"message": {"content": [
        {"type": "text", "text": "Hello "}, {"type": "text", "text": "there"}]}}]})
    assert await ask(fake_openrouter([parts], [])) == "Hello there"


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(401, json={"error": {"message": "No auth"}}), "rejected the API key"),
        (httpx.Response(402, json={}), "out of credits"),
        (httpx.Response(404, json={}), "does not know that model"),
        (httpx.Response(429, json={}), "rate limiting"),
        (httpx.Response(500, text="oops"), "returned an error (500)"),
        (httpx.Response(200, json={"choices": []}), "empty answer"),
        (httpx.Response(200, json={"choices": [{"message": {"content": "  "}}]}), "empty answer"),
        (httpx.Response(200, text="<html>gateway</html>"), "not JSON"),
        (httpx.Response(200, json=["unexpected"]), "empty answer"),
        (httpx.Response(503, json={"error": "upstream down"}), "upstream down"),
        (httpx.Response(502, json=["odd"]), "returned an error (502)"),
    ],
)
async def test_failures_are_explained(response, message):
    with pytest.raises(brain.BrainError, match=re.escape(message)):
        await ask(fake_openrouter([response], []), web_search=False)


async def test_missing_key_is_explained():
    with pytest.raises(brain.BrainError, match="OPENROUTER_API_KEY"):
        await ask(None, api_key="")


async def test_network_problems_are_explained():
    def handler(request):
        raise httpx.ConnectError("no route to host")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(brain.BrainError, match="Could not reach OpenRouter"):
        await ask(client, web_search=False)


def test_messages_typed_on_oscars_phone_are_labelled_as_oscar():
    memory = ChatMemory()
    own = msg("back in five", sender="", sender_id=OSCAR_PHONE, from_me=True, message_id="OWN")
    memory.remember(own, speaker="Oscar")
    question = msg("@oscar where are you?", message_id="Q")
    text = build_request_text(question, memory=memory, my_ids=MY_IDS, agent_name="Oscar", now=NOW)
    assert "Oscar (you): back in five" in text
    assert memory.names_in(GROUP) == {}  # Oscar's own ids are not stored as a contact name


# Searching the web (through Firecrawl)


async def test_the_model_searches_and_then_answers():
    seen: list[dict] = []
    searches: list[dict] = []
    client = fake_openrouter(
        [wants_to_search("Ippudo closing time"), answer("They close at 10pm.")], seen, searches=searches
    )
    assert await ask(client) == "They close at 10pm."

    # The search went to Firecrawl with Oscar's own key.
    (search,) = searches
    assert search["auth"] == "Bearer fc-test"
    assert search["json"] == {
        "query": "Ippudo closing time",
        "limit": 5,
        "sources": ["web", "news"],
        "timeout": 10000,
    }
    # What it found went back to the model, after the model's own request for it.
    first, second = (request["json"] for request in seen)
    assert second["messages"][:2] == first["messages"]
    asked, result = second["messages"][2:]
    assert asked == {
        "role": "assistant",
        "tool_calls": [
            {
                "id": "toolu_1",
                "type": "function",
                "function": {"name": "web_search", "arguments": '{"query": "Ippudo closing time"}'},
            }
        ],
    }
    assert result == {
        "role": "tool",
        "tool_call_id": "toolu_1",
        "content": "1. Ippudo (https://ippudo.example/hours)\nOpen 11am to 10pm daily.",
    }
    assert second["tools"] == [brain.WEB_SEARCH_TOOL] and "tool_choice" not in second


async def test_reasoning_the_provider_wants_back_is_sent_back():
    seen: list[dict] = []
    details = [{"type": "reasoning.text", "text": "I should check."}]
    client = fake_openrouter(
        [wants_to_search("x", content="Let me check.", reasoning_details=details), answer("Done.")], seen
    )
    await ask(client)
    asked = seen[1]["json"]["messages"][2]
    assert asked["content"] == "Let me check." and asked["reasoning_details"] == details
    assert "refusal" not in asked  # nothing the provider did not ask for


async def test_several_searches_at_once_each_get_an_answer():
    seen: list[dict] = []
    searches: list[dict] = []
    client = fake_openrouter(
        [wants_to_search("a", "b", "c", "d"), answer("Here you go.")], seen, searches=searches
    )
    assert await ask(client) == "Here you go."
    assert sorted(s["json"]["query"] for s in searches) == ["a", "b", "c"]  # three at most
    results = seen[1]["json"]["messages"][3:]
    # Every request gets a result, or the provider rejects the conversation.
    assert [r["tool_call_id"] for r in results] == ["toolu_1", "toolu_2", "toolu_3", "toolu_4"]
    assert results[3]["content"] == "No search was run: that is too many searches at once."


async def test_a_search_that_finds_nothing_says_so():
    seen: list[dict] = []
    client = fake_openrouter(
        [wants_to_search("zxqv"), answer("Nothing on that.")], seen, firecrawl=lambda: found({"web": []})
    )
    await ask(client)
    assert seen[1]["json"]["messages"][3]["content"] == "The search found nothing."


async def test_a_failed_search_is_reported_and_the_model_still_answers():
    seen: list[dict] = []
    problems: list[str] = []
    refused = lambda: httpx.Response(401, json={"success": False, "error": "Unauthorized: Invalid token"})
    client = fake_openrouter(
        [wants_to_search("Ippudo closing time"), answer("I couldn't search just now.")], seen, firecrawl=refused
    )
    assert await ask(client, search_problems=problems) == "I couldn't search just now."
    # The owner gets the reason. The chat does not: the model is only told to say it could not search.
    assert problems == [
        "Firecrawl rejected the API key (check FIRECRAWL_API_KEY). Unauthorized: Invalid token"
    ]
    told = seen[1]["json"]["messages"][3]["content"]
    assert told == "The search could not be run just now. Say that you could not search, and do not guess."
    assert "Firecrawl" not in told and "key" not in told.lower()


@pytest.mark.parametrize(
    "function",
    [
        {"name": "web_search", "arguments": "not json"},
        {"name": "web_search", "arguments": '{"query": "   "}'},
        {"name": "web_search", "arguments": "[1, 2]"},
        {"name": "send_email", "arguments": '{"query": "x"}'},
        None,
    ],
)
async def test_a_request_that_is_not_a_usable_search_runs_nothing(function):
    seen: list[dict] = []
    searches: list[dict] = []
    message = {"role": "assistant", "content": None, "tool_calls": [{"id": "toolu_9", "type": "function", "function": function}]}
    asks = httpx.Response(200, json={"choices": [{"message": message}]})
    await ask(fake_openrouter([asks, answer("OK.")], seen, searches=searches))
    assert searches == []
    assert seen[1]["json"]["messages"][3] == {
        "role": "tool",
        "tool_call_id": "toolu_9",
        "content": "No search was run: the request gave nothing to search for.",
    }


async def test_a_model_that_keeps_searching_is_made_to_answer():
    seen: list[dict] = []
    searches: list[dict] = []
    responses = [wants_to_search(f"try {n}") for n in range(1, 4)] + [answer("Best I found.")]
    assert await ask(fake_openrouter(responses, seen, searches=searches)) == "Best I found."
    assert len(searches) == brain.MAX_SEARCH_ROUNDS == 3
    choices = [request["json"].get("tool_choice") for request in seen]
    assert choices == [None, None, None, "none"]  # after three rounds it may not search again


async def test_a_model_that_never_answers_is_an_empty_answer():
    responses = [wants_to_search(f"try {n}") for n in range(1, 5)]
    with pytest.raises(brain.BrainError, match="empty answer"):
        await ask(fake_openrouter(responses, []))


async def test_a_failure_after_searching_is_explained():
    out_of_credit = httpx.Response(402, json={"error": {"message": "Insufficient credits"}})
    with pytest.raises(brain.BrainError, match="out of credits"):
        await ask(fake_openrouter([wants_to_search("x"), out_of_credit], []))


async def test_without_a_firecrawl_key_there_is_no_search_and_the_model_knows():
    seen: list[dict] = []
    await ask(
        fake_openrouter([answer("Hi!")], seen),
        firecrawl_api_key="",
        system_prompt="You can search the web.",
        system_prompt_without_search="You cannot browse the web.",
    )
    (request,) = seen
    assert "tools" not in request["json"]
    assert request["json"]["messages"][0]["content"] == "You cannot browse the web."


async def test_a_model_that_cannot_use_tools_answers_without_searching():
    seen: list[dict] = []
    searches: list[dict] = []
    refused = httpx.Response(404, json={"error": {"message": "No endpoints found that support tool use"}})
    reply = await ask(
        fake_openrouter([refused, answer("From memory: 10pm.")], seen, searches=searches),
        system_prompt="You can search the web.",
        system_prompt_without_search="You cannot browse the web.",
    )
    assert reply == "From memory: 10pm." and searches == []
    assert "tools" not in seen[1]["json"]
    assert seen[1]["json"]["messages"][0]["content"] == "You cannot browse the web."


# Oscar on its owner's own number

OTHER_OSCAR = "209999999999999"  # another member of the group, who goes by "Oscar" on WhatsApp
DM = dict(is_group=False, chat_id="13055550100@s.whatsapp.net")


def called(message, *, private=False, members_called_oscar=frozenset()):
    return is_addressed_to_oscar(
        message,
        my_ids=MY_IDS,
        agent_name="Oscar",
        reply_in_private_chats=private,
        own_number=True,
        members_called_oscar=members_called_oscar,
    )


def test_on_the_owners_number_only_oscars_name_calls_oscar():
    assert called(msg("@oscar what time is it in Sydney?"))
    # A mention of the account, or a reply to one of its messages, is for the owner.
    assert not called(msg(f"@{OSCAR_LID} are you coming?", mentioned_ids=frozenset({OSCAR_LID})))
    assert not called(
        msg("sounds good", quoted_sender_id=OSCAR_PHONE, quoted_text="see you at 8", quoted_kind="text")
    )
    assert not called(msg("morning all"))


def test_the_owner_can_call_oscar_from_their_own_account():
    assert called(msg("@oscar summarise this", from_me=True))
    assert not called(msg("on my way", from_me=True))
    # With a number to itself, whatever that account says is Oscar speaking.
    assert not addressed(msg("@oscar summarise this", from_me=True))


def test_picking_a_member_called_oscar_from_the_list_calls_oscar():
    picked = msg(f"@{OTHER_OSCAR} hello", mentioned_ids=frozenset({OTHER_OSCAR}))
    assert called(picked, members_called_oscar=frozenset({OTHER_OSCAR}))
    assert not called(picked)  # the member mentioned goes by another name
    assert not addressed(picked)  # and with a number to itself, Oscar is only ever itself


def test_a_reply_to_one_of_oscars_answers_carries_on_with_oscar():
    follow_up = msg(
        "and tomorrow?",
        quoted_sender_id=OSCAR_LID,
        quoted_text="*Oscar:* It is 21 degrees.",
        quoted_kind="text",
    )
    assert called(follow_up)
    # The same words quoted from somebody else are not Oscar's answer.
    assert not called(
        msg("and tomorrow?", quoted_sender_id="13055550177", quoted_text="*Oscar:* hi", quoted_kind="text")
    )


@pytest.mark.parametrize("text", ["*Oscar:* Call me with @oscar.", "  *oscar:* @oscar again"])
def test_oscar_never_answers_its_own_answers(text):
    assert not called(msg(text, from_me=True))
    assert not called(msg(text))


def test_private_chats_on_the_owners_number_need_the_setting_and_the_name():
    assert not called(msg("@oscar hi", **DM))
    assert called(msg("@oscar hi", **DM), private=True)
    assert not called(msg("are you free tonight?", **DM), private=True)  # that one is for the owner
    # With a number to itself, Oscar answers every private message once that is switched on.
    assert addressed(msg("are you free tonight?", **DM), private=True)


def test_on_the_owners_number_the_transcript_keeps_owner_and_oscar_apart():
    memory = ChatMemory()
    memory.remember(msg("anyone seen the keys?", sender="Jay"))
    memory.remember(msg("on the shelf", sender="", from_me=True, t=NOW + 1), speaker="Shrey", from_oscar=False)
    memory.remember_reply(GROUP, "Oscar", "It is 21 degrees in Sydney.", NOW + 2)
    ask = msg(
        f"@{OTHER_OSCAR} and @{OSCAR_LID} what about tomorrow?",
        sender="Roman",
        sender_id="13055550199",
        t=NOW + 3,
        mentioned_ids=frozenset({OTHER_OSCAR, OSCAR_LID}),
        quoted_sender_id=OSCAR_LID,
        quoted_text="*Oscar:* It is 21 degrees in Sydney.",
        quoted_kind="text",
    )
    text = build_request_text(
        ask,
        memory=memory,
        my_ids=MY_IDS,
        agent_name="Oscar",
        now=NOW + 3,
        own_number=True,
        owner_name="Shrey",
        members_called_oscar=frozenset({OTHER_OSCAR}),
    )
    assert "] Shrey: on the shelf" in text and "Shrey (you)" not in text
    assert "] Oscar (you): It is 21 degrees in Sydney." in text
    assert 'Roman, replying to you: "It is 21 degrees in Sydney."' in text
    assert text.endswith("@Oscar and @Shrey what about tomorrow?")


def test_on_the_owners_number_a_quoted_message_of_theirs_is_shown_as_theirs():
    ask = msg(
        "@oscar is that right?",
        quoted_sender_id=OSCAR_PHONE,
        quoted_text="the meeting is at 8",
        quoted_kind="text",
    )
    own = build_request_text(
        ask, memory=ChatMemory(), my_ids=MY_IDS, agent_name="Oscar", now=NOW, own_number=True, owner_name="Shrey"
    )
    assert 'replying to Shrey: "the meeting is at 8"' in own
    alone = build_request_text(ask, memory=ChatMemory(), my_ids=MY_IDS, agent_name="Oscar", now=NOW)
    assert 'replying to you: "the meeting is at 8"' in alone


@pytest.mark.parametrize(("owner_name", "shown"), [("Shrey", "Shrey"), ("", "The account's owner")])
def test_the_owners_own_question_is_shown_under_their_name(owner_name, shown):
    ask = msg("@oscar what is 2+2", sender="", from_me=True)
    text = build_request_text(
        ask, memory=ChatMemory(), my_ids=MY_IDS, agent_name="Oscar", now=NOW, own_number=True, owner_name=owner_name
    )
    assert text.endswith(f"] {shown}: @oscar what is 2+2")


def test_what_the_account_says_is_oscars_unless_told_otherwise():
    memory = ChatMemory()
    memory.remember(msg("typed on Oscar's phone", from_me=True), speaker="Oscar")
    memory.remember(msg("typed by the owner", from_me=True, t=NOW + 1), speaker="Shrey", from_oscar=False)
    assert [entry.from_oscar for entry in memory.recent(GROUP, now=NOW + 1)] == [True, False]


def test_on_the_owners_number_the_model_is_told_whose_account_it_is():
    common = dict(
        agent_name="Oscar", chat_label="Weekend plans", is_group=True, instructions="",
        web_search=False, now=datetime(2026, 11, 12, 1, 30).astimezone(),
    )
    own = build_system_prompt(**common, own_number=True, owner_name="Shrey")
    assert "an AI assistant that Shrey runs from their own WhatsApp account" in own
    assert "You are not Shrey" in own and "sent from Shrey's number" in own
    assert "a member of" not in own and "writing @Oscar" in own
    unnamed = build_system_prompt(**common, own_number=True)
    assert "that your owner runs" in unnamed
    alone = build_system_prompt(**common, owner_name="Shrey")
    assert "a member of" in alone and "Shrey" not in alone


def test_a_label_the_model_copies_from_the_chat_is_dropped_whole():
    assert clean_reply("*Oscar:* Sure thing.", "Oscar") == "Sure thing."
    assert clean_reply("*Oscar*: Sure thing.", "Oscar") == "Sure thing."


# What Oscar has been told about who it answers for


def test_the_fact_sheet_reaches_the_chat_model_with_the_rule_to_stick_to_it():
    common = dict(
        agent_name="Oscar", chat_label="Weekend plans", is_group=True, instructions="Talk like a pirate.",
        web_search=True, now=datetime(2026, 11, 12, 1, 30).astimezone(),
    )
    told = build_system_prompt(**common, knowledge="  A men's haircut is $45.\nClosed on Sundays.  ")
    assert '"""\nA men\'s haircut is $45.\nClosed on Sundays.\n"""' in told
    assert "only from these facts" in told and "do not search the web for it" in told
    # The owner's extra instructions still come last.
    assert told.index("Closed on Sundays.") < told.index("Talk like a pirate.")
    untold = build_system_prompt(**common)
    assert "who you answer for" not in untold and "only from these facts" not in untold
