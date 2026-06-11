"""Logging in with a stored credential: where it may be typed, and where it must never appear."""

import logging
import os
from collections.abc import Callable
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import DictRow

from tests.browser.conftest import Site
from tests.browser.support import InMemorySecretStore, ScriptedDecider, click, events, fill
from tests.conftest import User
from worker import broker
from worker.browser.actions import Done, Navigate, Scroll
from worker.browser.agent import BrowserExecutor
from worker.db import Pool
from worker.engine import TaskFailure
from worker.session_crypto import decrypt_state

pytestmark = [pytest.mark.browser, pytest.mark.db]
Admin = psycopg.Connection[DictRow]
PASSWORD = "correct-horse-battery"
USERNAME = "alice@mail.example"
BACK_OFF = Scroll(kind="scroll", direction="down")  # a harmless second step to observe the page


class Setup:
    def __init__(self, pool: Pool, admin: Admin, user: User, site: Site) -> None:
        self.pool, self.admin, self.user, self.site = pool, admin, user, site
        self.store = InMemorySecretStore()
        self._tasks = 0

    async def account(self, label: str = "main", origins: list[str] | None = None) -> UUID:
        async with self.pool.connection() as conn:
            return await broker.connect_account(
                conn,
                self.store,
                user_id=self.user.id,
                site_domain="shop.example",
                label=label,
                username=USERNAME,
                password=PASSWORD,
                allowed_origins=origins or [self.site.base],
                allow_insecure_origins=True,  # the test site is plain http on localhost
            )

    def task(self, *account_ids: UUID) -> UUID:
        self._tasks += 1
        row = self.admin.execute(
            "insert into public.tasks (user_id, short_code, title, spec, status, budget_cents, "
            "account_ids) values (%s, %s, 'Check my orders', '{}', 'running', 300, %s) "
            "returning id",
            (self.user.id, f"T{self._tasks}", list(account_ids)),
        ).fetchone()
        assert row is not None
        return UUID(str(row["id"]))


@pytest.fixture
def setup(pool: Pool, admin: Admin, make_user: Callable[..., User], site: Site) -> Setup:
    return Setup(pool, admin, make_user(), site)


def executor_for(
    make_executor: Callable[..., BrowserExecutor], decider: ScriptedDecider, setup: Setup
) -> BrowserExecutor:
    return make_executor(decider, setup.store)


async def test_a_login_works_and_the_password_appears_nowhere_the_model_or_logs_can_see(
    setup: Setup,
    make_executor: Callable[..., BrowserExecutor],
    caplog: pytest.LogCaptureFixture,
) -> None:
    account = await setup.account()
    task_id = setup.task(account)
    decider = ScriptedDecider(
        [
            Navigate(kind="navigate", url=setup.site.url("/login")),
            fill("textbox", "Email", account, "username"),
            fill("textbox", "Password", account, "password"),
            click("button", "Sign in"),
            Done(kind="done", result="Logged in."),
        ]
    )
    executor = executor_for(make_executor, decider, setup)
    caplog.set_level(logging.DEBUG)

    for _ in range(5):
        await executor(task_id)

    assert setup.site.app.state.logins[-1] == (USERNAME, PASSWORD)  # the site did receive it
    assert "Welcome alice@mail.example" in decider.briefs[-1].observation.snapshot

    everything_seen = "\n".join(
        [b.observation.snapshot + b.observation.title + " ".join(b.history) for b in decider.briefs]
        + [str(e) for e in events(setup.admin, task_id, "step", "step_failed", "denied")]
        + [r.getMessage() for r in caplog.records]
    )
    assert PASSWORD not in everything_seen
    assert decider.briefs[-1].accounts == (f"{account} - main (shop.example)",)


async def test_a_login_page_on_another_origin_gets_nothing_and_stops_the_task(
    setup: Setup,
    make_executor: Callable[..., BrowserExecutor],
    evil_site: Site,
) -> None:
    account = await setup.account()
    task_id = setup.task(account)
    decider = ScriptedDecider(
        [
            Navigate(kind="navigate", url=evil_site.url("/login")),  # a look-alike login page
            fill("textbox", "Email", account, "username"),
        ]
    )
    executor = executor_for(make_executor, decider, setup)

    await executor(task_id)
    with pytest.raises(TaskFailure) as stopped:
        await executor(task_id)

    assert stopped.value.error_class == "security_incident"
    assert evil_site.app.state.logins == []
    flagged = setup.admin.execute(
        "select metadata->>'detail' as detail from audit.events "
        "where action = 'credential_origin_mismatch' and target_id = %s",
        (str(task_id),),
    ).fetchone()
    assert flagged is not None and "not approved" in flagged["detail"]


async def test_a_login_form_framed_from_another_origin_inside_a_trusted_page_is_refused(
    setup: Setup,
    make_executor: Callable[..., BrowserExecutor],
    evil_site: Site,
) -> None:
    account = await setup.account()
    task_id = setup.task(account)
    framed = setup.site.url(f"/login-framed?src={evil_site.url('/login')}")
    decider = ScriptedDecider(
        [
            Navigate(kind="navigate", url=framed),  # the page is trusted; the form is not
            fill("textbox", "Email", account, "username"),
        ]
    )
    executor = executor_for(make_executor, decider, setup)

    await executor(task_id)
    with pytest.raises(TaskFailure) as stopped:
        await executor(task_id)
    assert stopped.value.error_class == "security_incident"
    assert evil_site.app.state.logins == []


async def test_an_account_the_task_was_not_given_cannot_be_used(
    setup: Setup, make_executor: Callable[..., BrowserExecutor]
) -> None:
    attached, other = await setup.account("work"), await setup.account("personal")
    task_id = setup.task(attached)  # `other` is not part of this task
    decider = ScriptedDecider(
        [
            Navigate(kind="navigate", url=setup.site.url("/login")),
            fill("textbox", "Email", other, "username"),
        ]
    )
    executor = executor_for(make_executor, decider, setup)

    await executor(task_id)
    await executor(task_id)

    denied = events(setup.admin, task_id, "denied")
    assert [d["summary"] for d in denied] == ["refused: that account is not attached to this task"]


async def test_a_task_with_no_accounts_cannot_log_in_at_all(
    setup: Setup, make_executor: Callable[..., BrowserExecutor]
) -> None:
    account = await setup.account()
    task_id = setup.task()  # no accounts attached
    decider = ScriptedDecider(
        [
            Navigate(kind="navigate", url=setup.site.url("/login")),
            fill("textbox", "Email", account, "username"),
        ]
    )
    executor = executor_for(make_executor, decider, setup)
    logins_before = len(setup.site.app.state.logins)
    await executor(task_id)
    await executor(task_id)
    assert len(events(setup.admin, task_id, "denied")) == 1
    assert len(setup.site.app.state.logins) == logins_before


async def test_a_credential_is_only_typed_into_the_kind_of_field_it_belongs_in(
    setup: Setup, make_executor: Callable[..., BrowserExecutor]
) -> None:
    account = await setup.account()
    task_id = setup.task(account)
    decider = ScriptedDecider(
        [
            Navigate(kind="navigate", url=setup.site.url("/search")),
            fill("textbox", "Search", account, "password"),  # a search box is not a password field
        ]
    )
    executor = executor_for(make_executor, decider, setup)
    await executor(task_id)
    await executor(task_id)
    failed = events(setup.admin, task_id, "step_failed")
    assert failed[-1]["summary"] == "could not fill: 'Search' is not a password field"


async def test_a_login_is_saved_encrypted_and_restored_for_the_next_task(
    setup: Setup, make_executor: Callable[..., BrowserExecutor]
) -> None:
    key = os.urandom(32)
    account = await setup.account()
    first_task = setup.task(account)
    first = make_executor(
        ScriptedDecider(
            [
                Navigate(kind="navigate", url=setup.site.url("/login")),
                fill("textbox", "Email", account, "username"),
                fill("textbox", "Password", account, "password"),
                click("button", "Sign in"),
                Done(kind="done", result="Logged in."),
            ]
        ),
        setup.store,
        key,
    )
    for _ in range(5):
        await first(first_task)  # finishing the task releases the browser and saves the login

    blob = setup.admin.execute(
        "select state_ciphertext from public.account_sessions where account_id = %s", (account,)
    ).fetchone()
    assert blob is not None
    assert b"sess-9f3a1c-private" not in bytes(blob["state_ciphertext"])  # unreadable at rest
    saved = decrypt_state(key, account, bytes(blob["state_ciphertext"]))
    assert [c["name"] for c in saved["cookies"]] == ["session"]

    # A later task opens the account page directly and is already signed in.
    second_task = setup.task(account)
    decider = ScriptedDecider([Navigate(kind="navigate", url=setup.site.url("/account")), BACK_OFF])
    second = make_executor(decider, setup.store, key)
    await second(second_task)
    await second(second_task)
    assert "Welcome back" in decider.briefs[-1].observation.snapshot


async def test_a_saved_login_that_cannot_be_decrypted_just_means_logging_in_again(
    setup: Setup, make_executor: Callable[..., BrowserExecutor]
) -> None:
    account = await setup.account()
    setup.admin.execute(
        "insert into public.account_sessions (account_id, user_id, state_ciphertext) "
        "values (%s, %s, %s)",
        (account, setup.user.id, os.urandom(60)),
    )
    task_id = setup.task(account)
    decider = ScriptedDecider([Navigate(kind="navigate", url=setup.site.url("/account")), BACK_OFF])
    executor = make_executor(decider, setup.store, os.urandom(32))
    await executor(task_id)
    await executor(task_id)
    assert "Please sign in" in decider.briefs[-1].observation.snapshot
