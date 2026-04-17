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

    failed = events(admin, task_id, "step_failed")
    assert len(failed) == 1 and failed[0]["summary"].startswith("could not click")
    assert decider.briefs[2].history[-1].startswith("could not click")  # the model was told
    assert events(admin, task_id, "step")[-1]["summary"].endswith("/products")


async def test_repeating_the_same_action_ends_the_task(
    make_executor: Callable[..., BrowserExecutor], new_task: Callable[[], UUID], site: Site
) -> None:
    scroll = Scroll(kind="scroll", direction="down")
    executor = make_executor(
        ScriptedDecider([Navigate(kind="navigate", url=site.url("/products")), *[scroll] * 5])
    )
    task_id = new_task()
    for _ in range(4):
        await executor(task_id)
    with pytest.raises(TaskFailure) as stopped:
        await executor(task_id)
    assert stopped.value.error_class == "loop_detected"


async def test_a_run_of_steps_that_cannot_act_ends_the_task_as_stuck(
    make_executor: Callable[..., BrowserExecutor], new_task: Callable[[], UUID], site: Site
) -> None:
    plan: list[Plan] = [Navigate(kind="navigate", url=site.url("/overlay"))]
    plan += [
        Type(kind="type", ref="e999", text=str(i)) for i in range(5)
    ]  # a ref that is not there
    executor, task_id = make_executor(ScriptedDecider(plan)), new_task()
    for _ in range(6):
        await executor(task_id)
    with pytest.raises(TaskFailure) as stopped:
        await executor(task_id)
    assert stopped.value.error_class == "stuck"


async def test_a_site_that_demands_human_verification_parks_the_task_with_its_browser_open(
    make_executor: Callable[..., BrowserExecutor], new_task: Callable[[], UUID], site: Site
) -> None:
    executor = make_executor(
        ScriptedDecider([Navigate(kind="navigate", url=site.url("/challenge"))])
    )
    task_id = new_task()
    with pytest.raises(WaitForUser) as parked:
        await executor(task_id)
    assert parked.value.reason == "login"
    # The owner will take over this very page, so the browser must still be there.
    session = executor.session_for(task_id)
    assert session is not None
    assert session.page.url.endswith("/challenge")


async def test_a_restarted_worker_resumes_on_the_page_where_the_task_was(
    make_executor: Callable[..., BrowserExecutor],
    new_task: Callable[[], UUID],
    site: Site,
) -> None:
    task_id = new_task()
    first = make_executor(ScriptedDecider([Navigate(kind="navigate", url=site.url("/product/1"))]))
    await first(task_id)
    await first.release(task_id)  # the process dies: its browser and memory are gone

    decider = ScriptedDecider([Back(kind="back")])
    await make_executor(decider)(task_id)  # a new process, same database

    resumed = decider.briefs[0]
    assert resumed.observation.url.endswith("/product/1")
    assert resumed.history == [f"opened {site.url('/product/1')}"]


async def test_a_destination_on_the_internal_network_is_refused_for_good(
    pool: Pool, new_task: Callable[[], UUID]
) -> None:
    executor = BrowserExecutor(
        pool,
        ScriptedDecider(
            [Navigate(kind="navigate", url="http://169.254.169.254/latest/meta-data/")]
        ),
        headless=True,
        retry_delays=(0, 0),
    )
    task_id = new_task()
    try:
        with pytest.raises(TaskFailure) as refused:
            await executor(task_id)
        assert refused.value.error_class == "unreachable"
    finally:
        await executor.release(task_id)


async def test_the_engine_drives_a_browser_task_to_a_texted_answer(
    pool: Pool,
    make_user: Callable[..., User],
    admin: psycopg.Connection[DictRow],
    site: Site,
) -> None:
    if admin.execute("select to_regclass('dbos.workflow_status') as t").fetchone() == {
        "t": "dbos.workflow_status"
    }:
        admin.execute("truncate dbos.workflow_status cascade")
    texts: list[str] = []

    async def notify(user_id: UUID, dedupe_key: str, body: str) -> None:
        texts.append(body)

    decider = ScriptedDecider(
        [
            Navigate(kind="navigate", url=site.url("/products")),
            Done(kind="done", result="Cheapest: Borealis 50 TV at $429.00"),
        ]
    )
    executor = BrowserExecutor(pool, decider, headless=True, allow_private_hosts=True)
    runtime = Runtime(pool=pool, notify=notify, executor=executor, release=executor.release)
    engine = Engine(runtime, WORKER_DSN)
    await engine.start()
    try:
        row = admin.execute(
            "insert into public.tasks (user_id, short_code, title, spec, status, budget_cents) "
            "values (%s, 'T9', 'Cheapest TV', '{}', 'queued', 300) returning id",
            (make_user().id,),
        ).fetchone()
        assert row is not None
        task_id = UUID(str(row["id"]))

        await engine.enqueue(task_id)
        await eventually(
            lambda: (
                admin.execute(
                    "select status from public.tasks where id = %s", (task_id,)
                ).fetchone()
                == {"status": "completed"}
            ),
            "completion",
            timeout=30,
        )
    finally:
        engine.stop()

    assert texts == ["T9 done. Cheapest: Borealis 50 TV at $429.00"]
    spent = admin.execute("select spent_cents from public.tasks where id = %s", (task_id,))
    assert spent.fetchone() == {"spent_cents": 2}  # two model calls, a cent each
    assert executor._runs == {}


async def test_a_browser_that_dies_mid_task_is_replaced_and_the_task_carries_on(
    make_executor: Callable[..., BrowserExecutor],
    new_task: Callable[[], UUID],
    site: Site,
    admin: psycopg.Connection[DictRow],
) -> None:
    decider = ScriptedDecider(
        [
            Navigate(kind="navigate", url=site.url("/products")),
            Done(kind="done", result="Found them."),
        ]
    )
    executor, task_id = make_executor(decider), new_task()
    await executor(task_id)
    browser = executor._runs[task_id].session._browser
    assert browser is not None
    await browser.close()  # what a crashed Chromium looks like to the worker

    lost = await executor(task_id)
    finished = await executor(task_id)

    assert (lost.cost_cents, lost.finished) == (0, False)
    assert finished.finished and finished.summary == "Found them."
    failed = events(admin, task_id, "step_failed")
    assert [f["summary"].startswith("lost the page") for f in failed] == [True]
    # The new browser went back to where the old one was.
    assert decider.briefs[-1].observation.url == site.url("/products")


async def test_a_browser_left_behind_by_an_ended_task_is_closed_and_live_ones_are_not(
    make_executor: Callable[..., BrowserExecutor],
    new_task: Callable[[], UUID],
    site: Site,
    admin: psycopg.Connection[DictRow],
) -> None:
    executor = make_executor(
        ScriptedDecider([Navigate(kind="navigate", url=site.url("/products"))] * 2)
    )
    ended, running = new_task(), new_task()
    await executor(ended)
    await executor(running)
    admin.execute("update public.tasks set status = 'cancelled' where id = %s", (ended,))

    closed = await executor.reap()

    assert closed == 1
    assert set(executor._runs) == {running}
    assert await executor.reap() == 0


async def test_what_the_owner_said_by_text_or_voice_reaches_the_model(
    make_executor: Callable[..., BrowserExecutor],
    new_task: Callable[[], UUID],
    admin: psycopg.Connection[DictRow],
) -> None:
    task_id = new_task()
    admin.execute(
        "update public.tasks set spec = spec || %s::jsonb where id = %s",
        ('{"owner_edits": [{"channel": "sms", "text": "skip Amazon"}]}', task_id),
    )
    for seq, (kind, summary) in enumerate(
        [
            ("owner_edit", "the owner said by text: skip Amazon"),
            ("owner_answer", "the owner answered: no, it failed"),
        ],
        1,
    ):
        admin.execute(
            "insert into public.task_events (task_id, user_id, seq, type, payload) "
            "select id, user_id, %s, %s, %s::jsonb from public.tasks where id = %s",
            (seq, kind, f'{{"summary": "{summary}"}}', task_id),
        )
    decider = ScriptedDecider([Done(kind="done", result="ok")])

    await make_executor(decider)(task_id)

    brief = decider.briefs[0]
    assert brief.spec["owner_edits"][0]["text"] == "skip Amazon"
    assert brief.history == [
        "the owner said by text: skip Amazon",
        "the owner answered: no, it failed",
    ]
