import asyncio
from collections.abc import Callable
from uuid import UUID, uuid4

import psycopg
import pytest
from agent_common.contracts import CreateTaskResponse
from psycopg.rows import DictRow

from tests.conftest import WORKER_DSN, User
from worker import tasks
from worker.db import Pool

pytestmark = pytest.mark.db


async def test_same_request_racing_past_the_existence_check_yields_one_task(
    pool: Pool, make_user: Callable[..., User], admin: psycopg.Connection[DictRow]
) -> None:
    user = make_user()
    conversation = admin.execute(
        "insert into public.conversations (user_id, channel) values (%s, 'voice') returning id",
        (user.id,),
    ).fetchone()
    assert conversation is not None
    request_id = uuid4()

    async def create() -> CreateTaskResponse:
        async with pool.connection() as conn:
            return await tasks.create_task(
                conn,
                request_id=request_id,
                conversation_id=conversation["id"],
                title="Race",
                spec={},
                sites=[],
            )

    # Hold the profile row lock so both creations pass the "already exists?" check and then
    # queue on the short-code UPDATE; the loser must hit the unique request_id and recover.
    async with (
        await psycopg.AsyncConnection.connect(WORKER_DSN) as blocker,
        blocker.transaction(),
    ):
        await blocker.execute("select 1 from public.profiles where id = %s for update", (user.id,))
        racing = [asyncio.create_task(create()) for _ in range(2)]
        await asyncio.sleep(0.3)  # both are now waiting on the lock
        assert not any(t.done() for t in racing)

    first, second = await asyncio.gather(*racing)

    assert first.task_id == second.task_id
    assert sorted([first.created, second.created]) == [False, True]
    count = admin.execute(
        "select count(*) as n from public.tasks where request_id = %s", (request_id,)
    ).fetchone()
    assert count == {"n": 1}


def add_account(admin: psycopg.Connection[DictRow], user: User, domain: str) -> UUID:
    row = admin.execute(
        "insert into public.connected_accounts (user_id, site_domain, allowed_origins, label, "
        "secret_ref) values (%s, %s, %s, 'main', 'ref') returning id",
        (user.id, domain, [f"https://{domain}"]),
    ).fetchone()
    assert row is not None
    return UUID(str(row["id"]))


def conversation_for(admin: psycopg.Connection[DictRow], user: User) -> UUID:
    row = admin.execute(
        "insert into public.conversations (user_id, channel) values (%s, 'voice') returning id",
        (user.id,),
    ).fetchone()
    assert row is not None
    return UUID(str(row["id"]))


async def test_a_task_is_given_exactly_the_accounts_for_the_sites_the_caller_named(
    pool: Pool, make_user: Callable[..., User], admin: psycopg.Connection[DictRow]
) -> None:
    user, other = make_user(), make_user()
    wanted = add_account(admin, user, "bestbuy.ca")
    add_account(admin, user, "walmart.ca")  # connected, but not asked for
    add_account(admin, other, "bestbuy.ca")  # someone else's login on the same site

    async with pool.connection() as conn:
        created = await tasks.create_task(
            conn,
            request_id=uuid4(),
            conversation_id=conversation_for(admin, user),
            title="Check my order",
            spec={},
            sites=["BestBuy.ca"],
        )

    row = admin.execute("select account_ids from public.tasks where id = %s", (created.task_id,))
    assert row.fetchone() == {"account_ids": [wanted]}


async def test_naming_a_site_with_no_connected_login_starts_nothing(
    pool: Pool, make_user: Callable[..., User], admin: psycopg.Connection[DictRow]
) -> None:
    user = make_user()
    async with pool.connection() as conn:
        with pytest.raises(tasks.UnknownSiteError, match=r"amazon\.ca"):
            await tasks.create_task(
                conn,
                request_id=uuid4(),
                conversation_id=conversation_for(admin, user),
                title="Check my order",
                spec={},
                sites=["amazon.ca"],
            )
    count = admin.execute("select count(*) as n from public.tasks where user_id = %s", (user.id,))
    assert count.fetchone() == {"n": 0}
