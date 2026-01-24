"""Creating confirmed tasks and allocating per-user short codes."""

from typing import Any
from uuid import UUID

from agent_common.contracts import CreateTaskResponse
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from worker.db import Conn

FALLBACK_BUDGET_CENTS = 300


class UnknownConversationError(Exception):
    pass


class UnknownSiteError(Exception):
    """The caller asked for a site they have no connected account for."""


async def create_task(
    conn: Conn,
    *,
    request_id: UUID,
    conversation_id: UUID,
    title: str,
    spec: dict[str, Any],
    sites: list[str],
) -> CreateTaskResponse:
    """Creates a confirmed task in 'queued'. Repeating a request_id returns the same task."""
    existing = await _find_by_request(conn, request_id)
    if existing:
        return existing

    owner = await (
        await conn.execute(
            "select user_id from public.conversations where id = %s", (conversation_id,)
        )
    ).fetchone()
    if owner is None:
        raise UnknownConversationError(str(conversation_id))
    user_id = owner["user_id"]
    account_ids = await _accounts_for(conn, user_id, sites)

    try:
        async with conn.transaction():
            # Row-locks the profile until commit: concurrent creations get distinct numbers.
            number = await (
                await conn.execute(
                    "update public.profiles set next_task_number = next_task_number + 1 "
                    "where id = %s returning next_task_number - 1 as n",
                    (user_id,),
                )
            ).fetchone()
            assert number is not None
            short_code = f"T{number['n']}"

            task = await (
                await conn.execute(
                    """
                    insert into public.tasks (user_id, short_code, title, spec, status,
                      budget_cents, request_id, created_from_conversation, account_ids)
                    values (%s, %s, %s, %s, 'queued',
                      coalesce((select default_task_budget_cents from public.user_settings
                                where user_id = %s), %s),
                      %s, %s, %s)
                    returning id
                    """,
                    (
                        user_id,
                        short_code,
                        title,
                        Jsonb(spec),
                        user_id,
                        FALLBACK_BUDGET_CENTS,
                        request_id,
                        conversation_id,
                        account_ids,
                    ),
                )
            ).fetchone()
            assert task is not None
            await conn.execute(
                "insert into public.task_events (task_id, user_id, seq, type, payload) "
                "values (%s, %s, 1, 'created', %s)",
                (task["id"], user_id, Jsonb({"conversation_id": str(conversation_id)})),
            )
            await conn.execute(
                "insert into audit.events (user_id, actor, action, target_type, target_id) "
                "values (%s, 'worker', 'task_created', 'task', %s)",
                (user_id, str(task["id"])),
            )
    except UniqueViolation:
        # A concurrent request with the same request_id won the race; return its task.
        existing = await _find_by_request(conn, request_id)
        if existing is None:
            raise
        return existing

    return CreateTaskResponse(task_id=task["id"], short_code=short_code, created=True)


async def _find_by_request(conn: Conn, request_id: UUID) -> CreateTaskResponse | None:
    row = await (
        await conn.execute(
            "select id, short_code from public.tasks where request_id = %s", (request_id,)
        )
    ).fetchone()
    if row is None:
        return None
    return CreateTaskResponse(task_id=row["id"], short_code=row["short_code"], created=False)


async def _accounts_for(conn: Conn, user_id: UUID, sites: list[str]) -> list[UUID]:
    """Every active account the user has on each named site. Unknown sites are an error: the
    caller said "use my Best Buy login" and there is none, so the task must not start quietly
    without it."""
    if not sites:
        return []
    wanted = {site.strip().lower() for site in sites}
    rows = await (
        await conn.execute(
            "select id, site_domain from public.connected_accounts "
            "where user_id = %s and status = 'active' and site_domain = any(%s)",
            (user_id, sorted(wanted)),
        )
    ).fetchall()
    missing = wanted - {r["site_domain"] for r in rows}
    if missing:
        raise UnknownSiteError(", ".join(sorted(missing)))
    return [r["id"] for r in rows]
