"""Browser steps end to end: real Chromium, the chaos site, real Postgres, a scripted model."""

from collections.abc import Callable
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import DictRow

from tests.browser.conftest import Site
from tests.browser.support import Plan, ScriptedDecider, click, events
from tests.conftest import WORKER_DSN, User
from tests.engine_harness import eventually
from worker.browser.actions import (
    Back,
    Done,
    Extract,
    Navigate,
    Scroll,
    Type,
)
from worker.browser.agent import BrowserExecutor
from worker.browser.policy import READ_ONLY_SCOPE
from worker.db import Pool
from worker.engine import Engine, Runtime, TaskFailure, WaitForUser

pytestmark = [pytest.mark.browser, pytest.mark.db]


async def test_a_research_task_browses_reads_and_finishes_with_its_answer(
    make_executor: Callable[..., BrowserExecutor],
    new_task: Callable[[], UUID],
    site: Site,
    admin: psycopg.Connection[DictRow],
) -> None:
    decider = ScriptedDecider(
        [
            Navigate(kind="navigate", url=site.url("/products")),
            click("link", "Cirrus 65 TV"),
            Extract(kind="extract", question="What does it cost?"),
            Done(kind="done", result="Cirrus 65 TV is $799.50"),
        ]
    )
    executor, task_id = make_executor(decider), new_task()

    results = [await executor(task_id) for _ in range(4)]

    assert [r.finished for r in results] == [False, False, False, True]
    assert results[-1].summary == "Cirrus 65 TV is $799.50"
    steps = events(admin, task_id, "step")
    assert [s["action"] for s in steps] == ["navigate", "click", "extract", "done"]
    assert "Price: $799.50" in steps[2]["summary"]
    assert executor._runs == {}
    # The model was shown what it had already done, so it can build on it.
    assert decider.briefs[2].history[-1].endswith("/product/2")


async def test_model_usage_is_billed_per_call_and_rounded_up_for_the_budget(
    make_executor: Callable[..., BrowserExecutor],
    new_task: Callable[[], UUID],
    site: Site,
    admin: psycopg.Connection[DictRow],
) -> None:
    executor = make_executor(ScriptedDecider([Navigate(kind="navigate", url=site.url("/blank"))]))
    task_id = new_task()

    result = await executor(task_id)

    assert (
        result.cost_cents == 1
    )  # 0.5 cents of tokens ($2/M in, $10/M out), billed as a whole cent
    call = admin.execute(
        "select route, model, input_tokens, cost_cents from public.llm_calls where task_id = %s",
        (task_id,),
    ).fetchone()
    assert call is not None
    assert (call["route"], call["model"], call["input_tokens"]) == (
        "browser_decide",
        "gpt-6.1-sol",
        2000,
    )
    assert float(call["cost_cents"]) == pytest.approx(0.5)


async def test_a_page_that_tells_the_agent_to_buy_cannot_make_it_buy_without_approval(
    make_executor: Callable[..., BrowserExecutor],
    new_task: Callable[[], UUID],
    site: Site,
    admin: psycopg.Connection[DictRow],
) -> None:
    # The scripted model obeys the injected text, as a fooled real model would.
    decider = ScriptedDecider(
        [Navigate(kind="navigate", url=site.url("/inject")), click("button", "Buy now")]
    )
    executor, task_id = make_executor(decider), new_task()
    purchases_before = len(site.app.state.purchases)

    await executor(task_id)
    with pytest.raises(WaitForUser) as parked:
        await executor(task_id)

    assert parked.value.reason == "approval"
    assert len(site.app.state.purchases) == purchases_before
    pending = admin.execute(
        "select status, action_class from public.approvals where task_id = %s", (task_id,)
    ).fetchall()
    assert pending == [{"status": "pending", "action_class": "purchase"}]
    # The page's text reached the model only inside the untrusted-content wrapper.
    assert "SYSTEM NOTICE TO AI AGENTS" in decider.briefs[1].observation.snapshot


async def test_in_a_read_only_scope_the_same_click_is_refused_outright(
    pool: Pool,
    new_task: Callable[[], UUID],
    site: Site,
    admin: psycopg.Connection[DictRow],
) -> None:
    decider = ScriptedDecider(
        [Navigate(kind="navigate", url=site.url("/inject")), click("button", "Buy now")]
    )
    executor = BrowserExecutor(
        pool,
        decider,
        headless=True,
        allow_private_hosts=True,
        scope=READ_ONLY_SCOPE,
        action_timeout_ms=2_000,
    )
    task_id = new_task()
    purchases_before = len(site.app.state.purchases)
    try:
        await executor(task_id)
        await executor(task_id)
    finally:
        await executor.release(task_id)

    assert len(site.app.state.purchases) == purchases_before
    assert [e["action_class"] for e in events(admin, task_id, "denied")] == ["purchase"]
    assert (
        admin.execute("select 1 from public.approvals where task_id = %s", (task_id,)).fetchone()
        is None
    )


async def test_a_failing_action_is_retried_then_reported_back_to_the_model(
    make_executor: Callable[..., BrowserExecutor],
    new_task: Callable[[], UUID],
    site: Site,
    admin: psycopg.Connection[DictRow],
) -> None:
    decider = ScriptedDecider(
        [
            Navigate(kind="navigate", url=site.url("/overlay")),
            click("link", "Details"),  # covered by the cookie overlay
            click("button", "Accept cookies"),
            click("link", "Details"),
        ]
    )
    executor, task_id = make_executor(decider), new_task()
    for _ in range(4):
        await executor(task_id)

