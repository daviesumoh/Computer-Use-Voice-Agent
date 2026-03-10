"""A real DBOS engine with a scripted executor, for tests of the engine and the jobs on it."""

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import UUID

import psycopg
from psycopg.rows import DictRow

from worker.engine import Engine, Runtime, StepResult

if TYPE_CHECKING:
    from tests.conftest import User


@dataclass
class Harness:
    engine: Engine
    runtime: Runtime
    admin: psycopg.Connection[DictRow]
    texts: list[tuple[str, str]] = field(default_factory=list)  # (dedupe_key, body)
    executed: list[UUID] = field(default_factory=list)

    def new_task(self, user: "User", *, budget_cents: int = 300) -> UUID:
        row = self.admin.execute(
            "insert into public.tasks (user_id, short_code, title, spec, status, budget_cents) "
            "values (%s, %s, 'Find a TV', '{}', 'queued', %s) returning id",
            (user.id, f"T{len(self.texts)}{abs(hash(user.id)) % 1000}", budget_cents),
        ).fetchone()
        assert row is not None
        return UUID(str(row["id"]))

    def instruct(self, task_id: UUID, command: str) -> None:
        self.admin.execute(
            "insert into public.instructions (user_id, task_id, command) "
            "select user_id, id, %s from public.tasks where id = %s",
            (command, task_id),
        )

    def status(self, task_id: UUID) -> dict[str, Any]:
        row = self.admin.execute(
            "select status, spent_cents, budget_cents, error_class from public.tasks where id = %s",
            (task_id,),
        ).fetchone()
        assert row is not None
        return row

    def events(self, task_id: UUID) -> list[str]:
        rows = self.admin.execute(
            "select type from public.task_events where task_id = %s order by seq", (task_id,)
        ).fetchall()
        return [r["type"] for r in rows]


async def eventually(check: Callable[[], bool], what: str, timeout: float = 10) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not check():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.05)


def scripted(h: Harness, steps: Sequence[StepResult | Exception]) -> None:
    remaining = list(steps)

    async def executor(task_id: UUID) -> StepResult:
        h.executed.append(task_id)
        await asyncio.sleep(0.05)
        step = remaining.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    h.runtime.executor = executor
