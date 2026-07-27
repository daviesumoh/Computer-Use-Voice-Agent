"""Storing what the owner said: no duplicates, and a newer statement replaces a contradicted one."""

import hashlib
import logging
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from openai import OpenAIError
from psycopg.types.json import Jsonb

from worker.db import Conn
from worker.memory.embeddings import Embedder, vector_literal
from worker.memory.gates import Rejection, screen
from worker.memory.models import Candidate, Known, MemoryModel, Relation, Utterance

log = logging.getLogger(__name__)

NEIGHBOURS = 5  # how many known memories each new statement is compared with
Actor = Literal["voice", "dashboard", "worker"]


@dataclass
class Outcome:
    created: list[UUID] = field(default_factory=list)
    refreshed: list[UUID] = field(default_factory=list)
    superseded: list[UUID] = field(default_factory=list)
    rejected: Counter[str] = field(default_factory=Counter)

    @property
    def touched(self) -> list[UUID]:
        """The memories this conversation is about, new or confirmed again."""
        return [*self.created, *self.refreshed]


def normal(text: str) -> str:
    return " ".join(text.lower().split()).rstrip(".!")


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


async def event(conn: Conn, user_id: UUID, memory_id: UUID, op: str, actor: Actor) -> None:
    await conn.execute(
        "insert into public.memory_events (user_id, memory_id, op, actor) values (%s, %s, %s, %s)",
        (user_id, memory_id, op, actor),
    )


async def embed_all(embedder: Embedder, texts: Sequence[str]) -> list[list[float]] | list[None]:
    """Vectors for the texts, or None for each if the embedding service is unavailable: a
    memory without a vector is still found by keyword search."""
    if not texts:
        return []
    try:
        return await embedder.embed(texts)
    except OpenAIError as unavailable:
        log.warning(
            "embeddings unavailable; storing without vectors", extra={"error": str(unavailable)}
        )
        return [None] * len(texts)


async def ingest(
    conn: Conn,
    *,
    embedder: Embedder,
    model: MemoryModel,
    user_id: UUID,
    candidates: Sequence[Candidate],
    said: Sequence[Utterance],
) -> Outcome:
    outcome = Outcome()
    accepted: list[tuple[Candidate, Utterance]] = []
    for candidate in candidates:
        verdict = screen(candidate, said)
        if isinstance(verdict, Rejection):
            outcome.rejected[verdict.reason] += 1
        else:
            accepted.append((candidate, verdict))

    vectors = await embed_all(embedder, [c.text for c, _ in accepted])
    for (candidate, source), vector in zip(accepted, vectors, strict=True):
        async with conn.transaction():
            await _store(conn, model, user_id, candidate, source, vector, outcome)
    return outcome


async def _neighbours(conn: Conn, user_id: UUID, vector: list[float] | None) -> list[Known]:
    columns = "select id, canonical_text from public.memories"
    live = "where user_id = %s and status in ('active', 'pending_confirm')"
    if vector is None:
        sql = f"{columns} {live} order by created_at desc limit %s"
        params: tuple[object, ...] = (user_id, NEIGHBOURS)
    else:
        nearest = "order by embedding <=> %s::extensions.vector nulls last, created_at desc"
        sql = f"{columns} {live} {nearest} limit %s"
        params = (user_id, vector_literal(vector), NEIGHBOURS)
    rows = await (await conn.execute(sql, params)).fetchall()
    return [Known(id=r["id"], text=r["canonical_text"]) for r in rows]


async def _store(
    conn: Conn,
    model: MemoryModel,
    user_id: UUID,
    candidate: Candidate,
    source: Utterance,
    vector: list[float] | None,
    outcome: Outcome,
) -> None:
    known = await _neighbours(conn, user_id, vector)
    relations: dict[UUID, Relation] = {}
    if same := next((k for k in known if normal(k.text) == normal(candidate.text)), None):
        relations[same.id] = "duplicate"
    elif known:
        listed = {k.id for k in known}
        relations = {j.id: j.relation for j in await model.relate(user_id, candidate.text, known)
                     if j.id in listed}  # fmt: skip

    duplicate = next((i for i, r in relations.items() if r == "duplicate"), None)
    if duplicate is not None:
        await _add_source(conn, duplicate, source, candidate.span)
        await conn.execute(
            "update public.memories set last_confirmed_at = now() "
            "where id = %s and status = 'active'",
            (duplicate,),
        )
        outcome.refreshed.append(duplicate)
        return

    replaced = [i for i, r in relations.items() if r == "contradicts"]
    habit = candidate.cue == "repeated_choice"
    if habit and replaced:
        owner_said = await (
            await conn.execute(
                "select 1 from public.memories where id = any(%s) "
                "and origin in ('user_stated', 'user_confirmed')",
                (replaced,),
            )
        ).fetchone()
        if owner_said:
            # A guess from behaviour never overrides something the owner said outright.
            outcome.rejected["contradicts_stated"] += 1
            return

    created = await (
        await conn.execute(
            "insert into public.memories (user_id, type, scope, canonical_text, status, origin, "
            "cue, embedding, last_confirmed_at) "
            "values (%s, %s, %s, %s, %s, %s, %s, %s::extensions.vector, now()) returning id",
            (
                user_id,
                candidate.type,
                Jsonb({"applies_to": candidate.applies_to} if candidate.applies_to else {}),
                candidate.text,
                "pending_confirm" if habit else "active",
                "inferred" if habit else "user_stated",
                candidate.cue,
                vector_literal(vector),
            ),
        )
    ).fetchone()
    assert created is not None
    await _add_source(conn, created["id"], source, candidate.span)
    await event(conn, user_id, created["id"], "create", "voice")
    outcome.created.append(created["id"])

    if replaced:
        await conn.execute(
            "update public.memories set status = 'superseded', valid_to = now(), "
            "superseded_by = %s where id = any(%s) and status in ('active', 'pending_confirm')",
            (created["id"], replaced),
        )
        for old in replaced:
            await event(conn, user_id, old, "supersede", "voice")
        outcome.superseded.extend(replaced)


async def _add_source(conn: Conn, memory_id: UUID, source: Utterance, span: str) -> None:
    await conn.execute(
        "insert into public.memory_sources (memory_id, source_type, source_id, span) "
        "values (%s, 'message', %s, %s) on conflict do nothing",
        (memory_id, source.id, span),
    )
