"""The worker watching itself: conditions that need the owner's attention become alerts.

An alert opens once per kind, is texted once, stays on the dashboard while the condition
holds and closes by itself when it clears. Nothing here retries or works around a problem:
it reports it."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from uuid import UUID

import psycopg

from worker import limits
from worker.db import Conn, Pool

log = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 600
FAILED_TASKS = 3  # failed tasks in a day that count as a bad day
INTERNAL_ERRORS = 2
DAY_SPIKE_SHARE = 0.25  # of the monthly cap, spent in one day
MONTH_WARNING_SHARE = 0.8
UNDELIVERED = ("failed", "opted_out", "no_reachable_number")

Notify = Callable[[UUID, str, str], Awaitable[None]]


async def _count_failed(conn: Conn, user_id: UUID, error_class: str | None = None) -> int:
    row = await (
        await conn.execute(
            "select count(*) as n from public.tasks where user_id = %s and status = 'failed' "
            "and updated_at > now() - interval '1 day' and (%s::text is null or error_class = %s)",
            (user_id, error_class, error_class),
        )
    ).fetchone()
    assert row is not None
    return int(row["n"])


async def current_problems(conn: Conn, user_id: UUID) -> dict[str, str]:
    """Every condition that is true now, by alert kind."""
    problems: dict[str, str] = {}
    if (failed := await _count_failed(conn, user_id)) >= FAILED_TASKS:
        problems["task_failures"] = f"{failed} tasks failed in the last day"
    if (broken := await _count_failed(conn, user_id, "internal")) >= INTERNAL_ERRORS:
        problems["internal_errors"] = f"{broken} tasks hit unexpected errors in the last day"
    if await _count_failed(conn, user_id, "security_incident"):
        problems["security_incident"] = "a task was stopped for a security reason"

    texts = await (
        await conn.execute(
            "select delivery_status, count(*) as n from public.messages "
            "where user_id = %s and direction = 'outbound' and channel = 'sms' "
            "and delivery_status = any(%s) and received_at > now() - interval '1 day' "
            "group by delivery_status order by delivery_status",
            (user_id, list(UNDELIVERED)),
        )
    ).fetchall()
    if texts:
        summary = ", ".join(f"{t['n']} {t['delivery_status'].replace('_', ' ')}" for t in texts)
        problems["sms_delivery"] = f"texts to you were not delivered ({summary})"

    cap = await limits.monthly_cap_cents(conn, user_id)
    if cap > 0:
        today = await limits.spent_cents(conn, user_id, since="day")
        month = await limits.spent_cents(conn, user_id, since="month")
        if today >= cap * DAY_SPIKE_SHARE:
            problems["cost_spike"] = f"${today / 100:.2f} spent today, a quarter of the monthly cap"
        if month >= cap * MONTH_WARNING_SHARE:
            problems["cost_cap_near"] = (
                f"${month / 100:.2f} of the ${cap / 100:.2f} monthly cap is spent"
            )
    return problems


async def reconcile(conn: Conn, user_id: UUID, problems: dict[str, str]) -> list[tuple[UUID, str]]:
    """Opens alerts for new problems and closes those that cleared. Returns (id, detail) of the
    alerts opened now, which are the ones to text about."""
    opened: list[tuple[UUID, str]] = []
    for kind, detail in problems.items():
        row = await (
            await conn.execute(
                "insert into public.alerts (user_id, kind, detail) values (%s, %s, %s) "
                "on conflict (user_id, kind) where resolved_at is null "
                "do update set detail = excluded.detail "
                "returning id, (xmax = 0) as is_new",
                (user_id, kind, detail),
            )
        ).fetchone()
        assert row is not None
        if row["is_new"]:
            opened.append((row["id"], detail))
    await conn.execute(
        "update public.alerts set resolved_at = now() "
        "where user_id = %s and resolved_at is null and kind <> all(%s)",
        (user_id, list(problems)),
    )
    return opened


async def check_all(pool: Pool, notify: Notify, dashboard_url: str | None) -> int:
    """One pass over every user. Returns how many alerts were opened."""
    async with pool.connection() as conn:
        users = [
            r["id"] for r in await (await conn.execute("select id from public.profiles")).fetchall()
        ]
    raised = 0
    for user_id in users:
        async with pool.connection() as conn:
            opened = await reconcile(conn, user_id, await current_problems(conn, user_id))
        for alert_id, detail in opened:
            link = f" {dashboard_url}" if dashboard_url else ""
            await notify(user_id, f"alert:{alert_id}", f"Alert: {detail}.{link}")
        raised += len(opened)
    return raised


async def watch(pool: Pool, notify: Notify, dashboard_url: str | None) -> None:
    """Runs the checks for as long as the worker lives."""
    while True:
        try:
            if raised := await check_all(pool, notify, dashboard_url):
                log.warning("alerts raised", extra={"count": raised})
        except psycopg.Error:
            log.exception("health check failed; will run again")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
