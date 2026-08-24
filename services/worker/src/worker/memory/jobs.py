"""Background learning: what to remember after a call, and what to note after a task."""

import logging
from dataclasses import dataclass
from uuid import UUID

from worker.db import Conn
from worker.memory import procedures
from worker.memory.embeddings import Embedder, vector_literal
from worker.memory.gates import sensitive_kind
from worker.memory.ingest import embed_all, ingest
from worker.memory.models import MemoryModel, Utterance
from worker.memory.recall import enabled

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Memory:
    embedder: Embedder
    model: MemoryModel


async def learn_from_conversation(conn: Conn, memory: Memory, conversation_id: UUID) -> str:
    """Safe to run again after a crash: statements already stored are recognised as duplicates
    and the call's summary is written once."""
    conversation = await (
        await conn.execute(
            "select user_id from public.conversations where id = %s and channel = 'voice'",
            (conversation_id,),
        )
    ).fetchone()
    if conversation is None:
        return "unknown conversation"
    user_id = conversation["user_id"]
    if not await enabled(conn, user_id):
        return "memory paused"
    summarised = await conn.execute(
        "select 1 from public.episodes where kind = 'call' and ref_id = %s", (conversation_id,)
    )
    if await summarised.fetchone():
        return "already learned"

    turns = await (
        await conn.execute(
            "select id, direction, body_redacted from public.messages "
            "where conversation_id = %s order by turn_index, received_at",
            (conversation_id,),
        )
    ).fetchall()
    said = [Utterance(t["id"], t["body_redacted"]) for t in turns if t["direction"] == "inbound"]
    if not said:
        return "nobody spoke"

    candidates = await memory.model.extract(user_id, said)
    outcome = await ingest(
        conn,
        embedder=memory.embedder,
        model=memory.model,
        user_id=user_id,
        candidates=candidates,
        said=said,
    )

    transcript = "\n".join(
        f"{'Owner' if t['direction'] == 'inbound' else 'Assistant'}: {t['body_redacted']}"
        for t in turns
    )
    summary = await memory.model.summarize(user_id, transcript)
    if summary and not sensitive_kind(summary):
        await _write_episode(conn, memory, user_id, "call", conversation_id, summary, None)
        await conn.execute(
            "insert into public.episode_memory_links (episode_id, memory_id) "
            "select e.id, m from public.episodes e, unnest(%s::uuid[]) m "
            "where e.kind = 'call' and e.ref_id = %s on conflict do nothing",
            (outcome.touched, conversation_id),
        )
    return (
        f"created {len(outcome.created)}, refreshed {len(outcome.refreshed)}, "
        f"superseded {len(outcome.superseded)}, rejected {dict(outcome.rejected)}"
    )


async def learn_from_task(conn: Conn, memory: Memory, task_id: UUID) -> str:
    """A finished task becomes an episode, and its route a procedure. The episode holds only
    what the owner and the engine said (title, summary, outcome), never text from websites."""
    task = await (
        await conn.execute(
            "select id, user_id, title, spec, status, error_class from public.tasks "
            "where id = %s and status in ('completed', 'failed', 'cancelled')",
            (task_id,),
        )
    ).fetchone()
    if task is None:
        return "not finished"
    if not await enabled(conn, task["user_id"]):
        return "memory paused"

    outcome: str = task["status"] + (f" ({task['error_class']})" if task["error_class"] else "")
    asked = task["spec"].get("summary") if isinstance(task["spec"], dict) else None
    summary = f"Task '{task['title']}'" + (f": {asked}" if asked else "") + f". It {outcome}."
    if not sensitive_kind(summary):
        await _write_episode(conn, memory, task["user_id"], "task", task_id, summary, outcome)
    await procedures.record_outcome(conn, task)
    return outcome


async def _write_episode(
    conn: Conn,
    memory: Memory,
    user_id: UUID,
    kind: str,
    ref_id: UUID,
    summary: str,
    outcome: str | None,
) -> None:
    [vector] = await embed_all(memory.embedder, [summary])
    await conn.execute(
        "insert into public.episodes (user_id, kind, ref_id, summary, outcome, embedding) "
        "values (%s, %s, %s, %s, %s, %s::extensions.vector) on conflict (kind, ref_id) do nothing",
        (user_id, kind, ref_id, summary, outcome, vector_literal(vector)),
    )
