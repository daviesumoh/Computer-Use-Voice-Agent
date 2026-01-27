"""Turns an owner's text into an action on their tasks and the reply to send back."""

import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from worker import edits
from worker.commands import HELP, OPT_OUT_WORDS, Command, parse
from worker.db import Conn
from worker.transitions import InvalidTransitionError, transition

ACTIVE = edits.ACTIVE
COMMAND_TO_INSTRUCTION = {"pause": "pause", "resume": "resume", "kill": "cancel"}


@dataclass(frozen=True)
class Routed:
    reply: str
    wake: tuple[UUID, ...] = ()  # workflows that should look at their inbox now


async def route(
    conn: Conn, *, user_id: UUID, message_id: UUID, text: str, dashboard_url: str | None = None
) -> Routed:
    command = parse(text)
    if command is None:
        return await _free_text(conn, user_id, message_id, text, dashboard_url)
    if command.verb == "halt_all":
        return await _halt_all(conn, user_id)
    if command.verb == "status":
        return Routed(await _status(conn, user_id, command))

    resolved = await _resolve(conn, user_id, command.short_code)
    if isinstance(resolved, str):
        return Routed(resolved)

    code, status = resolved["short_code"], resolved["status"]
    if status not in ACTIVE:
        return Routed(f"{code} is already {status}.")

    if command.verb == "kill" and status == "queued":
        # Not started yet, so no workflow is listening; cancel it directly.
        try:
            await transition(conn, resolved["id"], "cancelled", event_type="cancelled")
        except InvalidTransitionError:
            pass  # it started in the meantime: fall through and let the workflow apply it
        else:
            return Routed(f"{code} cancelled.")

    inserted = await (
        await conn.execute(
            "insert into public.instructions (user_id, task_id, message_id, command) "
            "values (%s, %s, %s, %s) on conflict (message_id) do nothing returning id",
            (user_id, resolved["id"], message_id, COMMAND_TO_INSTRUCTION[command.verb]),
        )
    ).fetchone()
    # A redelivered text already has its instruction; the workflow will reply once it applies it.
    if inserted is None:
        return Routed("", wake=(resolved["id"],))
    reply = (
        f"Got it: {command.verb} {code} queued; it starts after the current task."
        if (status == "queued")
        else ""
    )
    return Routed(reply, wake=(resolved["id"],))


_TASK_PREFIX = re.compile(r"^\s*(?P<code>t\d+)\s*[:,-]?\s+(?P<rest>.+)$", re.IGNORECASE | re.DOTALL)


async def _free_text(
    conn: Conn, user_id: UUID, message_id: UUID, text: str, dashboard_url: str | None
) -> Routed:
    """Anything that is not a command is a note for a task, or the answer to its question."""
    words = text.split()
    if words and words[0].lower().strip(".!") in OPT_OUT_WORDS:
        return Routed(HELP)  # a carrier opt-out word is never a note, and never a command
    prefixed = _TASK_PREFIX.match(text)
    short_code = prefixed["code"].upper() if prefixed else None
    body = prefixed["rest"] if prefixed else text
    resolved = await _resolve(conn, user_id, short_code)
    if isinstance(resolved, str):
        # With no task to address, an unknown text gets the list of commands.
        return Routed(HELP if resolved == "No active tasks." else resolved)
    code, status = resolved["short_code"], resolved["status"]
    if status not in ACTIVE:
        return Routed(f"{code} is already {status}.")
    if body.strip().lower().rstrip(".!") in edits.APPROVAL_WORDS and (
        resolved["waiting_reason"] == "approval"
    ):
        # Texts can be spoofed, so they can never approve anything.
        link = f" {dashboard_url}/approvals" if dashboard_url else ""
        return Routed(f"Approvals are only given in the dashboard, with your authenticator.{link}")
    await edits.submit(
        conn, user_id=user_id, task=resolved, text=body, channel="sms", message_id=message_id
    )
    return Routed(f"Noted for {code}.", wake=(resolved["id"],))


async def _resolve(conn: Conn, user_id: UUID, short_code: str | None) -> dict[str, Any] | str:
    """The addressed task, or the sentence to reply with when there isn't exactly one."""
    if short_code:
        row = await (
            await conn.execute(
                "select id, short_code, status, waiting_reason from public.tasks "
                "where user_id = %s and short_code = %s",
                (user_id, short_code),
            )
        ).fetchone()
        return row if row else f"No task {short_code}."
    rows = await (
        await conn.execute(
            "select id, short_code, status, waiting_reason from public.tasks "
            "where user_id = %s and status = any(%s) order by created_at",
            (user_id, list(ACTIVE)),
        )
    ).fetchall()
    if not rows:
        return "No active tasks."
    if len(rows) > 1:
        codes = ", ".join(r["short_code"] for r in rows)
        return f"Which task? Active: {codes}."
    return rows[0]


async def _status(conn: Conn, user_id: UUID, command: Command) -> str:
    rows = await (
        await conn.execute(
            "select short_code, title, status, waiting_reason, spent_cents, budget_cents "
            "from public.tasks where user_id = %s and (short_code = %s or (%s::text is null "
            "and status = any(%s))) order by created_at limit 10",
            (user_id, command.short_code, command.short_code, list(ACTIVE)),
        )
    ).fetchall()
    if not rows:
        return f"No task {command.short_code}." if command.short_code else "No active tasks."
    return "\n".join(
        f"{r['short_code']} {r['status']}"
        f"{' (' + r['waiting_reason'] + ')' if r['waiting_reason'] else ''}: {r['title']}"
        f" [${r['spent_cents'] / 100:.2f} of ${r['budget_cents'] / 100:.2f}]"
        for r in rows
    )


async def _halt_all(conn: Conn, user_id: UUID) -> Routed:
    await conn.execute(
        "insert into public.user_settings (user_id, emergency_stop) values (%s, true) "
        "on conflict (user_id) do update set emergency_stop = true, updated_at = now()",
        (user_id,),
    )
    await conn.execute(
        "insert into audit.events (user_id, actor, action) values (%s, 'user', 'emergency_stop')",
        (user_id,),
    )
    running = await (
        await conn.execute(
            "select id from public.tasks where user_id = %s and status = 'running'", (user_id,)
        )
    ).fetchall()
    return Routed(
        f"Emergency stop on. Pausing {len(running)} running task(s); nothing resumes until it is "
        "cleared in the dashboard.",
        wake=tuple(r["id"] for r in running),
    )
