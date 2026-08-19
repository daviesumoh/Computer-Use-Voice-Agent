"""What worked on a site last time, kept so the browser agent can start from it."""

from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from psycopg.types.json import Jsonb

from worker.db import Conn

MAX_STEPS = 12
MAX_STEP_CHARS = 120  # step texts contain element names from the website; keep them short
ROUTE_ACTIONS = frozenset({"navigate", "click", "type", "select", "press"})


async def route_hint(conn: Conn, user_id: UUID, task_id: UUID, domain: str) -> str | None:
    """The latest route that worked on this site, as text for the browser agent. Records that
    it was shown, so a failure afterwards can mark the route stale."""
    row = await (
        await conn.execute(
            "select id, task_type, steps from public.procedures "
            "where user_id = %s and domain = %s and not stale and success_count > 0 "
            "order by last_success_at desc limit 1",
            (user_id, domain),
        )
    ).fetchone()
    if row is None:
        return None
    await conn.execute(
        "insert into public.task_events (task_id, user_id, seq, type, payload) values "
        "(%s, %s, (select coalesce(max(seq), 0) + 1 from public.task_events where task_id = %s), "
        "'procedure_used', %s)",
        (task_id, user_id, task_id, Jsonb({"procedure_id": str(row["id"])})),
    )
    steps = "\n".join(f"{i}. {step}" for i, step in enumerate(row["steps"], 1))
    return f"A {row['task_type']} task on {domain} went like this last time:\n{steps}"


async def record_outcome(conn: Conn, task: dict[str, Any]) -> None:
    """After a task ends: keep its route if it worked, or mark the route it was given stale
    if it did not."""
    events = await (
        await conn.execute(
            "select type, payload from public.task_events where task_id = %s order by seq",
            (task["id"],),
        )
    ).fetchall()
    steps = [
        e["payload"]["summary"][:MAX_STEP_CHARS]
        for e in events
        if e["type"] == "step" and e["payload"].get("action") in ROUTE_ACTIONS
    ]
    last_url = next(
        (
            e["payload"]["url"]
            for e in reversed(events)
            if e["type"] == "step" and e["payload"].get("url")
        ),
        None,
    )
    domain = urlsplit(last_url).hostname if last_url else None
    if not domain:
        return
    given = next(
        (e["payload"]["procedure_id"] for e in events if e["type"] == "procedure_used"), None
    )

    if task["status"] == "failed" and given:
        await conn.execute(
            "update public.procedures set fail_count = fail_count + 1, stale = true where id = %s",
            (given,),
        )
    elif task["status"] == "completed" and steps:
        wrote = await (
            await conn.execute(
                "select 1 from public.browser_actions where task_id = %s", (task["id"],)
            )
        ).fetchone()
        await conn.execute(
            "insert into public.procedures (user_id, domain, task_type, steps, success_count, "
            "last_success_at) values (%s, %s, %s, %s, 1, now()) "
            "on conflict (user_id, domain, task_type) do update set steps = excluded.steps, "
            "success_count = procedures.success_count + 1, stale = false, last_success_at = now()",
            (task["user_id"], domain, "write" if wrote else "read", Jsonb(steps[:MAX_STEPS])),
        )
