"""Web push to the browsers the owner enabled alerts on, alongside the text message."""

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from agent_common.contracts import PushSubscribeRequest
from pywebpush import WebPushException, webpush_async

from worker.db import Conn, Pool
from worker.settings import Settings

log = logging.getLogger(__name__)

TITLE = "Computer Use Agent"
TTL_SECONDS = 3600  # an alert older than an hour is not worth showing late

Subscription = dict[str, Any]
Sender = Callable[[Subscription, str], Awaitable[None]]


class SubscriptionGoneError(Exception):
    """The browser vendor says this subscription no longer exists (HTTP 404 or 410)."""


def web_push_sender(settings: Settings) -> Sender:
    if settings.vapid_private_key is None or settings.vapid_subject is None:
        raise ValueError("web push needs the VAPID settings")
    private_key = settings.vapid_private_key.get_secret_value()
    claims = {"sub": settings.vapid_subject}

    async def send(subscription: Subscription, payload: str) -> None:
        try:
            await webpush_async(
                subscription_info=subscription,
                data=payload,
                vapid_private_key=private_key,
                vapid_claims=dict(claims),
                ttl=TTL_SECONDS,
            )
        except WebPushException as refused:
            response = refused.response
            status = getattr(response, "status", None) or getattr(response, "status_code", None)
            if status in {404, 410}:
                raise SubscriptionGoneError(subscription["endpoint"]) from refused
            raise

    return send


async def subscribe(conn: Conn, user_id: UUID, request: PushSubscribeRequest) -> None:
    await conn.execute(
        "insert into public.push_subscriptions (user_id, endpoint, p256dh, auth) "
        "values (%s, %s, %s, %s) on conflict (endpoint) do update "
        "set user_id = excluded.user_id, p256dh = excluded.p256dh, auth = excluded.auth",
        (user_id, request.endpoint, request.p256dh, request.auth),
    )


async def unsubscribe(conn: Conn, user_id: UUID, endpoint: str) -> None:
    await conn.execute(
        "delete from public.push_subscriptions where user_id = %s and endpoint = %s",
        (user_id, endpoint),
    )


async def push(conn: Conn, send: Sender, *, user_id: UUID, dedupe_key: str, body: str) -> int:
    """Pushes the text to each of the user's devices, once per dedupe_key. Returns how many
    devices were reached. A device the vendor reports as gone is forgotten; any other refusal
    is logged and the remaining devices are still tried."""
    claimed = await (
        await conn.execute(
            "insert into public.push_log (dedupe_key) values (%s) "
            "on conflict do nothing returning dedupe_key",
            (dedupe_key,),
        )
    ).fetchone()
    if claimed is None:
        return 0
    devices = await (
        await conn.execute(
            "select endpoint, p256dh, auth from public.push_subscriptions where user_id = %s",
            (user_id,),
        )
    ).fetchall()
    payload = json.dumps({"title": TITLE, "body": body, "url": "/"})
    reached = 0
    for device in devices:
        subscription = {
            "endpoint": device["endpoint"],
            "keys": {"p256dh": device["p256dh"], "auth": device["auth"]},
        }
        try:
            await send(subscription, payload)
            reached += 1
        except SubscriptionGoneError:
            await unsubscribe(conn, user_id, device["endpoint"])
        except WebPushException as refused:
            log.warning("push refused", extra={"error": str(refused)[:200]})
    return reached


def notifier(
    pool: Pool, send: Sender, text: Callable[[UUID, str, str], Awaitable[None]]
) -> Callable[[UUID, str, str], Awaitable[None]]:
    """Texts and pushes the same message. Both are idempotent per dedupe_key."""

    async def both(user_id: UUID, dedupe_key: str, body: str) -> None:
        await text(user_id, dedupe_key, body)
        async with pool.connection() as conn:
            await push(conn, send, user_id=user_id, dedupe_key=dedupe_key, body=body)

    return both
