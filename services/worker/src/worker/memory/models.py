"""The language-model side of memory: what to remember, how it relates to what is known, and
call summaries. Billed to llm_calls like every other model call."""

import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from openai import AsyncOpenAI
from pydantic import BaseModel

from worker.db import Pool

# USD per million tokens: (input, cached input, output). Source: OpenAI model page, 2026-11.
PRICES: dict[str, tuple[float, float, float]] = {"gpt-4.1-mini": (0.40, 0.10, 1.60)}

Cue = Literal["explicit_rule", "stated_fact", "repeated_choice", "one_off"]
Relation = Literal["duplicate", "refines", "contradicts", "unrelated"]


@dataclass(frozen=True)
class Utterance:
    """One thing the owner said, with the id of the stored message it came from."""

    id: UUID
    text: str


class Candidate(BaseModel):
    type: Literal["fact", "preference", "rule"]
    text: str  # one self-contained sentence, e.g. "Always buy electronics from Best Buy."
    applies_to: str  # when it applies ("electronics"), or "" when always
    span: str  # the owner's own words it was taken from, copied exactly
    cue: Cue


class _Candidates(BaseModel):
    candidates: list[Candidate]


class Known(BaseModel):
    id: UUID
    text: str


class Judgement(BaseModel):
    id: UUID
    relation: Relation


class _Judgements(BaseModel):
    judgements: list[Judgement]


class MemoryModel(Protocol):
    async def extract(self, user_id: UUID, said: Sequence[Utterance]) -> list[Candidate]: ...

    async def relate(self, user_id: UUID, new: str, known: Sequence[Known]) -> list[Judgement]: ...

    async def summarize(self, user_id: UUID, transcript: str) -> str: ...


EXTRACT_PROMPT = """\
You read what a person said to their phone assistant and list the lasting things worth
remembering about them. Only their own words count.

Cues:
- explicit_rule: they told you how to act from now on ("always use Best Buy for electronics").
- stated_fact: they told you something true about themselves or their world that will stay
  true ("my partner's name is Sam", "I'm in Toronto").
- repeated_choice: they made a choice that looks like a habit, but only said it once.
- one_off: anything about the task at hand ("find me a TV under $800", "check this site").

Rules:
- `span` must be copied exactly from what they said. If you cannot, leave the item out.
- Write `text` as one sentence that makes sense alone, in the third person or imperative.
- Skip passwords, PINs, card or account numbers, ID numbers and health details entirely.
- Skip anything that is only a request for this call. Most calls hold nothing to remember;
  return an empty list then.
"""

RELATE_PROMPT = """\
A new statement about a person is compared with what is already known. For each known item
say how the new statement relates to it:
- duplicate: says the same thing.
- refines: adds a narrower or more detailed case; the old item is still true.
- contradicts: both cannot be true at once; the new one replaces the old.
- unrelated: neither.
Judge every item listed, by its id.
"""

SUMMARY_PROMPT = """\
Summarise this phone call in one or two plain sentences: what the person wanted and what was
decided. Leave out passwords, card or account numbers, ID numbers and health details.
"""


class OpenAIMemoryModel:
    def __init__(self, client: AsyncOpenAI, model: str, pool: Pool) -> None:
        if model not in PRICES:
            raise ValueError(f"no price on file for {model}; add it to PRICES")
        self._client = client
        self._model = model
        self._pool = pool

    async def extract(self, user_id: UUID, said: Sequence[Utterance]) -> list[Candidate]:
        if not said:
            return []
        lines = "\n".join(f"- {u.text}" for u in said)
        parsed = await self._parse(user_id, "memory_extract", EXTRACT_PROMPT, lines, _Candidates)
        return parsed.candidates

    async def relate(self, user_id: UUID, new: str, known: Sequence[Known]) -> list[Judgement]:
        items = "\n".join(f"{k.id}: {k.text}" for k in known)
        prompt = f"New statement: {new}\n\nKnown:\n{items}"
        parsed = await self._parse(user_id, "memory_relate", RELATE_PROMPT, prompt, _Judgements)
        return parsed.judgements

    async def summarize(self, user_id: UUID, transcript: str) -> str:
        started = time.monotonic()
        response = await self._client.responses.create(
            model=self._model, instructions=SUMMARY_PROMPT, input=transcript, store=False
        )
        await self._bill(user_id, "memory_summary", response, started)
        return response.output_text.strip()

    async def _parse[T: BaseModel](
        self, user_id: UUID, route: str, instructions: str, text: str, shape: type[T]
    ) -> T:
        started = time.monotonic()
        response = await self._client.responses.parse(
            model=self._model,
            instructions=instructions,
            input=text,
            text_format=shape,
            store=False,  # what the owner said is not for OpenAI to retain
        )
        await self._bill(user_id, route, response, started)
        if response.output_parsed is None:
            raise ValueError(f"{route}: the model returned nothing parsable")
        return response.output_parsed

    async def _bill(self, user_id: UUID, route: str, response: object, started: float) -> None:
        usage = response.usage  # type: ignore[attr-defined]
        price_in, price_cached, price_out = PRICES[self._model]
        cached = usage.input_tokens_details.cached_tokens
        dollars = (
            (usage.input_tokens - cached) * price_in
            + cached * price_cached
            + usage.output_tokens * price_out
        ) / 1_000_000
        async with self._pool.connection() as conn:
            await conn.execute(
                "insert into public.llm_calls (user_id, route, model, input_tokens, cached_tokens, "
                "output_tokens, latency_ms, cost_cents) values (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    user_id, route, self._model, usage.input_tokens, cached, usage.output_tokens,
                    int((time.monotonic() - started) * 1000), round(dollars * 100, 4),
                ),
            )  # fmt: skip
