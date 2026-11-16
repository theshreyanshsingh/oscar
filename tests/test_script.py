"""Keeping Oscar on its script (app/script.py). OpenRouter is faked."""

import json

import httpx
import pytest

from app.script import (
    KEEPER_INSTRUCTIONS,
    KEEPER_MODEL,
    LAST_STEP_NOTE,
    STEP_NOTE,
    ScriptKeeper,
    ScriptLost,
    parse_place,
    script_steps,
    step_note,
    talk_text,
)

OPENROUTER = "https://openrouter.test/api/v1"
STEPS = ["Ask who is calling.", "Ask what they need help with.", "Thank them and say goodbye."]
TALK = [
    {"role": "assistant", "content": "Hi, this is Oscar. How can I help you today?"},
    {"role": "user", "content": "Hi, it's Daniel here."},
    {"role": "assistant", "content": "Hi Daniel, what do you need help with today?"},
]


def keeper_answering(text: str, status: int = 200, *, name: str = "Oscar"):
    """A keeper whose model answers `text`. Returns it and the requests it made."""
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request):
        requests.append(request)
        if status != 200:
            return httpx.Response(status, json={"error": {"message": text}})
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": text}}]})

    keeper = ScriptKeeper(
        STEPS,
        agent_name=name,
        api_key="sk-or-test",
        base_url=OPENROUTER,
        client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    return keeper, requests


# The steps the owner wrote


def test_a_script_is_its_lines_without_their_numbers_or_bullets():
    script = """
    1. Ask who is calling
    2) Ask what they need help with.

    Step 3: Ask which day suits them?
    - Say: "I'll pass that on."
    • Thank them and say goodbye!
       3 pm is the latest booking
    """
    assert script_steps(script) == [
        "Ask who is calling.",
        "Ask what they need help with.",
        "Ask which day suits them?",
        'Say: "I\'ll pass that on."',
        "Thank them and say goodbye!",
        "3 pm is the latest booking.",  # a line that starts with a number, not a numbered line
    ]


def test_no_script_is_no_steps():
    assert script_steps("") == []
    assert script_steps("  \n\n - \n 2. \n") == []


# What the speech model is told


def test_the_speech_model_is_told_the_step_it_is_on_and_the_one_after():
    assert step_note(STEPS, 0) == STEP_NOTE.format(step=STEPS[0], after=STEPS[1])
    assert step_note(STEPS, 0) == (
        "Your script. The next step to do: Ask who is calling. Only if what the caller has just "
        "said gives exactly that, skip to the one after it: Ask what they need help with. Do only "
        "one step, after briefly answering anything they asked."
    )
    assert step_note(STEPS, 1).count(STEPS[1]) == 1 and STEPS[2] in step_note(STEPS, 1)
    assert STEPS[0] not in step_note(STEPS, 1)  # what is done is not brought up again


def test_the_last_step_has_none_after_it_and_a_finished_script_has_no_note():
    assert step_note(STEPS, 2) == LAST_STEP_NOTE.format(step=STEPS[2])
    assert "Thank them and say goodbye." in step_note(STEPS, 2)
    assert step_note(STEPS, 3) == "" == step_note(STEPS, 9)
    assert step_note(["Say goodbye."], 0) == LAST_STEP_NOTE.format(step="Say goodbye.")


# Working out the place


@pytest.mark.parametrize(
    ("answer", "place"),
    [
        ("1", 0),
        ("2", 1),
        (" 3.\n", 2),
        ("Step 2", 1),
        ("DONE", 3),
        ("done.", 3),
        # Now and then the model writes its thinking first: the answer is the last line.
        ("Step 1 is finished: the caller said who they are.\n\nStep 2 was asked, not answered.\n\n2", 1),
        ("Every step has been said.\nDONE", 3),
    ],
)
def test_the_keepers_answer_is_a_place_in_the_script(answer, place):
    assert parse_place(answer, STEPS) == place


@pytest.mark.parametrize("answer", ["", None, "0", "4", "12", "two", "Step 2 is next", "2 or 3", "-1", 2])
def test_an_answer_that_is_not_a_step_is_not_a_place(answer):
    assert parse_place(answer, STEPS) is None


def test_the_call_is_shown_to_the_keeper_one_line_a_turn():
    assert talk_text(TALK, "Ava") == (
        "Ava: Hi, this is Oscar. How can I help you today?\n"
        "Caller: Hi, it's Daniel here.\n"
        "Ava: Hi Daniel, what do you need help with today?"
    )


async def test_the_place_is_asked_for_in_one_request_to_a_text_model():
    keeper, requests = keeper_answering("2")
    assert keeper.place == 0 and keeper.note() == step_note(STEPS, 0)

    assert await keeper.find_place(TALK) == 1

    assert keeper.place == 1 and keeper.note() == step_note(STEPS, 1)
    (request,) = requests
    assert str(request.url) == f"{OPENROUTER}/chat/completions"
    assert request.headers["authorization"] == "Bearer sk-or-test"
    body = json.loads(request.content)
    assert set(body) == {"model", "temperature", "max_tokens", "reasoning", "messages"}
    assert (body["model"], body["temperature"]) == (KEEPER_MODEL, 0)
    # A little thinking first: asked straight off, it named the wrong step one time in three.
    assert body["reasoning"] == {"effort": "low"} and body["max_tokens"] >= 1000
    system, asked = body["messages"]
    assert system == {"role": "system", "content": KEEPER_INSTRUCTIONS.format(name="Oscar")}
    assert asked == {
        "role": "user",
        "content": (
            "The script:\n"
            "1. Ask who is calling.\n"
            "2. Ask what they need help with.\n"
            "3. Thank them and say goodbye.\n"
            "\n"
            f"The call so far:\n{talk_text(TALK, 'Oscar')}"
        ),
    }


def test_the_keeper_is_told_what_makes_a_step_finished():
    rules = KEEPER_INSTRUCTIONS.format(name="Ava")
    assert "{" not in rules
    assert "Ava is an AI assistant on a phone call, working through a script" in rules
    # The mistake it made without this: a question asked counted as a step done.
    assert "It is not finished just because Ava has asked." in rules
    # Oscar hears the caller; the transcript can miss a turn.
    assert "If Ava has already gone on to a later step, the steps before that one are finished" in rules
    assert rules.endswith("or with DONE if every step is finished.")


async def test_a_finished_script_has_nothing_more_to_tell():
    keeper, _ = keeper_answering("DONE")
    assert await keeper.find_place(TALK) == 3
    assert keeper.note() == ""


async def test_the_place_can_go_back_when_a_step_turns_out_not_to_be_done():
    keeper, _ = keeper_answering("1")
    keeper.place = 2
    assert await keeper.find_place(TALK) == 0


@pytest.mark.parametrize(
    ("status", "message", "reason"),
    [
        (402, "Insufficient credits", "out of credits"),
        (404, "No endpoints found", f"OpenRouter no longer offers {KEEPER_MODEL}."),
        (500, "Internal error", r"OpenRouter returned an error \(500\). Internal error"),
    ],
)
async def test_a_refused_request_keeps_the_old_place_and_says_why(status, message, reason):
    keeper, _ = keeper_answering(message, status=status)
    keeper.place = 1
    with pytest.raises(ScriptLost, match=reason):
        await keeper.find_place(TALK)
    assert keeper.place == 1


async def test_an_answer_that_names_no_step_keeps_the_old_place():
    keeper, _ = keeper_answering("I think they are nearly done.", name="Ava")
    keeper.place = 1
    with pytest.raises(ScriptLost, match="The model keeping Ava's place answered 'I think they are nearly done.'"):
        await keeper.find_place(TALK)
    assert keeper.place == 1


async def test_an_unreachable_openrouter_keeps_the_old_place():
    def refuse(request: httpx.Request):
        raise httpx.ConnectError("no route to host")

    keeper = ScriptKeeper(
        STEPS,
        agent_name="Oscar",
        api_key="k",
        base_url=OPENROUTER,
        client=httpx.AsyncClient(transport=httpx.MockTransport(refuse)),
    )
    with pytest.raises(ScriptLost, match=r"OpenRouter was unreachable \(no route to host\)"):
        await keeper.find_place(TALK)
    assert keeper.place == 0


async def test_an_answer_in_a_shape_never_seen_keeps_the_old_place():
    def respond(request: httpx.Request):
        return httpx.Response(200, json={"choices": []})

    keeper = ScriptKeeper(
        STEPS,
        agent_name="Oscar",
        api_key="k",
        base_url=OPENROUTER,
        client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    with pytest.raises(ScriptLost, match="answered None"):
        await keeper.find_place(TALK)
