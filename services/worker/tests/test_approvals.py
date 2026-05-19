import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import DictRow

from tests.conftest import User
from worker import approvals, ledger
from worker.db import Pool

pytestmark = pytest.mark.db
Admin = psycopg.Connection[DictRow]


def make_task(admin: Admin, user: User, status: str = "waiting_user") -> UUID:
    reason = "approval" if status == "waiting_user" else None
    row = admin.execute(
        "insert into public.tasks (user_id, short_code, title, spec, status, waiting_reason, "
        "budget_cents) values (%s, 'T1', 'Buy a TV', '{}', %s, %s, 300) returning id",
        (user.id, status, reason),
    ).fetchone()
    assert row is not None
    return UUID(str(row["id"]))


async def ask(pool: Pool, user: User, task_id: UUID, hash_: str = "h1") -> UUID:
    async with pool.connection() as conn:
        return await approvals.request(
            conn,
            user_id=user.id,
            task_id=task_id,
            action_class="purchase",
            summary="click 'Place order'",
            hash_=hash_,
        )


def fresh() -> datetime:
    return datetime.now(UTC) - timedelta(seconds=30)


def status(admin: Admin, approval_id: UUID) -> str:
    row = admin.execute("select status from public.approvals where id = %s", (approval_id,))
    found = row.fetchone()
    assert found is not None
    return str(found["status"])


def test_the_hash_changes_when_the_page_total_or_the_target_changes() -> None:
    task = uuid4()

    def h(
        snapshot: str = "total $214.37", name: str = "Place order", url: str = "https://x.ca/c"
    ) -> str:
        return approvals.intent_hash(task, "purchase", "click", "button", name, url, snapshot)

    assert h() == h()
    assert h() != h(snapshot="total $219.99")
    assert h() != h(name="Place order (gift)")
    assert h() != h(url="https://x.ca/other")
    assert h(url="https://x.ca/c?session=1") == h(url="https://x.ca/c?session=2")  # query ignored
    assert h(snapshot="a $1.00 b $1.00") == h(snapshot="$1.00")  # same figures, same hash


async def test_approving_needs_a_recent_totp_and_returns_the_task_to_work(
    pool: Pool, make_user: Callable[..., User], admin: Admin
) -> None:
    user = make_user()
    task_id = make_task(admin, user)
    approval_id = await ask(pool, user, task_id)

    async with pool.connection() as conn:
        for stale in (None, datetime.now(UTC) - timedelta(minutes=6)):
            with pytest.raises(approvals.StaleMfaError):
                await approvals.decide(
                    conn, approval_id, user_id=user.id, decision="approved", mfa_verified_at=stale
                )
    assert status(admin, approval_id) == "pending"

    async with pool.connection() as conn:
        await approvals.decide(
            conn, approval_id, user_id=user.id, decision="approved", mfa_verified_at=fresh()
        )
    assert status(admin, approval_id) == "approved"
    task = admin.execute("select status from public.tasks where id = %s", (task_id,)).fetchone()
    assert task == {"status": "running"}
    audit = admin.execute(
        "select 1 from audit.events where target_id = %s and action = 'approval_approved'",
        (str(approval_id),),
    )
    assert audit.fetchone() is not None


async def test_denying_needs_no_fresh_code(
    pool: Pool, make_user: Callable[..., User], admin: Admin
) -> None:
    user = make_user()
    approval_id = await ask(pool, user, make_task(admin, user))
    async with pool.connection() as conn:
        await approvals.decide(
            conn, approval_id, user_id=user.id, decision="denied", mfa_verified_at=None
        )
    assert status(admin, approval_id) == "denied"


async def test_nobody_else_can_decide_and_a_decision_cannot_be_made_twice(
    pool: Pool, make_user: Callable[..., User], admin: Admin
) -> None:
    owner, other = make_user(), make_user()
    approval_id = await ask(pool, owner, make_task(admin, owner))
    async with pool.connection() as conn:
        with pytest.raises(approvals.UnknownApprovalError):
            await approvals.decide(
                conn, approval_id, user_id=other.id, decision="approved", mfa_verified_at=fresh()
            )
        await approvals.decide(
            conn, approval_id, user_id=owner.id, decision="denied", mfa_verified_at=None
        )
        with pytest.raises(approvals.NotPendingError) as again:
            await approvals.decide(
                conn, approval_id, user_id=owner.id, decision="approved", mfa_verified_at=fresh()
            )
    assert again.value.status == "denied"


async def test_an_expired_request_cannot_be_approved(
    pool: Pool, make_user: Callable[..., User], admin: Admin
) -> None:
    user = make_user()
    approval_id = await ask(pool, user, make_task(admin, user))
    admin.execute(
        "update public.approvals set expires_at = now() - interval '1 minute' where id = %s",
        (approval_id,),
    )
    async with pool.connection() as conn:
        with pytest.raises(approvals.NotPendingError) as late:
            await approvals.decide(
                conn, approval_id, user_id=user.id, decision="approved", mfa_verified_at=fresh()
            )
    assert late.value.status == "expired"
    assert status(admin, approval_id) == "expired"


async def test_an_approval_can_be_spent_exactly_once_even_when_two_steps_race(
    pool: Pool, make_user: Callable[..., User], admin: Admin
) -> None:
    user = make_user()
    approval_id = await ask(pool, user, make_task(admin, user))
    async with pool.connection() as conn:
        await approvals.decide(
            conn, approval_id, user_id=user.id, decision="approved", mfa_verified_at=fresh()
        )

    async def spend() -> bool:
        async with pool.connection() as conn:
            return await approvals.consume(conn, approval_id)

    results = await asyncio.gather(*(spend() for _ in range(5)))
    assert sorted(results) == [False] * 4 + [True]


async def test_asking_twice_for_the_same_action_reuses_the_open_request(
    pool: Pool, make_user: Callable[..., User], admin: Admin
) -> None:
    user = make_user()
    task_id = make_task(admin, user)
    await ask(pool, user, task_id)
    async with pool.connection() as conn:
        live = await approvals.find_live(conn, task_id, "h1")
        assert live is not None and live["status"] == "pending"
        with pytest.raises(psycopg.errors.UniqueViolation):
            await approvals.request(
                conn, user_id=user.id, task_id=task_id, action_class="purchase",
                summary="again", hash_="h1",
            )  # fmt: skip


async def test_the_ledger_lets_one_attempt_through_and_frees_the_slot_only_if_it_never_happened(
    pool: Pool, make_user: Callable[..., User], admin: Admin
) -> None:
    user = make_user()
    task_id = make_task(admin, user, "running")

    async def claim() -> int | None:
        async with pool.connection() as conn:
            return await ledger.start(
                conn, task_id=task_id, user_id=user.id, action_class="purchase",
                description="Place order", intent_hash="h1",
            )  # fmt: skip

    first, second = await asyncio.gather(claim(), claim())
    assert (first is None) != (second is None)  # exactly one wins the race
    winner = first or second
    assert winner is not None

    async with pool.connection() as conn:
        await ledger.finish(conn, winner, "unknown")
    assert await claim() is None  # may have happened: no silent retry

    async with pool.connection() as conn:
        await ledger.finish(conn, winner, "not_happened")
    assert await claim() is not None  # proven not done: a retry is allowed
