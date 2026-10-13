"""What the owner adds to a task while it runs: a note, or the answer to its question.

Both travel as instructions and are applied at the task's next safe point, in the order they
arrived. Notes land in the task's spec as `owner_edits`, which the browser agent reads with
the rest of the task. Two notes from different channels close together might contradict each
other; the task then stops and asks which one stands (see engine._apply_edit)."""

from typing import Any
from uuid import UUID

from worker.db import Conn

ACTIVE = ("queued", "running", "paused", "waiting_user")
MAX_BODY = 1000
APPROVAL_WORDS = frozenset({"approve", "approved", "yes", "y", "ok", "okay", "confirm", "go"})
# Notes from different channels this close together may contradict each other.
CONFLICT_WINDOW_SECONDS = 60


async def submit(
    conn: Conn,
    *,
    user_id: UUID,
    task: dict[str, Any],
    text: str,
    channel: str,
    message_id: UUID | None = None,
) -> bool:
    """Records the text as an answer (when the task is waiting to be asked) or a note. False
    if this exact message was already recorded."""
    waiting_to_answer = (task["status"], task["waiting_reason"]) == ("waiting_user", "clarify")
    inserted = await (
        await conn.execute(
            "insert into public.instructions "
            "(user_id, task_id, message_id, command, channel, body) "
            "values (%s, %s, %s, %s, %s, %s) on conflict (message_id) do nothing returning id",
            (
                user_id,
                task["id"],
                message_id,
                "answer" if waiting_to_answer else "edit",
                channel,
                text.strip()[:MAX_BODY],
            ),
        )
    ).fetchone()
    return inserted is not None
