from collections.abc import Callable
from uuid import UUID

import psycopg
import pytest
from psycopg.errors import InsufficientPrivilege, UniqueViolation
from psycopg.rows import DictRow

from tests.browser.support import InMemorySecretStore
from tests.conftest import User
from worker import broker
from worker.db import Pool

pytestmark = pytest.mark.db
Admin = psycopg.Connection[DictRow]


async def connect(pool: Pool, store: InMemorySecretStore, user: User, **overrides: object) -> UUID:
    args: dict[str, object] = {
        "site_domain": "shop.example",
        "label": "main",
        "username": "alice@mail.example",
        "password": "correct-horse-battery",
    }
    args.update(overrides)
    async with pool.connection() as conn:
        return await broker.connect_account(conn, store, user_id=user.id, **args)  # type: ignore[arg-type]


async def test_connecting_keeps_the_password_in_the_store_and_only_a_hint_in_the_database(
    pool: Pool, make_user: Callable[..., User], admin: Admin
) -> None:
    store, user = InMemorySecretStore(), make_user()
    account_id = await connect(pool, store, user)

    row = admin.execute(
        "select * from public.connected_accounts where id = %s", (account_id,)
    ).fetchone()
    assert row is not None
    assert row["username_hint"] == "al***@mail.example"
    assert row["allowed_origins"] == ["https://shop.example", "https://www.shop.example"]
    assert "correct-horse-battery" not in str(row)
    assert "correct-horse-battery" in store.secrets[row["secret_ref"]]
    audit = admin.execute(
        "select 1 from audit.events where target_id = %s and action = 'account_connected'",
        (str(account_id),),
    )
    assert audit.fetchone() is not None


async def test_the_dashboard_role_can_list_accounts_but_never_read_the_secret_reference(
    pool: Pool, make_user: Callable[..., User], admin: Admin
) -> None:
    await connect(pool, InMemorySecretStore(), make_user())
    with psycopg.connect(
        "postgresql://postgres:postgres@127.0.0.1:55322/postgres", autocommit=True
    ) as conn:
        conn.execute("set role authenticated")
        conn.execute("select id, site_domain, label from public.connected_accounts")  # allowed
        with pytest.raises(InsufficientPrivilege):
            conn.execute("select secret_ref from public.connected_accounts")
        with pytest.raises(InsufficientPrivilege):
            conn.execute("select state_ciphertext from public.account_sessions")


@pytest.mark.parametrize(
    "origins",
    [
        ["https://evil.example"],
        ["https://shop.example.evil.example"],  # looks like the site, is not
        ["https://notshop.example"],
        ["http://shop.example"],  # not https
        ["https://shop.example/login"],  # not a bare origin
        [],
    ],
)
async def test_origins_that_are_not_https_on_the_sites_own_domain_are_refused(
    pool: Pool, make_user: Callable[..., User], origins: list[str]
) -> None:
    store = InMemorySecretStore()
    with pytest.raises(broker.InvalidAccountError):
        await connect(pool, store, make_user(), allowed_origins=origins)
    assert store.secrets == {}  # nothing was saved


async def test_a_failed_save_does_not_leave_the_credential_behind(
    pool: Pool, make_user: Callable[..., User]
) -> None:
    store, user = InMemorySecretStore(), make_user()
    await connect(pool, store, user)
    with pytest.raises(UniqueViolation):
        await connect(pool, store, user)  # same site and label
    assert len(store.secrets) == 1


async def test_disconnecting_deletes_the_credential_the_session_and_the_row(
    pool: Pool, make_user: Callable[..., User], admin: Admin
) -> None:
    store, user = InMemorySecretStore(), make_user()
    account_id = await connect(pool, store, user)
    admin.execute(
        "insert into public.account_sessions (account_id, user_id, state_ciphertext) "
        "values (%s, %s, '\\x00')",
        (account_id, user.id),
    )
    async with pool.connection() as conn:
        await broker.disconnect_account(conn, store, user_id=user.id, account_id=account_id)

    assert store.secrets == {}
    left = admin.execute(
        "select 1 from public.account_sessions where account_id = %s", (account_id,)
    )
    assert left.fetchone() is None


async def test_disconnect_that_cannot_reach_the_store_keeps_the_account_for_a_retry(
    pool: Pool, make_user: Callable[..., User], admin: Admin
) -> None:
    store, user = InMemorySecretStore(), make_user()
    account_id = await connect(pool, store, user)
    store.fail_delete = True
    async with pool.connection() as conn:
        with pytest.raises(Exception, match="outage"):
            await broker.disconnect_account(conn, store, user_id=user.id, account_id=account_id)
    row = admin.execute("select 1 from public.connected_accounts where id = %s", (account_id,))
    assert row.fetchone() is not None


async def test_nobody_can_disconnect_someone_elses_account(
    pool: Pool, make_user: Callable[..., User]
) -> None:
    store, owner, other = InMemorySecretStore(), make_user(), make_user()
    account_id = await connect(pool, store, owner)
    async with pool.connection() as conn:
        with pytest.raises(broker.UnknownAccountError):
            await broker.disconnect_account(conn, store, user_id=other.id, account_id=account_id)
    assert len(store.secrets) == 1
