"""SMS in and out over Twilio. Outbound sends are deduplicated; inbound webhooks too."""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

import httpx

from worker.db import Conn, Pool
from worker.settings import Settings
from worker.transcripts import redact_for_storage

log = logging.getLogger(__name__)
TWILIO_API = "https://api.twilio.com/2010-04-01"


class SmsSendError(Exception):
    def __init__(self, status_code: int, twilio_code: int | None, message: str) -> None:
        super().__init__(f"Twilio HTTP {status_code} (code {twilio_code}): {message}")
        self.twilio_code = twilio_code


async def send_via_twilio(client: httpx.AsyncClient, settings: Settings, to: str, body: str) -> str:
    """Returns the Twilio message SID."""
    response = await client.post(
        f"{TWILIO_API}/Accounts/{settings.twilio_account_sid}/Messages.json",
        data={"To": to, "From": settings.twilio_from_number, "Body": body},
        auth=(settings.twilio_api_key_sid, settings.twilio_api_key_secret.get_secret_value()),
    )
    if response.status_code >= 300:
        try:
            error = response.json()
            code, message = error.get("code"), str(error.get("message", ""))
        except ValueError:
            code, message = None, response.text[:200]
        raise SmsSendError(response.status_code, code, message)
    return str(response.json()["sid"])


async def _sms_conversation(conn: Conn, user_id: UUID) -> UUID:
    """The user's single open SMS thread (a unique partial index guarantees there is one)."""
    row = await (
        await conn.execute(
            """
            insert into public.conversations (user_id, channel) values (%s, 'sms')
            on conflict (user_id) where channel = 'sms' and ended_at is null
            do update set channel = excluded.channel
            returning id
            """,
            (user_id,),
        )
    ).fetchone()
    assert row is not None
    return UUID(str(row["id"]))


async def notify(
    conn: Conn,
    client: httpx.AsyncClient,
    settings: Settings,
    *,
    user_id: UUID,
    dedupe_key: str,
    body: str,
) -> str:
    """Texts the user once per dedupe_key. Returns the resulting delivery status."""
    conversation_id = await _sms_conversation(conn, user_id)
    # Recorded before sending, so a retry after a crash finds it and does not text twice.
    claimed = await (
        await conn.execute(
            """
            insert into public.messages
              (conversation_id, user_id, direction, channel, body_redacted, dedupe_key,
               delivery_status)
            values (%s, %s, 'outbound', 'sms', %s, %s, 'sending')
            on conflict (dedupe_key) do nothing
            returning id
            """,
            (conversation_id, user_id, redact_for_storage(body), dedupe_key),
        )
    ).fetchone()
    if claimed is None:
        return "duplicate"

    phone = await (
        await conn.execute(
            "select e164 from public.phone_numbers "
            "where user_id = %s and is_primary and verified_at is not null and not sms_opted_out",
            (user_id,),
        )
    ).fetchone()
    if phone is None:
        status, sid = "no_reachable_number", None
    else:
        try:
            sid, status = await send_via_twilio(client, settings, phone["e164"], body), "sent"
        except (SmsSendError, httpx.HTTPError) as exc:
            log.warning("sms send failed", extra={"dedupe_key": dedupe_key, "error": str(exc)})
            # 21610: the recipient texted STOP; Twilio refuses until they text START.
            opted_out = isinstance(exc, SmsSendError) and exc.twilio_code == 21610
            status, sid = ("opted_out" if opted_out else "failed"), None
            if opted_out:
                await conn.execute(
                    "update public.phone_numbers set sms_opted_out = true where e164 = %s",
                    (phone["e164"],),
                )

    await conn.execute(
        "update public.messages set delivery_status = %s, provider_sid = %s where id = %s",
        (status, sid, claimed["id"]),
    )
    return status


@dataclass(frozen=True)
class InboundMessage:
    id: UUID
    user_id: UUID
    is_new: bool


async def record_inbound(
    conn: Conn, *, message_sid: str, from_number: str, body: str
) -> InboundMessage | None:
    """Stores an inbound text from a verified user. None for unknown senders.

    A redelivery returns the original message with is_new False.
    """
    owner = await (
        await conn.execute(
            "select user_id from public.phone_numbers where e164 = %s and verified_at is not null",
            (from_number,),
        )
    ).fetchone()
    if owner is None:
        await conn.execute(
            "insert into audit.events (actor, action, metadata) "
            "values ('worker', 'sms_rejected', jsonb_build_object('number_suffix', %s::text))",
            (from_number[-4:],),
        )
        return None

    user_id = owner["user_id"]
    conversation_id = await _sms_conversation(conn, user_id)
    stored = await (
        await conn.execute(
            """
            insert into public.messages
              (conversation_id, user_id, direction, channel, provider_sid, body_redacted)
            values (%s, %s, 'inbound', 'sms', %s, %s)
            on conflict (provider_sid) do nothing
            returning id
            """,
            (conversation_id, user_id, message_sid, redact_for_storage(body)),
        )
    ).fetchone()
    if stored:
        return InboundMessage(stored["id"], user_id, is_new=True)
    existing = await (
        await conn.execute("select id from public.messages where provider_sid = %s", (message_sid,))
    ).fetchone()
    assert existing is not None
    return InboundMessage(existing["id"], user_id, is_new=False)


def notifier(
    settings: Settings, pool: Pool, client: httpx.AsyncClient
) -> Callable[[UUID, str, str], Awaitable[None]]:
    """`notify` bound to a pool, for callers (the API, the task engine) that hold no connection."""

    async def send(user_id: UUID, dedupe_key: str, body: str) -> None:
        async with pool.connection() as conn:
            await notify(conn, client, settings, user_id=user_id, dedupe_key=dedupe_key, body=body)

    return send
