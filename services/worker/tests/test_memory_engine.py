"""Learning jobs run through the durable engine: after a task ends and after a call hangs up."""

from collections.abc import Callable

import pytest

from tests.conftest import User
from tests.engine_harness import Harness, eventually, scripted
from tests.memory_support import ScriptedMemoryModel, rule, says
from worker.engine import StepResult, TaskFailure
from worker.memory.jobs import Memory

pytestmark = pytest.mark.db


def episodes(h: Harness, user: User) -> list[str]:
    rows = h.admin.execute(
        "select summary from public.episodes where user_id = %s", (user.id,)
    ).fetchall()
    return [r["summary"] for r in rows]


async def test_a_finished_task_is_remembered_by_a_background_job(
    harness: Harness,
    make_user: Callable[..., User],
    memory: Memory,
) -> None:
    harness.runtime.memory = memory
    scripted(harness, [StepResult(1, finished=True, summary="Top pick: X.")])
    user = make_user()
    task_id = harness.new_task(user)

    await harness.engine.enqueue(task_id)

    await eventually(lambda: bool(episodes(harness, user)), "the task's episode")
    assert episodes(harness, user) == ["Task 'Find a TV'. It completed."]


async def test_a_failed_task_is_remembered_too(
    harness: Harness,
    make_user: Callable[..., User],
    memory: Memory,
) -> None:
    harness.runtime.memory = memory
    scripted(harness, [TaskFailure("stuck", "no progress")])
    user = make_user()
    task_id = harness.new_task(user)

    await harness.engine.enqueue(task_id)

    await eventually(lambda: bool(episodes(harness, user)), "the task's episode")
    assert episodes(harness, user) == ["Task 'Find a TV'. It failed (stuck)."]


async def test_a_hung_up_call_is_learned_from_once_even_if_reported_twice(
    harness: Harness,
    make_user: Callable[..., User],
    memory: Memory,
    scripted: ScriptedMemoryModel,
) -> None:
    harness.runtime.memory = memory
    scripted.candidates = [rule("Always buy electronics from Best Buy.", "always use Best Buy")]
    user = make_user()
    call, _ = says(harness.admin, user.id, "always use Best Buy for electronics")

    await harness.engine.remember_conversation(call)
    await harness.engine.remember_conversation(call)

    await eventually(lambda: bool(episodes(harness, user)), "the call's episode")
    count = harness.admin.execute(
        "select count(*) n from public.memories where user_id = %s", (user.id,)
    ).fetchone()
    assert count == {"n": 1}


async def test_without_memory_configured_nothing_is_queued(
    harness: Harness,
    make_user: Callable[..., User],
) -> None:
    user = make_user()
    call, _ = says(harness.admin, user.id, "always use Best Buy for electronics")

    await harness.engine.remember_conversation(call)  # no memory on the runtime: a no-op

    rows = harness.admin.execute(
        "select count(*) n from dbos.workflow_status where workflow_uuid = %s",
        (f"learn-call:{call}",),
    ).fetchone()
    assert rows == {"n": 0}
