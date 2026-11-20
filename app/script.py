"""Keeping Oscar on its script.

The owner writes the steps of a call, one a line, in the dashboard's "Script"
box. On each of the caller's turns the speech model is told the step it is on
(`step_note`), and a text model works out which step that is:

    POST {openrouter}/chat/completions   the steps and the talk so far in; the number
                                         of the step Oscar is on out

That request runs after each of Oscar's replies, while the caller is still
listening to it, so it adds nothing to the wait for an answer.

Why the speech model is not simply handed the script, measured on 2026-11-12
(real replies in an Indian accent; the step each one did was read off by a
text model, the accent judged by listening models not told what to expect):
- With the whole script in its instructions it did the step it was on in 19
  replies of 32. It lost its place: four times it asked who was calling after
  the caller had said.
- Told the next step and the one after, in the words of STEP_NOTE, it did the
  right single step in 45 replies of 46 and skipped none: the next step after
  a side question, the one after it when the caller had just answered. That
  much it can judge by itself, because it hears the caller.
- Shown every step still to come, it asked a caller who had given their name
  for it again (3 replies of 4).
- The wording is narrow. "If the caller has just done or answered that" let it
  skip a step on a real call, when the caller had answered some other question
  (3 of 8 in a trial of that case): hence "only if ... gives exactly that".
- The note costs a little of the accent: 43 of those 46 replies kept it,
  against 80 of 80 without a script. Looser wordings kept it every time (52 of
  52) and skipped a step after a side question about three times in ten, which
  is the worse fault. Naming the accent again as the note's last words
  (app/agent.py) is what lets the narrow wording keep as much of it as it does.
"""

import re

import httpx

from app.costs import USAGE, CostMeter
from app.openrouter import DEFAULT_BASE_URL, auth_headers, describe_failure, new_client

KEEPER_MODEL = "anthropic/claude-haiku-5.5"
# Room for the thinking before the answer.
KEEPER_MAX_TOKENS = 1200
KEEPER_TIMEOUT = httpx.Timeout(15.0, connect=5.0)

# Asked for an answer straight off, the model named the wrong step in 13 to 18
# of 42 tries, however the rules were worded (it took a question Oscar had
# asked for a step done). Thinking a little first: 2 of 42, both the caller who
# would not give a name. Answers then took 2.7 seconds, 3.8 at the slowest,
# which is still inside the time the caller spends hearing Oscar's reply.
KEEPER_INSTRUCTIONS = (
    "{name} is an AI assistant on a phone call, working through a script one step at a time "
    "and in order. You are given the script and the call so far.\n"
    "Decide for each step whether it is finished:\n"
    "- A step that asks the caller for something (who they are, a day, a number) is finished "
    "once the caller has given it or has said they will not, whether {name} asked for it or "
    "the caller offered it. It is not finished just because {name} has asked.\n"
    "- Any other step, one that has {name} say or do something, is finished once {name} has "
    "said or done it.\n"
    "- If {name} has already gone on to a later step, the steps before that one are finished: "
    "{name} hears the caller, and the written call can miss something they said.\n"
    "Answer with the number of the first step that is not finished and nothing else, or with "
    "DONE if every step is finished."
)

# What the speech model is told about its script before a caller's turn. It
# hears the turn, so it is left to judge one thing: whether the caller has
# just given what the next step asks for.
STEP_NOTE = (
    "Your script. The next step to do: {step} Only if what the caller has just said gives "
    "exactly that, skip to the one after it: {after} Do only one step, after briefly answering "
    "anything they asked."
)
LAST_STEP_NOTE = (
    "Your script. The last step to do: {step} Do only that step, after briefly answering "
    "anything they asked."
)

# "1.", "2)", "Step 3:", "-" and the like at the start of a line.
_LIST_MARK = re.compile(r"^\s*(?:step\s*\d+\s*[:.)-]|\d+\s*[.):-]|[-*•–])\s*", flags=re.IGNORECASE)


_ENDS_AS_A_SENTENCE = re.compile(r"[.?!][\"'”’)\]]*$")


class ScriptLost(Exception):
    """Oscar's place in the script could not be worked out. The message is a sentence for the dashboard."""


def script_steps(script: str) -> list[str]:
    """The steps of a script: its lines, without their numbers or bullets."""
    steps = (_LIST_MARK.sub("", line).strip() for line in script.splitlines())
    # Each ends as a sentence, so that the note the speech model reads stays readable.
    return [step if _ENDS_AS_A_SENTENCE.search(step) else f"{step}." for step in steps if step]


def step_note(steps: list[str], place: int) -> str:
    """What the speech model is told about its script, on step `place` (counted from 0).

    Nothing once the script is finished.
    """
    if place >= len(steps):
        return ""
    if place == len(steps) - 1:
        return LAST_STEP_NOTE.format(step=steps[place])
    return STEP_NOTE.format(step=steps[place], after=steps[place + 1])


def parse_place(text: object, steps: list[str]) -> int | None:
    """The keeper's answer as a place in the script, or None when it is not one."""
    lines = text.split("\n") if isinstance(text, str) else []
    # The answer is the last line: now and then the model writes its thinking above it.
    text = next((line.strip() for line in reversed(lines) if line.strip()), "")
    if text.upper().rstrip(".") == "DONE":
        return len(steps)
    number = re.fullmatch(r"(?:step\s*)?(\d+)\.?", text, flags=re.IGNORECASE)
    if number and 1 <= int(number.group(1)) <= len(steps):
        return int(number.group(1)) - 1
    return None


def talk_text(talk: list[dict], agent_name: str) -> str:
    """The call so far, one line a turn."""
    return "\n".join(
        f"{agent_name if message['role'] == 'assistant' else 'Caller'}: {message['content']}"
        for message in talk
    )


class ScriptKeeper:
    """Knows which step of the script Oscar is on."""

    def __init__(
        self,
        steps: list[str],
        *,
        agent_name: str,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.AsyncClient | None = None,
        meter: CostMeter | None = None,
    ):
        self.steps = steps
        # The step Oscar is on, counted from 0. len(steps) once the script is finished.
        self.place = 0
        self._agent_name = agent_name
        self._instructions = KEEPER_INSTRUCTIONS.format(name=agent_name)
        self._chat_url = f"{base_url}/chat/completions"
        self._headers = auth_headers(api_key)
        self._client = client or new_client()
        self._meter = meter  # counts what the call's requests cost (app/costs.py)

    async def aclose(self) -> None:
        await self._client.aclose()

    def note(self) -> str:
        return step_note(self.steps, self.place)

    async def find_place(self, talk: list[dict]) -> int:
        """Work out again which step Oscar is on, from the call so far (as text).

        Raises ScriptLost, and keeps the place it had, when that cannot be done.
        """
        script = "\n".join(f"{number}. {step}" for number, step in enumerate(self.steps, 1))
        payload = {
            "model": KEEPER_MODEL,
            "usage": USAGE,
            "temperature": 0,
            "max_tokens": KEEPER_MAX_TOKENS,
            "reasoning": {"effort": "low"},  # see KEEPER_INSTRUCTIONS
            "messages": [
                {"role": "system", "content": self._instructions},
                {
                    "role": "user",
                    "content": (
                        f"The script:\n{script}\n\n"
                        f"The call so far:\n{talk_text(talk, self._agent_name)}"
                    ),
                },
            ],
        }
        try:
            response = await self._client.post(
                self._chat_url, json=payload, headers=self._headers, timeout=KEEPER_TIMEOUT
            )
        except httpx.HTTPError as exc:
            raise ScriptLost(f"OpenRouter was unreachable ({exc}).") from None
        if response.status_code != 200:
            raise ScriptLost(
                describe_failure(response, not_found=f"OpenRouter no longer offers {KEEPER_MODEL}.")
            )
        try:
            body = response.json()
        except ValueError:
            body = None
        if self._meter:
            self._meter.charge(body)
        try:
            text = body["choices"][0]["message"]["content"]
        except (LookupError, TypeError):
            text = None
        place = parse_place(text, self.steps)
        if place is None:
            raise ScriptLost(f"The model keeping {self._agent_name}'s place answered {text!r}.")
        self.place = place
        return place
