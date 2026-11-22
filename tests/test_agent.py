"""What the speech model is told about how Oscar should sound (app/agent.py)."""

from datetime import date

import pytest

from app.agent import (
    NO_KNOWLEDGE_NOTE,
    NO_TOOLS_NOTE,
    SEARCH_NOTE,
    accent_kept,
    accent_note,
    accent_reminder,
    build_lookup,
    build_script,
    line_instruction,
    spoken_date,
    system_instruction,
)
from app.script import step_note
from app.store import ACCENTS, DEFAULT_AGENT
from tests.conftest import make_config


def with_accent(accent: str):
    return DEFAULT_AGENT.model_copy(update={"accent": accent})


def test_the_default_agent_is_asked_for_an_indian_accent():
    instruction = system_instruction(DEFAULT_AGENT)
    name, sound, behaviour, known, limits = instruction.split("\n\n")
    assert name == "Your name is Oscar."
    assert sound == accent_note(DEFAULT_AGENT)
    assert "clear, natural Indian accent" in sound
    assert behaviour == DEFAULT_AGENT.system_prompt
    assert known == NO_KNOWLEDGE_NOTE
    assert limits == NO_TOOLS_NOTE
    assert accent_reminder(DEFAULT_AGENT) == "(You are speaking in your Indian accent.)"
    assert accent_kept(DEFAULT_AGENT) == "Keep your Indian accent."


def test_a_script_is_kept_out_of_the_speech_models_instructions():
    # Handed the whole script, the model lost its place in it: it is told one step at a time.
    agent = DEFAULT_AGENT.model_copy(update={"script": "Ask who is calling.\nSay goodbye."})
    assert system_instruction(agent) == system_instruction(DEFAULT_AGENT)
    assert line_instruction(agent) == line_instruction(DEFAULT_AGENT)
    assert "who is calling" not in system_instruction(agent, can_search=True)


def test_a_call_keeps_its_place_in_the_owners_script(tmp_path):
    cfg = make_config(tmp_path, openrouter_api_key="sk-or-test", openrouter_base_url="https://openrouter.test/api/v1")
    agent = DEFAULT_AGENT.model_copy(
        update={"name": "Ava", "script": "1. Ask who is calling\n2. Ask what they need.\n\n- Say goodbye."}
    )
    keeper = build_script(cfg, agent)
    assert keeper.steps == ["Ask who is calling.", "Ask what they need.", "Say goodbye."]
    assert keeper.place == 0 and keeper.note() == step_note(keeper.steps, 0)
    assert keeper._chat_url == "https://openrouter.test/api/v1/chat/completions"
    assert keeper._headers["Authorization"] == "Bearer sk-or-test"
    assert "Ava is an AI assistant on a phone call" in keeper._instructions


def test_without_a_script_nothing_keeps_a_place(tmp_path):
    cfg = make_config(tmp_path)
    assert build_script(cfg, DEFAULT_AGENT) is None
    assert build_script(cfg, DEFAULT_AGENT.model_copy(update={"script": " \n - \n"})) is None


@pytest.mark.parametrize("accent", [a for a in ACCENTS if a != "none"])
def test_each_accent_is_named_in_the_instructions_and_the_reminder(accent):
    agent = with_accent(accent)
    assert f"natural {ACCENTS[accent]} accent" in system_instruction(agent)
    assert f"your {ACCENTS[accent]} accent" in accent_reminder(agent)
    # No other accent is asked for ("Southern American" holds the word "American").
    others = [ACCENTS[a] for a in ACCENTS if a not in (accent, "none")]
    said = system_instruction(agent) + accent_reminder(agent)
    assert not any(f"natural {other} accent" in said or f"your {other} accent" in said for other in others)


def test_every_accent_is_offered_and_can_be_saved():
    from typing import get_args

    from app.store import AccentId

    assert set(get_args(AccentId)) == set(ACCENTS)


def test_leaving_the_voice_as_it_is_asks_for_no_accent():
    agent = with_accent("none")
    assert system_instruction(agent) == (
        f"Your name is Oscar.\n\n{DEFAULT_AGENT.system_prompt}\n\n{NO_KNOWLEDGE_NOTE}\n\n{NO_TOOLS_NOTE}"
    )
    assert accent_note(agent) == accent_reminder(agent) == accent_kept(agent) == ""
    assert "accent" not in system_instruction(agent)


# Web search


def test_with_web_search_the_model_is_told_the_date_and_not_to_pretend():
    instruction = system_instruction(DEFAULT_AGENT, can_search=True, today=date(2026, 11, 11))
    name, sound, behaviour, known, today, note = instruction.split("\n\n")
    assert (name, sound, behaviour, known) == (
        "Your name is Oscar.",
        accent_note(DEFAULT_AGENT),
        DEFAULT_AGENT.system_prompt,
        NO_KNOWLEDGE_NOTE,
    )
    assert today == "Today's date is Wednesday 11 November 2026."
    assert note == SEARCH_NOTE
    assert NO_TOOLS_NOTE not in instruction
    # Searching is done elsewhere. Told more than this, the model lost the
    # agent's accent on some replies; told less, it made up the weather.
    assert "never make up live or current information" in note
    assert len(SEARCH_NOTE.split()) < 75


def test_the_date_is_said_the_way_a_person_says_it():
    assert spoken_date(date(2026, 1, 5)) == "Monday 5 January 2026"


def test_a_line_to_say_comes_with_who_oscar_is_and_nothing_about_searching():
    # With "never say you are checking something" in front of it, the model
    # refused to say "One moment, let me look that up" on real calls.
    instruction = line_instruction(DEFAULT_AGENT)
    assert instruction == (
        f"Your name is Oscar.\n\n{accent_note(DEFAULT_AGENT)}\n\n{DEFAULT_AGENT.system_prompt}"
    )
    assert "search" not in instruction.lower() and "look" not in instruction.lower()
    assert line_instruction(with_accent("none")) == (
        f"Your name is Oscar.\n\n{DEFAULT_AGENT.system_prompt}"
    )


def test_web_search_needs_a_firecrawl_key(tmp_path):
    today = date(2026, 11, 11)
    assert build_lookup(make_config(tmp_path), DEFAULT_AGENT, today) is None
    with_key = make_config(tmp_path, firecrawl_api_key="fc-test")
    lookup = build_lookup(with_key, DEFAULT_AGENT, today)
    assert (lookup.can_search, lookup.has_facts) == (True, False)


# What Oscar knows

FACTS = "We are Harbour Street Barbers. A men's haircut is $45. Call 02 5550 0142."


def knowing(facts: str = FACTS):
    return DEFAULT_AGENT.model_copy(update={"knowledge": facts})


def test_the_speech_model_is_told_not_to_invent_a_business():
    # Asked "What time do you close on Saturday?" it used to say "six in the evening".
    assert "you know nothing about any business's opening hours" in NO_KNOWLEDGE_NOTE
    assert "say you don't have that information" in NO_KNOWLEDGE_NOTE
    # Facts the owner wrote into the call instructions themselves still count.
    assert NO_KNOWLEDGE_NOTE.startswith("Apart from anything said above,")
    assert len(NO_KNOWLEDGE_NOTE.split()) < 50  # long notes cost the model its accent


@pytest.mark.parametrize("can_search", [False, True])
def test_the_fact_sheet_is_kept_out_of_the_speech_models_own_instructions(can_search):
    # In there it gave right answers but cost the accent on 7 of 28 real replies
    # (14 of 28 with a long sheet). Turns about it are answered elsewhere.
    told = system_instruction(knowing(), can_search=can_search, today=date(2026, 11, 12))
    assert told == system_instruction(DEFAULT_AGENT, can_search=can_search, today=date(2026, 11, 12))
    assert "Harbour Street" not in told and "5550" not in told
    assert "Harbour Street" not in line_instruction(knowing())


def test_a_fact_sheet_is_looked_things_up_in_even_without_web_search(tmp_path):
    today = date(2026, 11, 12)
    lookup = build_lookup(make_config(tmp_path), knowing(), today)
    assert (lookup.can_search, lookup.has_facts) == (False, True)
    both = build_lookup(make_config(tmp_path, firecrawl_api_key="fc-test"), knowing(), today)
    assert (both.can_search, both.has_facts) == (True, True)
    # An empty sheet is no sheet.
    assert build_lookup(make_config(tmp_path), knowing("   "), today) is None


def test_without_web_search_the_model_is_not_told_it_can_search(tmp_path):
    # A fact sheet alone gives a lookup, but no internet.
    told = system_instruction(knowing(), can_search=False)
    assert NO_TOOLS_NOTE in told and SEARCH_NOTE not in told


# The two models that hear when a caller speaks and stops (shared by every call)


def test_each_call_has_its_own_turn_detectors_on_models_loaded_once():
    from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
    from pipecat.audio.vad.silero import SileroVADAnalyzer

    from app.agent import turn_detectors

    (voice_a, turn_a), (voice_b, turn_b) = turn_detectors(), turn_detectors()
    # The loaded models are the same ones ...
    assert voice_a._model.session is voice_b._model.session and turn_a._session is turn_b._session
    # ... and what each call hears is its own.
    assert voice_a._model._state is not voice_b._model._state
    assert turn_a._audio_buffer is not turn_b._audio_buffer
    # Exactly the state pipecat's own constructors make: an upgrade that adds some fails here.
    assert set(vars(voice_a)) == set(vars(SileroVADAnalyzer()))
    assert set(vars(turn_a)) == set(vars(LocalSmartTurnAnalyzerV3()))


async def test_voice_detection_runs_where_the_audio_arrives():
    from app.agent import turn_detectors

    voice, _ = turn_detectors()
    voice.set_sample_rate(8000)
    voice._executor.shutdown()  # a thread hand-off would fail now
    assert (await voice.analyze_audio(b"\x00\x00" * 160)).name == "QUIET"
