"""Web push: who gets it, once, and what happens when a device is gone or refuses."""

import json
from collections.abc import Callable
from typing import Any
from uuid import UUID

import psycopg
import pytest
from agent_common.contracts import PushSubscribeRequest
from psycopg.rows import DictRow
from pywebpush import WebPushException

from tests.conftest import SignedClient, User
from worker import push
from worker.db import Pool

pytestmark = pytest.mark.db


class Browsers:
    """Stands in for the browser vendors' push services."""

    def __init__(self) -> None:
        self.delivered: list[tuple[str, dict[str, Any]]] = []
        self.gone: set[str] = set()
        self.refusing: set[str] = set()

    async def __call__(self, subscription: dict[str, Any], payload: str) -> None:
        endpoint = subscription["endpoint"]
        if endpoint in self.gone:
            raise push.SubscriptionGoneError(endpoint)
        if endpoint in self.refusing:
            raise WebPushException("rate limited")
        self.delivered.append((endpoint, json.loads(payload)))


def device(admin: psycopg.Connection[DictRow], user: User, name: str) -> str:
    endpoint = f"https://push.example/{name}-{user.id}"
    admin.execute(
        "insert into public.push_subscriptions (user_id, endpoint, p256dh, auth) "
        "values (%s, %s, 'k', 'a')",
        (user.id, endpoint),
    )
    return endpoint


async def test_every_device_of_the_user_gets_the_text_once_per_key(
    pool: Pool, admin: psycopg.Connection[DictRow], make_user: Callable[..., User]
) -> None:
    user, browsers = make_user(), Browsers()
    phone, laptop = device(admin, user, "phone"), device(admin, user, "laptop")

    async with pool.connection() as conn:
        first = await push.push(
            conn, browsers, user_id=user.id, dedupe_key=f"k:{user.id}", body="T7 done"
        )
        again = await push.push(
            conn, browsers, user_id=user.id, dedupe_key=f"k:{user.id}", body="T7 done"
        )

    assert (first, again) == (2, 0)
    assert {e for e, _ in browsers.delivered} == {phone, laptop}
    assert browsers.delivered[0][1] == {"title": push.TITLE, "body": "T7 done", "url": "/"}


async def test_nobody_else_is_pushed_to(
    pool: Pool, admin: psycopg.Connection[DictRow], make_user: Callable[..., User]
) -> None:
    owner, stranger, browsers = make_user(), make_user(), Browsers()
    device(admin, stranger, "theirs")

    async with pool.connection() as conn:
        reached = await push.push(
            conn, browsers, user_id=owner.id, dedupe_key=f"k:{owner.id}", body="x"
        )

    assert reached == 0 and browsers.delivered == []


async def test_a_device_the_vendor_reports_gone_is_forgotten_and_others_still_get_it(
    pool: Pool, admin: psycopg.Connection[DictRow], make_user: Callable[..., User]
) -> None:
    user, browsers = make_user(), Browsers()
    old, current = device(admin, user, "old"), device(admin, user, "current")
    browsers.gone.add(old)

    async with pool.connection() as conn:
        reached = await push.push(
            conn, browsers, user_id=user.id, dedupe_key=f"k:{user.id}", body="x"
        )

    assert reached == 1
    left = admin.execute(
        "select endpoint from public.push_subscriptions where user_id = %s", (user.id,)
    ).fetchall()
    assert [r["endpoint"] for r in left] == [current]


async def test_a_refusal_is_logged_and_does_not_forget_the_device_or_stop_the_rest(
    pool: Pool, admin: psycopg.Connection[DictRow], make_user: Callable[..., User],
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    user, browsers = make_user(), Browsers()
    limited, fine = device(admin, user, "limited"), device(admin, user, "fine")
    browsers.refusing.add(limited)

    async with pool.connection() as conn:
        reached = await push.push(
            conn, browsers, user_id=user.id, dedupe_key=f"k:{user.id}", body="x"
        )

    assert reached == 1 and [e for e, _ in browsers.delivered] == [fine]
    assert "push refused" in caplog.text
    count = admin.execute(
        "select count(*) n from public.push_subscriptions where user_id = %s", (user.id,)
    ).fetchone()
    assert count == {"n": 2}


async def test_the_notifier_texts_and_pushes_the_same_message(
    pool: Pool, admin: psycopg.Connection[DictRow], make_user: Callable[..., User]
) -> None:
    user, browsers, texts = make_user(), Browsers(), []
    device(admin, user, "phone")

    async def text(user_id: UUID, key: str, body: str) -> None:
        texts.append((user_id, key, body))

    notify = push.notifier(pool, browsers, text)
    await notify(user.id, f"alert:{user.id}", "Alert: 3 tasks failed.")
    await notify(user.id, f"alert:{user.id}", "Alert: 3 tasks failed.")  # a repeated step

    assert len(texts) == 2  # the text path dedupes itself; push is once
    assert [p["body"] for _, p in browsers.delivered] == ["Alert: 3 tasks failed."]


async def test_the_owner_registers_and_removes_a_device_from_the_dashboard(
    api: SignedClient, admin: psycopg.Connection[DictRow], make_user: Callable[..., User]
) -> None:
    user = make_user()
    token = api.auth.sign_in(user)
    body = {"endpoint": "https://push.example/abc", "p256dh": "key", "auth": "secret"}

    first = await api.post_as(token, "/v1/push/subscribe", body)
    again = await api.post_as(token, "/v1/push/subscribe", body | {"auth": "rotated"})
    rows = admin.execute(
        "select auth from public.push_subscriptions where user_id = %s", (user.id,)
    ).fetchall()
    removed = await api.post_as(token, "/v1/push/unsubscribe", {"endpoint": body["endpoint"]})

    assert (first.status_code, again.status_code, removed.status_code) == (204, 204, 204)
    assert [r["auth"] for r in rows] == ["rotated"]  # a repeat updates, it does not duplicate
    assert not admin.execute(
        "select 1 from public.push_subscriptions where user_id = %s", (user.id,)
    ).fetchall()


async def test_push_registration_needs_a_full_sign_in_and_an_https_endpoint(
    api: SignedClient, make_user: Callable[..., User]
) -> None:
    user = make_user()
    body = {"endpoint": "https://push.example/abc", "p256dh": "k", "auth": "a"}

    weak = await api.post_as(api.auth.sign_in(user, aal="aal1"), "/v1/push/subscribe", body)
    anonymous = await api.post_as(None, "/v1/push/subscribe", body)
    plain = await api.post_as(
        api.auth.sign_in(user), "/v1/push/subscribe", body | {"endpoint": "http://push.example/x"}
    )

    assert (weak.status_code, anonymous.status_code, plain.status_code) == (403, 401, 422)


def test_a_subscription_request_is_what_the_browser_sends() -> None:
    request = PushSubscribeRequest(endpoint="https://push.example/x", p256dh="k", auth="a")
    assert request.model_dump() == {
        "endpoint": "https://push.example/x",
        "p256dh": "k",
        "auth": "a",
    }
