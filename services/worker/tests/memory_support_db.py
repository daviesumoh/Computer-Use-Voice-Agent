"""Direct inserts for tests that need memories in a particular state."""

from uuid import UUID

import psycopg
from psycopg.rows import DictRow

from tests.memory_support import FakeEmbedder
from worker.memory.embeddings import vector_literal


def add_memory(
    admin: psycopg.Connection[DictRow],
    user_id: UUID,
    text: str,
    *,
    kind: str = "rule",
    status: str = "active",
    age_days: int = 0,
) -> UUID:
    row = admin.execute(
        "insert into public.memories (user_id, type, canonical_text, status, origin, cue, "
        "embedding, created_at) values (%s, %s, %s, %s, 'user_stated', 'explicit_rule', "
        "%s::extensions.vector, now() - make_interval(days => %s)) returning id",
        (user_id, kind, text, status, vector_literal(FakeEmbedder._one(text)), age_days),
    ).fetchone()
    assert row is not None
    return UUID(str(row["id"]))


def add_episode(
    admin: psycopg.Connection[DictRow], user_id: UUID, summary: str, kind: str = "call"
) -> UUID:
    row = admin.execute(
        "insert into public.episodes (user_id, kind, ref_id, summary, embedding) "
        "values (%s, %s, gen_random_uuid(), %s, %s::extensions.vector) returning id",
        (user_id, kind, summary, vector_literal(FakeEmbedder._one(summary))),
    ).fetchone()
    assert row is not None
    return UUID(str(row["id"]))
