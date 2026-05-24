"""A task that reaches a purchase waits for the owner, acts once on a yes, never on a no."""

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import DictRow

from tests.browser.conftest import Site
from tests.browser.support import ScriptedDecider, click, events
from tests.conftest import WORKER_DSN, User
from tests.engine_harness import eventually
from worker import approvals
from worker.browser.actions import Done, Navigate
from worker.browser.agent import BrowserExecutor
from worker.db import Pool
from worker.engine import Engine, Runtime, WaitForUser

pytestmark = [pytest.mark.browser, pytest.mark.db]
Admin = psycopg.Connection[DictRow]
BUY = click("button", "Buy now")


@dataclass
class Rig:
    engine: Engine
    runtime: Runtime
    pool: Pool
    admin: Admin
    texts: list[str] = field(default_factory=list)
    executor: BrowserExecutor | None = None

    def use(self, decider: ScriptedDecider) -> None:
        executor = BrowserExecutor(
            self.pool, decider, headless=True, allow_private_hosts=True, action_timeout_ms=2_000
        )
        self.executor = executor
        self.runtime.executor, self.runtime.release = executor, executor.release

    def task(self, user: User) -> UUID:
        row = self.admin.execute(
            "insert into public.tasks (user_id, short_code, title, spec, status, budget_cents) "
            "values (%s, 'T3', 'Buy the TV', '{}', 'queued', 300) returning id",
            (user.id,),
        ).fetchone()
        assert row is not None
        return UUID(str(row["id"]))

    def row(self, task_id: UUID) -> dict[str, Any]:
        found = self.admin.execute(
            "select status, waiting_reason from public.tasks where id = %s", (task_id,)
        ).fetchone()
        assert found is not None
        return found

    def pending(self, task_id: UUID) -> UUID:
        found = self.admin.execute(
            "select id from public.approvals where task_id = %s and status = 'pending'", (task_id,)
        ).fetchone()
        assert found is not None
        return UUID(str(found["id"]))

    async def decide(self, user: User, approval_id: UUID, decision: str) -> None:
        async with self.pool.connection() as conn:
            await approvals.decide(
                conn,
                approval_id,
                user_id=user.id,
                decision=decision,  # type: ignore[arg-type]
                mfa_verified_at=datetime.now(UTC),
            )


@pytest.fixture
async def rig(pool: Pool, admin: Admin) -> AsyncIterator[Rig]:
    if admin.execute("select to_regclass('dbos.workflow_status') as t").fetchone() == {
        "t": "dbos.workflow_status"
    }:
        admin.execute("truncate dbos.workflow_status cascade")
    texts: list[str] = []

    async def notify(user_id: UUID, dedupe_key: str, body: str) -> None:
        texts.append(body)

    runtime = Runtime(
        pool=pool, notify=notify, poll_seconds=0.2, dashboard_url="https://dash.example"
    )
    engine = Engine(runtime, WORKER_DSN)
    await engine.start()
    yield Rig(engine, runtime, pool, admin, texts)
    engine.stop()


def waiting_for_approval(rig: Rig, task_id: UUID) -> bool:
    return rig.row(task_id) == {"status": "waiting_user", "waiting_reason": "approval"}


async def test_a_yes_lets_the_purchase_happen_exactly_once(
    rig: Rig, make_user: Callable[..., User], site: Site
) -> None:
    user = make_user()
    rig.use(
        ScriptedDecider(
            [
                Navigate(kind="navigate", url=site.url("/inject")),
                BUY,
                BUY,
                Done(kind="done", result="Bought it."),
            ]
        )
    )
    task_id, before = rig.task(user), len(site.app.state.purchases)

    await rig.engine.enqueue(task_id)
    await eventually(lambda: waiting_for_approval(rig, task_id), "the approval request", 30)
    assert len(site.app.state.purchases) == before  # nothing happened while waiting
    # The checkout page the owner is deciding about stays open while the task waits.
    assert rig.executor is not None and rig.executor.session_for(task_id) is not None
    await eventually(  # the text goes out just after the status changes
        lambda: any(
            t.startswith("T3 needs you:") and t.endswith(f"https://dash.example/tasks/{task_id}")
            for t in rig.texts
        ),
        "the approval text",
    )

    await rig.decide(user, rig.pending(task_id), "approved")
    await eventually(lambda: rig.row(task_id)["status"] == "completed", "completion", 30)

    assert len(site.app.state.purchases) == before + 1
    used = rig.admin.execute("select status from public.approvals where task_id = %s", (task_id,))
    assert used.fetchall() == [{"status": "used"}]
    ledger = rig.admin.execute(
        "select action_class, phase from public.browser_actions where task_id = %s", (task_id,)
    )
    assert ledger.fetchall() == [{"action_class": "purchase", "phase": "confirmed"}]


async def test_a_no_means_nothing_is_bought_and_the_model_is_told(
    rig: Rig, make_user: Callable[..., User], site: Site
) -> None:
    user = make_user()
    decider = ScriptedDecider(
        [
            Navigate(kind="navigate", url=site.url("/inject")),
            BUY,
            Done(kind="done", result="You declined, so I stopped."),
        ]
    )
    rig.use(decider)
    task_id, before = rig.task(user), len(site.app.state.purchases)

    await rig.engine.enqueue(task_id)
    await eventually(lambda: waiting_for_approval(rig, task_id), "the approval request", 30)
    await rig.decide(user, rig.pending(task_id), "denied")
    await eventually(lambda: rig.row(task_id)["status"] == "completed", "completion", 30)

    assert len(site.app.state.purchases) == before
    assert decider.briefs[-1].history[-1].startswith("owner denied:")


async def test_an_unanswered_request_expires_and_the_task_carries_on(
    rig: Rig, make_user: Callable[..., User], site: Site
) -> None:
    rig.use(
        ScriptedDecider(
            [
                Navigate(kind="navigate", url=site.url("/inject")),
                BUY,
                Done(kind="done", result="Skipped the purchase."),
            ]
        )
    )
    task_id = rig.task(make_user())
    await rig.engine.enqueue(task_id)
    await eventually(lambda: waiting_for_approval(rig, task_id), "the approval request", 30)

    rig.admin.execute(
        "update public.approvals set expires_at = %s where task_id = %s",
        (datetime.now(UTC) - timedelta(minutes=1), task_id),
    )
    await eventually(lambda: rig.row(task_id)["status"] == "completed", "completion", 30)

    states = rig.admin.execute("select status from public.approvals where task_id = %s", (task_id,))
    assert states.fetchall() == [{"status": "expired"}]
    assert "approval_expired" in [e["type"] for e in events(rig.admin, task_id, "approval_expired")]


async def test_an_approval_does_not_carry_over_when_the_price_changes(
    rig: Rig, make_user: Callable[..., User], site: Site
) -> None:
    user = make_user()
    rig.use(
        ScriptedDecider(
            [
                Navigate(kind="navigate", url=site.url("/priced?total=214.37")),
                BUY,
                Navigate(kind="navigate", url=site.url("/priced?total=219.99")),
                BUY,
            ]
        )
    )
    task_id, before = rig.task(user), len(site.app.state.purchases)
    await rig.engine.enqueue(task_id)
    await eventually(lambda: waiting_for_approval(rig, task_id), "first request", 30)
    first = rig.pending(task_id)

    await rig.decide(user, first, "approved")
    # The page now shows a different total, so the yes for $214.37 must not be spent on it.
    await eventually(lambda: waiting_for_approval(rig, task_id), "second request", 30)

    assert rig.pending(task_id) != first
    assert len(site.app.state.purchases) == before
    first_state = rig.admin.execute("select status from public.approvals where id = %s", (first,))
    assert first_state.fetchone() == {"status": "approved"}  # unspent


async def test_a_purchase_whose_outcome_is_unknown_is_asked_about_and_never_repeated(
    pool: Pool,
    make_executor: Callable[..., BrowserExecutor],
    new_task: Callable[[], UUID],
    make_user: Callable[..., User],
    admin: Admin,
    site: Site,
) -> None:
    # The site takes the order but answers after the click gives up: the classic silent submit.
    place = click("button", "Place order")
    decider = ScriptedDecider(
        [Navigate(kind="navigate", url=site.url("/slow-checkout")), place, place, place, place]
    )
    executor, task_id = make_executor(decider), new_task()
    user_id = admin.execute("select user_id from public.tasks where id = %s", (task_id,)).fetchone()
    assert user_id is not None
    owner = User(user_id["user_id"], "")
    before = len(site.app.state.purchases)
    site.app.state.order_gate.clear()

    async def approve_pending() -> None:
        found = admin.execute(
            "select id from public.approvals where task_id = %s and status = 'pending'", (task_id,)
        ).fetchone()
        assert found is not None
        async with pool.connection() as conn:
            await approvals.decide(
                conn,
                found["id"],
                user_id=owner.id,
                decision="approved",
                mfa_verified_at=datetime.now(UTC),
            )

    async def until_parked() -> WaitForUser:
        # After a hung page the executor first spends a step re-opening its browser.
        for _ in range(3):
            try:
                await executor(task_id)
            except WaitForUser as parked:
                return parked
        raise AssertionError("the task never parked")

    assert (await until_parked()).reason == "approval"
    await approve_pending()
    unsure = await until_parked()  # clicks; the page never answers in time
    assert unsure.reason == "clarify"
    assert "could not confirm" in unsure.message

    ledger = admin.execute(
        "select phase from public.browser_actions where task_id = %s", (task_id,)
    )
    assert ledger.fetchall() == [{"phase": "unknown"}]

    # Even with a fresh yes, the ledger refuses to run the same purchase a second time.
    assert (await until_parked()).reason == "approval"
    await approve_pending()
    await executor(task_id)
    assert events(admin, task_id, "step_failed")[-1]["summary"].startswith("not repeated")

    site.app.state.order_gate.set()  # the site finishes processing the one order that was placed
    await asyncio.sleep(1)
    assert len(site.app.state.purchases) == before + 1
