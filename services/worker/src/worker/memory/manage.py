"""Changing what is remembered: forgetting (with an undo window), restoring, editing, and the
owner's answers to the questions memory asks."""

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from worker.db import Conn
from worker.memory.embeddings import Embedder, vector_literal
from worker.memory.gates import sensitive_kind
from worker.memory.ingest import Actor, digest, embed_all, event

DEFAULT_UNDO_HOURS = 24


class SensitiveMemoryError(Exception):
    def __init__(self, kind: str) -> None:
        super().__init__(f"not stored: it looks like {kind.replace('_', ' ')}")
        self.kind = kind


class UnknownMemoryError(Exception):
    pass


async def _with_history(conn: Conn, user_id: UUID, ids: Sequence[UUID]) -> list[dict[str, Any]]:
    """The memories, plus every older version they replaced: forgetting a fact must not leave
    the previous wording of it behind."""
    return await (
        await conn.execute(
            """
            with recursive chain as (
              select id, canonical_text, status from public.memories
              where id = any(%s) and user_id = %s
              union
              select o.id, o.canonical_text, o.status
              from public.memories o join chain c on o.superseded_by = c.id
            )
            select id, canonical_text, status from chain
            """,
            (list(ids), user_id),
        )
    ).fetchall()


async def forget(
    conn: Conn,
    user_id: UUID,
    ids: Sequence[UUID],
    *,
    actor: Actor,
    immediately: bool = False,
    with_sources: bool = False,
) -> int:
    """Hides the memories now. They are erased after the owner's undo window (24 hours by
    default), or at once if asked, or if the owner's source messages are to go too."""
    async with conn.transaction():
        rows = await _with_history(conn, user_id, ids)
        if not rows:
            return 0
        found = [r["id"] for r in rows]
        hours = await _undo_hours(conn, user_id)
        erase = immediately or with_sources or hours == 0

        if with_sources:
            await conn.execute(
                "delete from public.messages where user_id = %s and id in "
                "(select source_id from public.memory_sources "
                " where memory_id = any(%s) and source_type = 'message')",
                (user_id, found),
            )
        # A summary written while the fact was known may contain it: those go at once.
        await conn.execute(
            "delete from public.episodes where user_id = %s and id in "
            "(select episode_id from public.episode_memory_links where memory_id = any(%s))",
            (user_id, found),
        )
        if erase:
            await conn.execute("delete from public.memories where id = any(%s)", (found,))
        else:
            await conn.execute(
                "update public.memories set status = 'deleted', deleted_at = now() "
                "where id = any(%s) and status <> 'deleted'",
                (found,),
            )
        for row in rows:
            await _record_deletion(conn, user_id, row["id"], row["canonical_text"], actor, erase)
        return len(rows)


async def _undo_hours(conn: Conn, user_id: UUID) -> int:
    row = await (
        await conn.execute(
            "select memory_undo_hours from public.user_settings where user_id = %s", (user_id,)
        )
    ).fetchone()
    return DEFAULT_UNDO_HOURS if row is None else row["memory_undo_hours"]


async def _record_deletion(
    conn: Conn, user_id: UUID, memory_id: UUID, text: str, actor: Actor, erased: bool
) -> None:
    # Only a hash of the text is kept, so the audit trail cannot leak what was forgotten.
    await conn.execute(
        "insert into public.memory_events (user_id, memory_id, op, actor, detail_hash) "
        "values (%s, %s, %s, %s, %s)",
        (user_id, memory_id, "purge" if erased else "delete", actor, digest(text)),
    )
    await conn.execute(
        "insert into audit.events (user_id, actor, action, target_type, target_id, metadata) "
        "values (%s, %s, 'memory_deleted', 'memory', %s, %s)",
        (user_id, actor, str(memory_id), Jsonb({"erased": erased, "text_sha256": digest(text)})),
    )


async def restore(conn: Conn, user_id: UUID, memory_id: UUID) -> bool:
    row = await (
        await conn.execute(
            "update public.memories set status = 'active', deleted_at = null "
            "where id = %s and user_id = %s and status = 'deleted' returning id",
            (memory_id, user_id),
        )
    ).fetchone()
    if row is not None:
        await event(conn, user_id, memory_id, "restore", "dashboard")
    return row is not None


async def purge_expired(conn: Conn) -> int:
    """Erases memories whose undo window has passed."""
    gone = await (
        await conn.execute(
            "delete from public.memories m where m.status = 'deleted' and m.deleted_at < now() - "
            "make_interval(hours => coalesce((select s.memory_undo_hours "
            "from public.user_settings s where s.user_id = m.user_id), %s)) "
            "returning m.id, m.user_id, m.canonical_text",
            (DEFAULT_UNDO_HOURS,),
        )
    ).fetchall()
    for row in gone:
        await _record_deletion(
            conn, row["user_id"], row["id"], row["canonical_text"], "worker", erased=True
        )
    return len(gone)


async def clear(conn: Conn, user_id: UUID, *, kind: str | None = None) -> int:
    """Forgets every memory, or every memory of one type. Clearing everything also wipes the
    call and task summaries and the remembered site routes. Returns the memories forgotten."""
    rows = await (
        await conn.execute(
            "select id from public.memories where user_id = %s and status <> 'deleted' "
            "and (%s::text is null or type = %s)",
            (user_id, kind, kind),
        )
    ).fetchall()
    forgotten = await forget(conn, user_id, [r["id"] for r in rows], actor="dashboard")
    if kind is None:
        await conn.execute("delete from public.episodes where user_id = %s", (user_id,))
        await conn.execute("delete from public.procedures where user_id = %s", (user_id,))
    return forgotten


async def settle(conn: Conn, user_id: UUID, memory_id: UUID, *, keep: bool, actor: Actor) -> bool:
    """The owner's answer about a habit memory noticed on its own."""
    if not keep:
        pending = await (
            await conn.execute(
                "select id from public.memories where id = %s and user_id = %s "
                "and status = 'pending_confirm'",
                (memory_id, user_id),
            )
        ).fetchone()
        # A habit that was guessed, not said, is erased at once when refused.
        return (
            pending is not None
            and await forget(conn, user_id, [memory_id], actor=actor, immediately=True) == 1
        )
    row = await (
        await conn.execute(
            "update public.memories set status = 'active', origin = 'user_confirmed', "
            "last_confirmed_at = now() where id = %s and user_id = %s "
            "and status = 'pending_confirm' returning id",
            (memory_id, user_id),
        )
    ).fetchone()
    if row is not None:
        await event(conn, user_id, memory_id, "confirm", actor)
    return row is not None


async def edit(conn: Conn, embedder: Embedder, user_id: UUID, memory_id: UUID, text: str) -> UUID:
    """The owner's rewording, as a new version that replaces the old one."""
    if kind := sensitive_kind(text):
        raise SensitiveMemoryError(kind)
    [vector] = await embed_all(embedder, [text])
    async with conn.transaction():
        old = await (
            await conn.execute(
                "select type, scope, cue from public.memories where id = %s and user_id = %s "
                "and status in ('active', 'pending_confirm')",
                (memory_id, user_id),
            )
        ).fetchone()
        if old is None:
            raise UnknownMemoryError(str(memory_id))
        created = await (
            await conn.execute(
                "insert into public.memories (user_id, type, scope, canonical_text, status, "
                "origin, cue, embedding, last_confirmed_at, announced_at) "
                "values (%s, %s, %s, %s, 'active', 'user_confirmed', %s, "
                "%s::extensions.vector, now(), now()) returning id",
                (
                    user_id,
                    old["type"],
                    Jsonb(old["scope"]),
                    text,
                    old["cue"],
                    vector_literal(vector),
                ),
            )
        ).fetchone()
        assert created is not None
        await conn.execute(
            "insert into public.memory_sources (memory_id, source_type, source_id, span) "
            "values (%s, 'dashboard_edit', %s, %s)",
            (created["id"], created["id"], text),
        )
        await conn.execute(
            "update public.memories set status = 'superseded', valid_to = now(), "
            "superseded_by = %s where id = %s",
            (created["id"], memory_id),
        )
        await event(conn, user_id, created["id"], "edit", "dashboard")
        new_id: UUID = created["id"]
        return new_id


async def set_enabled(conn: Conn, user_id: UUID, enabled: bool) -> None:
    """Pausing stops new memories and recall. What is already stored stays until deleted."""
    await conn.execute(
        "insert into public.user_settings (user_id, memory_enabled) values (%s, %s) "
        "on conflict (user_id) do update set memory_enabled = excluded.memory_enabled",
        (user_id, enabled),
    )
