"""Spending and working-time limits that pause a task instead of letting it run on."""

from uuid import UUID

from worker.db import Conn

DEFAULT_MONTHLY_CAP_CENTS = 15_000  # matches user_settings.monthly_cost_cap_cents
MAX_ACTIVE_SECONDS = 2 * 60 * 60  # per task, between the owner's resumes


async def monthly_cap_cents(conn: Conn, user_id: UUID) -> int:
    row = await (
        await conn.execute(
            "select monthly_cost_cap_cents from public.user_settings where user_id = %s",
            (user_id,),
        )
    ).fetchone()
    return DEFAULT_MONTHLY_CAP_CENTS if row is None else int(row["monthly_cost_cap_cents"])


async def spent_cents(conn: Conn, user_id: UUID, *, since: str) -> float:
    """Model spend since 'month' or 'day' started (UTC)."""
    assert since in {"month", "day"}
    row = await (
        await conn.execute(
            "select coalesce(sum(cost_cents), 0) as cents from public.llm_calls "
            "where user_id = %s and ts >= date_trunc(%s, now())",
            (user_id, since),
        )
    ).fetchone()
    assert row is not None
    return float(row["cents"])


async def over_monthly_cap(conn: Conn, user_id: UUID) -> bool:
    cap = await monthly_cap_cents(conn, user_id)
    return cap > 0 and await spent_cents(conn, user_id, since="month") >= cap
