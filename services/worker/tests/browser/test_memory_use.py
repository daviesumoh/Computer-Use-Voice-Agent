"""The browser agent starts from what is remembered: preferences and the route that worked."""

from collections.abc import Callable
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import DictRow

from tests.browser.conftest import Site
from tests.browser.support import ScriptedDecider
from tests.conftest import User
from tests.memory_support_db import add_memory
from worker.browser.actions import Done, Navigate
from worker.browser.agent import BrowserExecutor
from worker.db import Pool
from worker.memory import manage
from worker.memory.jobs import Memory

pytestmark = [pytest.mark.browser, pytest.mark.db]


def task_titled(admin: psycopg.Connection[DictRow], user: User, title: str) -> UUID:
    row = admin.execute(
        "insert into public.tasks (user_id, short_code, title, spec, status, budget_cents) "
        "values (%s, 'T1', %s, '{}', 'running', 300) returning id",
        (user.id, title),
    ).fetchone()
    assert row is not None
    return UUID(str(row["id"]))


async def test_the_model_is_given_only_preferences_that_bear_on_the_task(
    make_executor: Callable[..., BrowserExecutor],
    admin: psycopg.Connection[DictRow],
    make_user: Callable[..., User],
    memory: Memory,
) -> None:
    user = make_user()
    add_memory(admin, user.id, "Always buy electronics from Best Buy.")
    add_memory(admin, user.id, "Prefers window seats on flights.")
    add_memory(admin, user.id, "Lives in Toronto.", kind="fact")
    decider = ScriptedDecider([Done(kind="done", result="ok")])

    await make_executor(decider, memory=memory)(task_titled(admin, user, "Buy electronics"))

    assert decider.briefs[0].notes == ("Always buy electronics from Best Buy.",)


async def test_a_paused_memory_gives_the_model_no_notes(
    make_executor: Callable[..., BrowserExecutor],
    admin: psycopg.Connection[DictRow],
    make_user: Callable[..., User],
    memory: Memory,
    pool: Pool,
) -> None:
    user = make_user()
    add_memory(admin, user.id, "Always buy electronics from Best Buy.")
    async with pool.connection() as conn:
        await manage.set_enabled(conn, user.id, False)
    decider = ScriptedDecider([Done(kind="done", result="ok")])

    await make_executor(decider, memory=memory)(task_titled(admin, user, "Buy electronics"))

    assert decider.briefs[0].notes == ()


async def test_a_remembered_route_is_offered_once_the_browser_is_on_that_site(
    make_executor: Callable[..., BrowserExecutor],
    admin: psycopg.Connection[DictRow],
    make_user: Callable[..., User],
    memory: Memory,
    site: Site,
) -> None:
    user = make_user()
    admin.execute(
        "insert into public.procedures (user_id, domain, task_type, steps, success_count, "
        "last_success_at) "
        "values (%s, '127.0.0.1', 'read', %s::jsonb, 1, now())",
        (user.id, '["opened /products", "clicked link \'Cirrus 65 TV\'"]'),
    )
    decider = ScriptedDecider(
        [Navigate(kind="navigate", url=site.url("/products")), Done(kind="done", result="ok")]
    )
    executor, task_id = make_executor(decider, memory=memory), task_titled(admin, user, "Find a TV")

    await executor(task_id)
    await executor(task_id)

    assert decider.briefs[0].route == ""  # the blank start page is on no site yet
    assert "clicked link 'Cirrus 65 TV'" in decider.briefs[1].route


async def test_without_memory_the_browser_behaves_as_before(
    make_executor: Callable[..., BrowserExecutor],
    admin: psycopg.Connection[DictRow],
    make_user: Callable[..., User],
) -> None:
    user = make_user()
    add_memory(admin, user.id, "Always buy electronics from Best Buy.")
    decider = ScriptedDecider([Done(kind="done", result="ok")])

    await make_executor(decider)(task_titled(admin, user, "Buy electronics"))

    assert (decider.briefs[0].notes, decider.briefs[0].route) == ((), "")
