"""The voice agent: it listens and speaks through one speech-to-speech model.

The caller's audio goes to OpenRouter and the model's spoken answer comes
straight back (app/speech.py), one round trip per turn. Pipecat decides when
the caller has finished a turn and keeps the conversation history.

The same pipeline serves browser calls (WebRTC) and phone calls (Twilio); only
the transport and the audio sample rates differ.
"""

import asyncio
from dataclasses import asdict
from datetime import date

from loguru import logger
from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.transports.base_transport import BaseTransport
from pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy import (
    TurnAnalyzerUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner

from app.calls import CallManager
from app.config import Config
from app.costs import CostMeter
from app.script import ScriptKeeper, script_steps
from app.search import WebLookup
from app.speech import EchoGate, GreetingFrame, OpenRouterVoiceService, Recordings
from app.store import ACCENTS, AgentSettings
from app.summary import start_summary

# Input and output sample rates (Hz). Phone audio is 8 kHz.
BROWSER_SAMPLE_RATES = (16000, 24000)
PHONE_SAMPLE_RATES = (8000, 8000)

# Hard cap on a single call, so a forgotten call cannot run up provider bills.
MAX_CALL_SECONDS = 30 * 60

# The caller's turn ends as soon as what they said sounds finished. When it
# sounds unfinished ("Hey", "Are you..."), it ends after this much more silence
# anyway. Pipecat's default is 3 seconds, which feels like Oscar has gone deaf.
# A short wait is safe here: the answer takes over a second to arrive, and a
# caller who carries on talking meanwhile is simply heard again in full
# (see app/speech.py).
UNFINISHED_TURN_SILENCE_SECONDS = 0.6

# On a real call Oscar told a caller "let me check the latest data" and then had
# nothing to check with. Without a Firecrawl key, calls have no web search.
NO_TOOLS_NOTE = (
    "You cannot look anything up during this call: you have no internet access and no "
    "tools. Never say you are checking or searching for something. If the caller asks "
    "for live or current information, say you cannot look it up from here, then offer "
    "what you know in general."
)


# The speech model's voices have no accents to choose from, so it is asked to
# put one on. This wording and where it goes were settled by measurement on
# 2026-11-11: about 290 real replies, each judged for accent by listening models
# that were not told which accent to expect.
# - Asked for only in the instructions, the accent was there on most replies
#   but faded on some. Repeated just before the caller's turn (ACCENT_REMINDER),
#   it held on every reply tried with the default voice.
# - Banning slang outright ('never say "g'day"') made one reply in four come
#   out American. Asking for "hi" or "hello" keeps the accent and still stops
#   most replies opening with "G'day!".
# - A reminder placed after the caller's turn was often answered as if the
#   caller had said it ("Sure thing. ..."). Worded as a statement and placed
#   before the turn, it was not.
# - Twelve of the thirteen voices take the accent. Ballad stays British.
# The same wording was measured for an Indian accent on 2026-11-12, when the
# owner asked for one: the default voice (Cedar) had it on 80 replies of 80,
# the greeting and looked-up answers included. Verse, Marin, Coral and Sage
# had it on 4 replies of 4; Alloy, Ash and Nova on 3; Shimmer on 2; Ballad,
# Echo, Fable and Onyx on 1, sounding British or American on the rest.
# Sixteen more accents were tried with this wording on 2026-11-22: a greeting
# and a first answer each from Cedar and Marin, then a first answer from
# Verse, Coral and Ash, judged by three listening models told nothing (a reply
# held when two of the three named the accent asked for). Kept, by replies
# held of 7: Southern American 6; French, German, Singaporean and Nigerian 4;
# Scottish 3 (Marin and Coral every time). Which voices held each is in
# ACCENT_FIT in app/static/app.js. Left out: Irish, New Zealand and South
# African (1 of 6 each, heard as American, Australian or British), Filipino,
# Italian and Mexican (1 of 4; asked for Mexican, Cedar answered in Spanish),
# Spanish (0 of 4), Canadian (always heard as American), and Kenyan and
# Jamaican (3 of 7, no voice held either on more than one reply).
ACCENT_NOTE = (
    "You speak English with a clear, natural {accent} accent, like someone who grew up "
    "there. Keep that accent in every sentence of the call, starting with your very first "
    "word. Choose the same everyday words you otherwise would: no slang or regional "
    "greetings unless the caller uses them first, and never mention your accent. "
    'Say hello with "hi" or "hello".'
)
ACCENT_REMINDER = "(You are speaking in your {accent} accent.)"


def accent_note(agent: AgentSettings) -> str:
    """How the model is told to sound, or nothing when the voice is left as it is."""
    return "" if agent.accent == "none" else ACCENT_NOTE.format(accent=ACCENTS[agent.accent])


def accent_reminder(agent: AgentSettings) -> str:
    return "" if agent.accent == "none" else ACCENT_REMINDER.format(accent=ACCENTS[agent.accent])


# With a script, the step Oscar is on follows the reminder, and the accent is
# named once more as that note's last words (app/script.py has the numbers).
ACCENT_KEPT = "Keep your {accent} accent."


def accent_kept(agent: AgentSettings) -> str:
    return "" if agent.accent == "none" else ACCENT_KEPT.format(accent=ACCENTS[agent.accent])


# With a Firecrawl key, turns that need the web are looked up and answered
# outside the speech model's own request (app/search.py). All it is told is
# this, which is kept short on purpose: a longer note about searching cost it
# the agent's accent on some replies. Without the middle sentence it made up
# the weather when asked for it.
SEARCH_NOTE = (
    "Questions that need the internet are looked up by another part of the system, which "
    "then answers the caller instead of you. So when it is your turn to answer, nothing has "
    "been looked up: never say you are checking or searching for something, and never make "
    "up live or current information. If the caller asks whether you can search the "
    "internet, say yes and ask what they would like to know."
)


# Without facts about anyone, Oscar used to answer as if it worked somewhere
# and made things up: asked "What time do you close on Saturday?" it said "six
# in the evening" on one call and "five PM" on the next. With this it said it
# did not have the hours, on 8 real replies of 8, and kept its accent (27 of 28).
# "Apart from anything said above" leaves alone facts the owner wrote into the
# call instructions themselves: those were still used, 8 times of 8.
#
# The owner's fact sheet (the dashboard's "What Oscar knows") is deliberately
# not put here. In these instructions it cost the model its accent on a quarter
# to half of its replies, so turns about it are answered outside this request
# (app/search.py), and the model is told the same thing with or without one.
NO_KNOWLEDGE_NOTE = (
    "Apart from anything said above, you know nothing about any business's opening hours, "
    "prices, address, services or bookings. If the caller asks for one of those and it is not "
    "above, say you don't have that information. Never guess or make it up."
)


def spoken_date(day: date) -> str:
    return f"{day:%A} {day.day} {day:%B %Y}"


def system_instruction(
    agent: AgentSettings, *, can_search: bool = False, today: date | None = None
) -> str:
    if can_search:
        # The model does not know the date, and "today" or "the latest" need one.
        abilities = f"Today's date is {spoken_date(today or date.today())}.\n\n{SEARCH_NOTE}"
    else:
        abilities = NO_TOOLS_NOTE
    parts = [
        f"Your name is {agent.name}.",
        accent_note(agent),
        agent.system_prompt,
        NO_KNOWLEDGE_NOTE,
        abilities,
    ]
    return "\n\n".join(part for part in parts if part)


def line_instruction(agent: AgentSettings) -> str:
    """What the model is told when it is handed a line to say (see app/speech.py).

    Who it is and how it sounds, and nothing about what it can or cannot do:
    with this it said 23 of 24 lines word for word, all in the agent's accent.
    Without the agent's own instructions it lost the accent on 6 of 18.
    """
    parts = [f"Your name is {agent.name}.", accent_note(agent), agent.system_prompt]
    return "\n\n".join(part for part in parts if part)


def build_lookup(
    cfg: Config, agent: AgentSettings, today: date, meter: CostMeter | None = None
) -> WebLookup | None:
    """What a call can look things up in: the web with a Firecrawl key, and the owner's fact sheet."""
    if not cfg.search_enabled and not agent.knowledge.strip():
        return None
    return WebLookup(
        firecrawl_api_key=cfg.firecrawl_api_key if cfg.search_enabled else "",
        firecrawl_base_url=cfg.firecrawl_base_url,
        openrouter_api_key=cfg.openrouter_api_key,
        openrouter_base_url=cfg.openrouter_base_url,
        agent_name=agent.name,
        agent_instructions=agent.system_prompt,
        knowledge=agent.knowledge,
        today=spoken_date(today),
        meter=meter,
    )


def build_script(cfg: Config, agent: AgentSettings, meter: CostMeter | None = None) -> ScriptKeeper | None:
    """What keeps Oscar's place in the owner's script, when there is one."""
    steps = script_steps(agent.script)
    if not steps:
        return None
    return ScriptKeeper(
        steps,
        agent_name=agent.name,
        api_key=cfg.openrouter_api_key,
        base_url=cfg.openrouter_base_url,
        meter=meter,
    )


def build_voice(
    cfg: Config, agent: AgentSettings, context: LLMContext, meter: CostMeter | None = None
) -> OpenRouterVoiceService:
    today = date.today()
    lookup = build_lookup(cfg, agent, today, meter)
    return OpenRouterVoiceService(
        api_key=cfg.openrouter_api_key,
        base_url=cfg.openrouter_base_url,
        voice=agent.voice,
        instructions=system_instruction(agent, can_search=cfg.search_enabled, today=today),
        reminder=accent_reminder(agent),
        line_instructions=line_instruction(agent),
        lookup=lookup,
        script=build_script(cfg, agent, meter),
        script_reminder=accent_kept(agent),
        context=context,
        # Next to the database: each machine records its own.
        recordings=Recordings(cfg.db_path.parent / "recordings"),
        warm_up=True,
        meter=meter,
    )


async def run_call(
    *,
    transport: BaseTransport,
    call_id: str,
    calls: CallManager,
    cfg: Config,
    agent: AgentSettings,
    sample_rates: tuple[int, int],
    barge_in: bool,
) -> None:
    """Run one call from connection to hang-up, logging it as it goes.

    `barge_in` lets the caller interrupt Oscar by talking over it. That only
    works where Oscar's voice cannot leak back into the caller's microphone (a
    phone, or headphones). Without it, Oscar finishes speaking before it listens.
    """
    error = ""
    connected = False
    watchdog: asyncio.Task | None = None
    try:
        context = LLMContext()
        # Everything this call asks OpenRouter for is counted toward it.
        meter = CostMeter(
            lambda dollars: calls.add_cost(call_id, dollars),
            api_key=cfg.openrouter_api_key,
            base_url=cfg.openrouter_base_url,
            spawn=calls.spawn,
        )
        voice = build_voice(cfg, agent, context, meter)
        # Realtime mode: the caller's turn ends on their silence alone, without
        # waiting for a transcript (the model hears the audio itself).
        end_of_turn = LocalSmartTurnAnalyzerV3(
            params=SmartTurnParams(stop_secs=UNFINISHED_TURN_SILENCE_SECONDS)
        )
        aggregators = LLMContextAggregatorPair(
            context,
            user_params=LLMUserAggregatorParams(
                vad_analyzer=SileroVADAnalyzer(),
                user_turn_strategies=UserTurnStrategies(
                    stop=[TurnAnalyzerUserTurnStopStrategy(turn_analyzer=end_of_turn)]
                ),
            ),
            realtime_service_mode=True,
        )
        user, assistant = aggregators.user(), aggregators.assistant()

        listening = [] if barge_in else [EchoGate()]
        pipeline = Pipeline(
            [transport.input(), *listening, user, voice, transport.output(), assistant]
        )
        in_rate, out_rate = sample_rates
        worker = PipelineWorker(
            pipeline,
            params=PipelineParams(audio_in_sample_rate=in_rate, audio_out_sample_rate=out_rate),
            enable_rtvi=False,
        )

        @transport.event_handler("on_client_connected")
        async def on_client_connected(transport, client):
            nonlocal connected
            connected = True
            calls.set_status_if_open(call_id, "live")
            if agent.greeting:
                await worker.queue_frames([GreetingFrame(agent.greeting)])

        @transport.event_handler("on_client_disconnected")
        async def on_client_disconnected(transport, client):
            await worker.cancel(reason="The caller hung up")

        # The transcript reads in the order things happened: what the caller
        # asked, what Oscar searched for, what Oscar said. The caller's words
        # are written down a moment after their turn (they are transcribed on
        # the side), so a search note that is ready sooner waits for them.
        words_pending = False
        notes_waiting: list[str] = []

        def add_waiting_notes():
            for note in notes_waiting:
                calls.add_message(call_id, "note", note)
            notes_waiting.clear()

        @user.event_handler("on_user_turn_started")
        async def on_user_turn_started(aggregator, strategy):
            nonlocal words_pending
            words_pending = True

        @user.event_handler("on_user_turn_message_added")
        async def on_user_turn_message_added(aggregator, message):
            nonlocal words_pending
            words_pending = False
            calls.add_message(call_id, "user", message.content)
            add_waiting_notes()

        @assistant.event_handler("on_assistant_turn_stopped")
        async def on_assistant_turn_stopped(aggregator, message):
            add_waiting_notes()  # the caller's words never came: do not lose the note
            text = message.content.strip()
            if text and message.interrupted:
                text += " [interrupted]"
            calls.add_message(call_id, "agent", text)

        @voice.event_handler("on_web_search")
        async def on_web_search(service, query, problem, sources):
            if problem:
                logger.warning(f"Call {call_id}: web search failed: {problem}")
                calls.note_error(call_id, f"Web search failed: {problem}")
                notes_waiting.append(f"Could not search the web. {problem}")
            else:
                notes_waiting.append(f"Searched the web for: {query}")
                # The caller hears a sentence or two of what was found. The pages
                # are kept for the notes written when the call ends (app/research.py).
                calls.add_search(call_id, query, [asdict(source) for source in sources])
            if not words_pending:
                add_waiting_notes()

        @voice.event_handler("on_facts_failed")
        async def on_facts_failed(service, problem):
            # Oscar has apologised to the caller; the owner should know why.
            logger.warning(f"Call {call_id}: could not answer from the fact sheet: {problem}")
            calls.note_error(call_id, f"Could not answer from what Oscar knows: {problem}")

        @voice.event_handler("on_script_lost")
        async def on_script_lost(service, problem):
            # Oscar carries on from the step it was last known to be on.
            logger.warning(f"Call {call_id}: lost its place in the script: {problem}")
            calls.note_error(call_id, f"Could not keep {agent.name}'s place in its script: {problem}")

        @worker.event_handler("on_pipeline_error")
        async def on_pipeline_error(worker, frame):
            logger.warning(f"Call {call_id}: {frame.error}")
            calls.note_error(call_id, str(frame.error))

        async def end_after_max_length():
            await asyncio.sleep(MAX_CALL_SECONDS)
            calls.note_error(call_id, "The call reached the 30 minute limit.")
            await worker.cancel(reason="Maximum call length reached")

        calls.register_worker(call_id, worker)
        watchdog = asyncio.create_task(end_after_max_length())

        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(worker)
        await runner.run()
    except Exception as exc:
        logger.exception(f"Call {call_id} crashed")
        error = f"The call stopped unexpectedly: {exc}"
    finally:
        if watchdog:
            watchdog.cancel()
        calls.unregister_worker(call_id)
        if not error and not connected:
            error = "The call never connected."
        calls.finish(call_id, error)
        # What the call was about, for the dashboard. Written now so that it
        # never delays a reply or the hang-up.
        call = calls.store.get_call(call_id)
        if call:
            start_summary(cfg, calls, call, agent, notify=True)
