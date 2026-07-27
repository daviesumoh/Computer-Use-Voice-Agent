"""Stand-ins for the paid services behind memory (embeddings, extraction), plus helpers that
set up conversations. The database is always real."""

import hashlib
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import psycopg
from openai import OpenAIError
from psycopg.rows import DictRow

from worker.memory.embeddings import DIMENSIONS
from worker.memory.models import Candidate, Judgement, Known, Utterance

_STOP = frozenset(
    [
        "a",
        "an",
        "and",
        "the",
        "for",
        "to",
        "of",
        "in",
        "on",
        "at",
        "is",
        "are",
        "was",
        "be",
        "i",
        "my",
        "me",
        "it",
        "that",
        "this",
        "with",
        "from",
    ]
)


class FakeEmbedder:
    """Hashes words into a vector, so texts that share words are close and others are not.
    Enough to exercise ranking; real embedding quality is measured in the live eval."""

    def __init__(self) -> None:
        self.down = False

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if self.down:
            raise OpenAIError("embeddings unavailable")
        return [self._one(t) for t in texts]

    @staticmethod
    def _one(text: str) -> list[float]:
        vector = [0.0] * DIMENSIONS
        for word in re.findall(r"[a-z0-9]+", text.lower()):
            if word not in _STOP:
                slot = int(hashlib.md5(word.encode()).hexdigest(), 16) % DIMENSIONS
                vector[slot] += 1.0
        norm = math.sqrt(sum(x * x for x in vector)) or 1.0
        return [x / norm for x in vector]


@dataclass
class ScriptedMemoryModel:
    """Returns what the test scripted instead of calling a language model."""

    candidates: list[Candidate] = field(default_factory=list)
    summary: str = "The owner asked about TV prices."
    judge: Callable[[str, Sequence[Known]], list[Judgement]] = lambda new, known: []
    related: list[tuple[str, list[Known]]] = field(default_factory=list)

    async def extract(self, user_id: UUID, said: Sequence[Utterance]) -> list[Candidate]:
        return self.candidates

    async def relate(self, user_id: UUID, new: str, known: Sequence[Known]) -> list[Judgement]:
        self.related.append((new, list(known)))
        return self.judge(new, known)

    async def summarize(self, user_id: UUID, transcript: str) -> str:
        return self.summary


def rule(text: str, span: str, *, applies_to: str = "", cue: str = "explicit_rule") -> Candidate:
    return Candidate(type="rule", text=text, applies_to=applies_to, span=span, cue=cue)  # type: ignore[arg-type]


def says(
    admin: psycopg.Connection[DictRow], user_id: UUID, *lines: str, agent: Sequence[str] = ()
) -> tuple[UUID, list[Utterance]]:
    """A finished call in which the owner said the lines; returns it and the stored utterances."""
    conversation = admin.execute(
        "insert into public.conversations (user_id, channel, provider_call_sid, ended_at) "
        "values (%s, 'voice', %s, now()) returning id",
        (user_id, f"CA{uuid4().hex}"),
    ).fetchone()
    assert conversation is not None
    utterances = []
    for index, text in enumerate(lines):
        row = admin.execute(
            "insert into public.messages (conversation_id, user_id, direction, channel, "
            "provider_sid, body_redacted, turn_index) "
            "values (%s, %s, 'inbound', 'voice', %s, %s, %s) "
            "returning id",
            (conversation["id"], user_id, f"v:{uuid4()}", text, index * 2),
        ).fetchone()
        assert row is not None
        utterances.append(Utterance(row["id"], text))
    for index, text in enumerate(agent):
        admin.execute(
            "insert into public.messages (conversation_id, user_id, direction, channel, "
            "provider_sid, body_redacted, turn_index) "
            "values (%s, %s, 'outbound', 'voice', %s, %s, %s)",
            (conversation["id"], user_id, f"v:{uuid4()}", text, index * 2 + 1),
        )
    return conversation["id"], utterances
