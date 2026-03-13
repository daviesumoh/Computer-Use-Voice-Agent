"""The task engine against real Postgres and a real DBOS runtime; only the executor is scripted."""

import asyncio
from collections.abc import Callable
from uuid import UUID

import pytest

from tests.conftest import User
from tests.engine_harness import Harness, eventually, scripted
from worker import limits
from worker.engine import StepResult, TaskFailure

pytestmark = pytest.mark.db


async def test_a_task_runs_its_steps_and_completes_with_one_text(
    harness: Harness, make_user: Callable[..., User]
) -> None:
    scripted(
        harness,
        [StepResult(10), StepResult(15), StepResult(5, finished=True, summary="Top pick: X.")],
    )
    task_id = harness.new_task(make_user())

    await harness.engine.enqueue(task_id)
    await eventually(lambda: harness.status(task_id)["status"] == "completed", "completion")

    await eventually(lambda: bool(harness.texts), "the completion text")
    assert harness.status(task_id)["spent_cents"] == 30
    assert harness.events(task_id) == ["started", "completed"]
    assert [body.split(" ", 1)[1] for _, body in harness.texts] == ["done. Top pick: X."]


async def test_enqueueing_the_same_task_twice_runs_it_once(
    harness: Harness, make_user: Callable[..., User]
) -> None:
    scripted(harness, [StepResult(1, finished=True)])
    task_id = harness.new_task(make_user())

    await asyncio.gather(harness.engine.enqueue(task_id), harness.engine.enqueue(task_id))
    await eventually(lambda: harness.status(task_id)["status"] == "completed", "completion")

    assert len(harness.executed) == 1


async def test_pause_holds_the_task_at_a_safe_point_and_resume_continues_it(
    harness: Harness, make_user: Callable[..., User]
) -> None:
    scripted(harness, [StepResult(1)] * 40 + [StepResult(1, finished=True)])
    task_id = harness.new_task(make_user())
    await harness.engine.enqueue(task_id)
    await eventually(lambda: len(harness.executed) >= 2, "first steps")

    harness.instruct(task_id, "pause")
    await harness.engine.wake(task_id)
    await eventually(lambda: harness.status(task_id)["status"] == "paused", "pause")
    steps_when_paused = len(harness.executed)
    await asyncio.sleep(0.6)
    assert len(harness.executed) == steps_when_paused  # nothing runs while paused

    harness.instruct(task_id, "resume")
    await harness.engine.wake(task_id)
    await eventually(lambda: harness.status(task_id)["status"] == "completed", "completion")
    assert harness.events(task_id)[:3] == ["started", "paused", "resumed"]


async def test_kill_stops_a_running_task_for_good(
    harness: Harness, make_user: Callable[..., User]
) -> None:
    scripted(harness, [StepResult(1)] * 100)
    task_id = harness.new_task(make_user())
    await harness.engine.enqueue(task_id)
    await eventually(lambda: len(harness.executed) >= 2, "first steps")

    harness.instruct(task_id, "cancel")
    await eventually(lambda: harness.status(task_id)["status"] == "cancelled", "cancellation")
    steps = len(harness.executed)
    await asyncio.sleep(0.4)
    assert len(harness.executed) == steps
    assert any("cancelled" in body for _, body in harness.texts)


async def test_budget_exhaustion_pauses_and_resume_grants_one_more_allowance(
    harness: Harness, make_user: Callable[..., User]
) -> None:
    scripted(harness, [StepResult(30), StepResult(30), StepResult(30, finished=True)])
    task_id = harness.new_task(make_user(), budget_cents=50)
    await harness.engine.enqueue(task_id)

    await eventually(lambda: harness.status(task_id)["status"] == "paused", "budget pause")
    assert harness.status(task_id)["spent_cents"] == 60
    assert harness.events(task_id) == ["started", "budget_exhausted"]

    harness.instruct(task_id, "resume")
    await harness.engine.wake(task_id)
    await eventually(lambda: harness.status(task_id)["status"] == "completed", "completion")
    assert harness.status(task_id)["budget_cents"] == 360  # spent 60 + the 300 default allowance


async def test_a_failing_executor_fails_the_task_with_its_class_and_one_text(
    harness: Harness, make_user: Callable[..., User]
) -> None:
    scripted(harness, [TaskFailure("site_unreachable", "bestbuy.ca did not load")])
    task_id = harness.new_task(make_user())
    await harness.engine.enqueue(task_id)

    await eventually(lambda: harness.status(task_id)["status"] == "failed", "failure")
    await eventually(lambda: bool(harness.texts), "the failure text")  # sent just after the status
    assert harness.status(task_id)["error_class"] == "site_unreachable"
    assert [body.split(": ", 1)[1] for _, body in harness.texts] == ["bestbuy.ca did not load"]


async def test_an_unexpected_error_still_ends_the_task_instead_of_leaving_it_running(
    harness: Harness, make_user: Callable[..., User]
) -> None:
    scripted(harness, [RuntimeError("boom")])
    task_id = harness.new_task(make_user())
    await harness.engine.enqueue(task_id)

    await eventually(lambda: harness.status(task_id)["status"] == "failed", "failure")
    assert harness.status(task_id)["error_class"] == "internal"


async def test_emergency_stop_pauses_running_tasks_and_blocks_resume(
    harness: Harness, make_user: Callable[..., User]
) -> None:
    user = make_user()
    scripted(harness, [StepResult(1)] * 100)
    task_id = harness.new_task(user)
    await harness.engine.enqueue(task_id)
    await eventually(lambda: len(harness.executed) >= 2, "first steps")

    harness.admin.execute(
        "insert into public.user_settings (user_id, emergency_stop) values (%s, true)", (user.id,)
    )
    await eventually(lambda: harness.status(task_id)["status"] == "paused", "emergency pause")

    harness.instruct(task_id, "resume")
    await harness.engine.wake(task_id)
    await eventually(
        lambda: any("Emergency stop is on" in body for _, body in harness.texts), "refusal text"
    )
    assert harness.status(task_id)["status"] == "paused"


async def test_a_task_cancelled_before_its_turn_never_starts(
    harness: Harness, make_user: Callable[..., User]
) -> None:
    scripted(harness, [StepResult(1, finished=True)])
    task_id = harness.new_task(make_user())
    harness.admin.execute("update public.tasks set status = 'cancelled' where id = %s", (task_id,))

    await harness.engine.enqueue(task_id)
    await asyncio.sleep(0.5)
    assert harness.executed == []
    assert harness.status(task_id)["status"] == "cancelled"


async def test_a_task_pauses_when_the_monthly_cap_is_reached_and_stays_paused_on_resume(
    harness: Harness, make_user: Callable[..., User]
) -> None:
    scripted(harness, [StepResult(1, finished=True)])
    user = make_user()
    harness.admin.execute(
        "insert into public.user_settings (user_id, monthly_cost_cap_cents) values (%s, 100)",
        (user.id,),
    )
    harness.admin.execute(
        "insert into public.llm_calls (user_id, route, model, input_tokens, output_tokens, "
        "latency_ms, cost_cents) values (%s, 'browser_decide', 'm', 1, 1, 1, 150)",
        (user.id,),
    )
    task_id = harness.new_task(user)

    await harness.engine.enqueue(task_id)
    await eventually(lambda: harness.status(task_id)["status"] == "paused", "the pause")
    harness.instruct(task_id, "resume")
    await harness.engine.wake(task_id)
    await eventually(
        lambda: any("cap is reached; T" in body for _, body in harness.texts), "the refusal"
    )

    assert harness.executed == []  # nothing ran past the cap
    assert harness.status(task_id)["status"] == "paused"
    assert "monthly_cap" in harness.events(task_id)
    assert any("monthly spending cap is reached" in body for _, body in harness.texts)


async def test_a_task_pauses_after_its_working_time_and_resume_grants_more(
    harness: Harness, make_user: Callable[..., User], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(limits, "MAX_ACTIVE_SECONDS", 1)

    async def slow_step(task_id: UUID) -> StepResult:
        harness.executed.append(task_id)
        await asyncio.sleep(1.1)
        return StepResult(1, finished=len(harness.executed) == 2)

    harness.runtime.executor = slow_step
    task_id = harness.new_task(make_user())

    await harness.engine.enqueue(task_id)
    await eventually(lambda: harness.status(task_id)["status"] == "paused", "the time-limit pause")
    assert harness.events(task_id)[-1] == "time_limit"

    harness.instruct(task_id, "resume")
    await harness.engine.wake(task_id)
    await eventually(lambda: harness.status(task_id)["status"] == "completed", "completion")
