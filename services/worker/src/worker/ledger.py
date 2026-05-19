"""The action ledger: an irreversible action is written down before it is attempted."""

from uuid import UUID

from psycopg.errors import UniqueViolation

from worker.db import Conn


async def start(
    conn: Conn,
    *,
    task_id: UUID,
    user_id: UUID,
    action_class: str,
    description: str,
    intent_hash: str,
) -> int | None:
    """Claims the right to perform this action once. None means it was already attempted."""
    try:
        row = await (
            await conn.execute(
                "insert into public.browser_actions (task_id, user_id, action_class, description, "
                "intent_hash, phase) values (%s, %s, %s, %s, %s, 'started') returning id",
                (task_id, user_id, action_class, description, intent_hash),
            )
        ).fetchone()
    except UniqueViolation:
        return None
    assert row is not None
    return int(row["id"])


async def finish(conn: Conn, entry_id: int, phase: str) -> None:
    await conn.execute(
        "update public.browser_actions set phase = %s, finished_at = now() where id = %s",
        (phase, entry_id),
    )
