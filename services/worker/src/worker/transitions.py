"""The task state machine. Every status change goes through `transition`."""

from typing import Any

from psycopg.types.json import Jsonb

from worker.db import Conn

# From each status, where a task may go next. Terminal statuses have no way out.
ALLOWED: dict[str, frozenset[str]] = {
    "draft": frozenset({"queued", "cancelled"}),
    "queued": frozenset({"running", "paused", "cancelled"}),
    "running": frozenset({"paused", "waiting_user", "completed", "failed", "cancelled"}),
    "paused": frozenset({"running", "cancelled"}),
    "waiting_user": frozenset({"running", "paused", "cancelled", "failed"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}
TERMINAL = frozenset(status for status, targets in ALLOWED.items() if not targets)


class InvalidTransitionError(Exception):
    def __init__(self, task_id: object, current: str | None, target: str) -> None:
        super().__init__(f"task {task_id}: cannot go from {current!r} to {target!r}")
        self.current = current


async def transition(
    conn: Conn,
    task_id: object,
    target: str,
    *,
    event_type: str,
    payload: dict[str, Any] | None = None,
    waiting_reason: str | None = None,
    error_class: str | None = None,
) -> int:
    """Moves the task to `target` and records an event in one transaction.

    Returns the event's seq. Raises InvalidTransitionError if the task's current status does
    not allow the move; that includes losing a race (a task cancelled while it finished).
    """
    sources = [status for status, targets in ALLOWED.items() if target in targets]
    async with conn.transaction():
        row = await (
            await conn.execute(
                """
                update public.tasks
                   set status = %s, waiting_reason = %s, error_class = %s,
                       version = version + 1, updated_at = now()
                 where id = %s and status = any(%s)
             returning user_id
                """,
                (target, waiting_reason, error_class, task_id, sources),
            )
        ).fetchone()
        if row is None:
            current = await (
                await conn.execute("select status from public.tasks where id = %s", (task_id,))
            ).fetchone()
            raise InvalidTransitionError(task_id, current["status"] if current else None, target)
        # The UPDATE holds this task's row lock, so concurrent writers cannot pick the same seq.
        event = await (
            await conn.execute(
                """
                insert into public.task_events (task_id, user_id, seq, type, payload)
                select %s, %s, coalesce(max(seq), 0) + 1, %s, %s
                  from public.task_events where task_id = %s
             returning seq
                """,
                (task_id, row["user_id"], event_type, Jsonb(payload or {}), task_id),
            )
        ).fetchone()
        assert event is not None
        return int(event["seq"])


async def record_event(
    conn: Conn, task_id: object, event_type: str, payload: dict[str, Any]
) -> int:
    """Appends a task event without changing status. Returns its seq."""
    async with conn.transaction():
        # Locking the task row serialises writers, so two events never pick the same seq.
        row = await (
            await conn.execute(
                "select user_id from public.tasks where id = %s for update", (task_id,)
            )
        ).fetchone()
        assert row is not None, f"task {task_id} vanished"
        event = await (
            await conn.execute(
                """
                insert into public.task_events (task_id, user_id, seq, type, payload)
                select %s, %s, coalesce(max(seq), 0) + 1, %s, %s
                  from public.task_events where task_id = %s
             returning seq
                """,
                (task_id, row["user_id"], event_type, Jsonb(payload), task_id),
            )
        ).fetchone()
        assert event is not None
        return int(event["seq"])
