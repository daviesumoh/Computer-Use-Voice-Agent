"""The plan's load: five calls at once while three tasks queue. Real Postgres, in-process API.

Run with `uv run pytest -m load -s`. Latencies are the worker's own (no network), so they
show contention on the database pool and the engine, not what a phone caller experiences."""

import asyncio
import statistics
import time
from collections.abc import Callable
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import DictRow

from tests.conftest import SignedClient, User
from tests.engine_harness import Harness, eventually
from worker.engine import StepResult

pytestmark = [pytest.mark.load, pytest.mark.db]

CALLS, TURNS = 5, 20
P95_LIMIT_SECONDS = 0.5


async def one_call(api: SignedClient, user: User, latencies: list[float]) -> UUID:
    async def timed(path: str, payload: dict[str, object]) -> dict[str, object]:
        began = time.monotonic()
        response = await api.post(path, payload)
        latencies.append(time.monotonic() - began)
        assert response.status_code in (200, 204), response.text
        return dict(response.json()) if response.content else {}

    started = await timed(
        "/v1/calls/start", {"call_sid": f"CA{uuid4().hex}", "caller_number": user.phone}
    )
    call = UUID(str(started["conversation_id"]))
    for turn in range(TURNS):
        await timed(
            f"/v1/calls/{call}/turns",
            {
                "turn_index": turn,
                "speaker": "user" if turn % 2 == 0 else "agent",
                "text": f"turn {turn}",
                "state": {"turn": turn},
            },
        )
    await timed(f"/v1/calls/{call}/end", {"reason": "hangup"})
    return call


async def test_five_calls_at_once_all_complete_with_every_turn_stored(
    api: SignedClient, admin: psycopg.Connection[DictRow], make_user: Callable[..., User]
) -> None:
    users = [make_user() for _ in range(CALLS)]
    latencies: list[float] = []

    began = time.monotonic()
    calls = await asyncio.gather(*(one_call(api, u, latencies) for u in users))
    elapsed = time.monotonic() - began

    p95 = statistics.quantiles(latencies, n=20)[-1]
    print(
        f"{len(latencies)} requests in {elapsed:.1f}s: "
        f"median {statistics.median(latencies) * 1000:.0f} ms, "
        f"p95 {p95 * 1000:.0f} ms, max {max(latencies) * 1000:.0f} ms"
    )
    for call in calls:
        row = admin.execute(
            "select count(*) n, (select ended_at is not null from public.conversations "
            "where id = %s) as ended from public.messages where conversation_id = %s",
            (call, call),
        ).fetchone()
        assert row == {"n": TURNS, "ended": True}
    assert p95 < P95_LIMIT_SECONDS


async def test_three_tasks_queue_and_run_one_at_a_time_in_order(
    harness: Harness, make_user: Callable[..., User]
) -> None:
    running = peak = 0
    order: list[UUID] = []

    async def step(task_id: UUID) -> StepResult:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        if task_id not in order:
            order.append(task_id)
        await asyncio.sleep(0.2)
        running -= 1
        return StepResult(1, finished=True)

    harness.runtime.executor = step
    tasks = [harness.new_task(make_user()) for _ in range(3)]

    for task in tasks:
        await harness.engine.enqueue(task)
    await eventually(
        lambda: all(harness.status(t)["status"] == "completed" for t in tasks), "all three", 30
    )

    assert peak == 1  # one browser at a time on the small host
    assert order == tasks
