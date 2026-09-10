"""Alerts: what raises them, that each is texted once, and that they clear by themselves."""

from collections.abc import Callable
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import DictRow

from tests.conftest import User
from worker import health
from worker.db import Pool

pytestmark = pytest.mark.db


class Texts:
    def __init__(self) -> None:
        self.sent: list[tuple[UUID, str, str]] = []

    async def __call__(self, user_id: UUID, key: str, body: str) -> None:
        self.sent.append((user_id, key, body))


def failed_task(
    admin: psycopg.Connection[DictRow], user: User, error_class: str, n: int, age: str = "0 days"
) -> None:
    for _ in range(n):
        admin.execute(
            "insert into public.tasks (user_id, short_code, title, spec, status, error_class, "
            "budget_cents, updated_at) "
            "values (%s, %s, 't', '{}', 'failed', %s, 300, now() - %s::interval)",
            (user.id, f"F{uuid4().hex[:8]}", error_class, age),
        )


def spend(
    admin: psycopg.Connection[DictRow], user: User, cents: float, age: str = "0 days"
) -> None:
    admin.execute(
        "insert into public.llm_calls (user_id, route, model, input_tokens, output_tokens, "
        "latency_ms, cost_cents, ts) values "
        "(%s, 'browser_decide', 'm', 1, 1, 1, %s, now() - %s::interval)",
        (user.id, cents, age),
    )


async def problems(pool: Pool, user: User) -> dict[str, str]:
    async with pool.connection() as conn:
        return await health.current_problems(conn, user.id)


async def test_a_quiet_system_raises_nothing(pool: Pool, make_user: Callable[..., User]) -> None:
    assert await problems(pool, make_user()) == {}


async def test_three_failed_tasks_in_a_day_is_a_bad_day_but_two_are_not(
    pool: Pool, admin: psycopg.Connection[DictRow], make_user: Callable[..., User]
) -> None:
    user = make_user()
    failed_task(admin, user, "stuck", 2)
    assert "task_failures" not in await problems(pool, user)

    failed_task(admin, user, "unreachable", 1)

    assert (await problems(pool, user))["task_failures"] == "3 tasks failed in the last day"


async def test_old_failures_do_not_count(
    pool: Pool, admin: psycopg.Connection[DictRow], make_user: Callable[..., User]
) -> None:
    user = make_user()
    failed_task(admin, user, "stuck", 5, age="3 days")

    assert await problems(pool, user) == {}


async def test_unexpected_errors_and_security_stops_have_their_own_alerts(
    pool: Pool, admin: psycopg.Connection[DictRow], make_user: Callable[..., User]
) -> None:
    user = make_user()
    failed_task(admin, user, "internal", 2)
    failed_task(admin, user, "security_incident", 1)

    found = await problems(pool, user)

    # Three failures in all, so the bad-day alert fires as well.
    assert set(found) == {"internal_errors", "security_incident", "task_failures"}


@pytest.mark.parametrize("status", ["failed", "opted_out", "no_reachable_number"])
async def test_an_undelivered_text_raises_an_alert(
    pool: Pool, admin: psycopg.Connection[DictRow], make_user: Callable[..., User], status: str
) -> None:
    user = make_user()
    conversation = admin.execute(
        "insert into public.conversations (user_id, channel) values (%s, 'sms') returning id",
        (user.id,),
    ).fetchone()
    assert conversation is not None
    for status_ in (status, "sent"):
        admin.execute(
            "insert into public.messages (conversation_id, user_id, direction, channel, "
            "provider_sid, body_redacted, delivery_status) "
            "values (%s, %s, 'outbound', 'sms', gen_random_uuid()::text, 'x', %s)",
            (conversation["id"], user.id, status_),
        )

    found = await problems(pool, user)

    assert found["sms_delivery"] == (
        f"texts to you were not delivered (1 {status.replace('_', ' ')})"
    )


async def test_cost_alerts_use_the_monthly_cap(
    pool: Pool, admin: psycopg.Connection[DictRow], make_user: Callable[..., User]
) -> None:
    user = make_user()
    admin.execute(
        "insert into public.user_settings (user_id, monthly_cost_cap_cents) values (%s, 1000)",
        (user.id,),
    )
    spend(admin, user, 200)
    assert await problems(pool, user) == {}

    spend(admin, user, 100)  # 300 today: over a quarter of 1000
    assert set(await problems(pool, user)) == {"cost_spike"}

    spend(admin, user, 520, age="2 days")  # 820 this month at least, if still in the month
    found = await problems(pool, user)
    assert "cost_spike" in found


async def test_an_alert_is_texted_once_stays_open_and_clears_when_the_problem_goes(
    pool: Pool, admin: psycopg.Connection[DictRow], make_user: Callable[..., User]
) -> None:
    user, texts = make_user(), Texts()
    failed_task(admin, user, "stuck", 3)

    await health.check_all(pool, texts, "https://dash.example")
    await health.check_all(pool, texts, "https://dash.example")

    mine = [t for t in texts.sent if t[0] == user.id]
    assert [t[2] for t in mine] == ["Alert: 3 tasks failed in the last day. https://dash.example"]
    open_ = admin.execute(
        "select kind from public.alerts where user_id = %s and resolved_at is null", (user.id,)
    ).fetchall()
    assert [a["kind"] for a in open_] == ["task_failures"]

    admin.execute(
        "update public.tasks set updated_at = now() - interval '2 days' where user_id = %s",
        (user.id,),
    )
    await health.check_all(pool, texts, None)
    assert admin.execute(
        "select count(*) n from public.alerts where user_id = %s and resolved_at is null",
        (user.id,),
    ).fetchone() == {"n": 0}

    failed_task(admin, user, "stuck", 3)  # it comes back: a new alert, a new text
    await health.check_all(pool, texts, None)
    assert len([t for t in texts.sent if t[0] == user.id]) == 2


async def test_alerts_are_per_user(
    pool: Pool, admin: psycopg.Connection[DictRow], make_user: Callable[..., User]
) -> None:
    sick, well = make_user(), make_user()
    failed_task(admin, sick, "stuck", 3)

    assert await problems(pool, well) == {}
