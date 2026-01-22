import asyncio
from collections.abc import Callable
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import DictRow

from tests.conftest import User
from worker.db import Pool
from worker.transitions import InvalidTransitionError, transition

pytestmark = pytest.mark.db


def make_task(admin: psycopg.Connection[DictRow], user: User, status: str = "queued") -> UUID:
    row = admin.execute(
        "insert into public.tasks (user_id, short_code, title, spec, status, budget_cents) "
        "values (%s, %s, 'Task', '{}', %s, 300) returning id",
        (user.id, f"T{abs(hash((user.id, status))) % 10_000}", status),
    ).fetchone()
    assert row is not None
    return UUID(str(row["id"]))


async def test_transition_updates_status_and_records_an_event(
    pool: Pool, make_user: Callable[..., User], admin: psycopg.Connection[DictRow]
) -> None:
    task_id = make_task(admin, make_user())
    async with pool.connection() as conn:
        seq = await transition(conn, task_id, "running", event_type="started")

    task = admin.execute("select status, version from public.tasks where id = %s", (task_id,))
    assert task.fetchone() == {"status": "running", "version": 1}
    events = admin.execute(
        "select seq, type from public.task_events where task_id = %s", (task_id,)
    )
    assert events.fetchall() == [{"seq": seq, "type": "started"}]


async def test_waiting_user_requires_a_reason_and_clears_when_leaving(
    pool: Pool, make_user: Callable[..., User], admin: psycopg.Connection[DictRow]
) -> None:
    task_id = make_task(admin, make_user(), "running")
    async with pool.connection() as conn:
        await transition(
            conn, task_id, "waiting_user", event_type="needs_login", waiting_reason="login"
        )
        await transition(conn, task_id, "running", event_type="resumed")
    row = admin.execute("select status, waiting_reason from public.tasks where id = %s", (task_id,))
    assert row.fetchone() == {"status": "running", "waiting_reason": None}


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("completed", "running"),
        ("cancelled", "queued"),
        ("queued", "completed"),
        ("paused", "failed"),
    ],
)
async def test_disallowed_moves_are_rejected_and_leave_no_trace(
    pool: Pool,
    make_user: Callable[..., User],
    admin: psycopg.Connection[DictRow],
    current: str,
    target: str,
) -> None:
    task_id = make_task(admin, make_user(), current)
    async with pool.connection() as conn:
        with pytest.raises(InvalidTransitionError) as raised:
            await transition(conn, task_id, target, event_type="x")
    assert raised.value.current == current
    row = admin.execute("select status from public.tasks where id = %s", (task_id,)).fetchone()
    assert row == {"status": current}
    assert (
        admin.execute("select 1 from public.task_events where task_id = %s", (task_id,)).fetchone()
        is None
    )


async def test_concurrent_transitions_get_distinct_event_numbers(
    pool: Pool, make_user: Callable[..., User], admin: psycopg.Connection[DictRow]
) -> None:
    task_id = make_task(admin, make_user(), "running")

    async def toggle(target: str) -> int | None:
        async with pool.connection() as conn:
            try:
                return await transition(conn, task_id, target, event_type=target)
            except InvalidTransitionError:
                return None

    # Both race for the same row; exactly one can win the move out of 'running'.
    results = await asyncio.gather(toggle("paused"), toggle("completed"))
    assert sorted(r for r in results if r is not None) == [1]
    seqs = admin.execute("select seq from public.task_events where task_id = %s", (task_id,))
    assert [r["seq"] for r in seqs.fetchall()] == [1]


async def test_a_late_cancel_cannot_overwrite_a_finished_task(
    pool: Pool, make_user: Callable[..., User], admin: psycopg.Connection[DictRow]
) -> None:
    task_id = make_task(admin, make_user(), "running")
    async with pool.connection() as conn:
        await transition(conn, task_id, "completed", event_type="completed")
        with pytest.raises(InvalidTransitionError):
            await transition(conn, task_id, "cancelled", event_type="cancelled")
    row = admin.execute("select status from public.tasks where id = %s", (task_id,)).fetchone()
    assert row == {"status": "completed"}
