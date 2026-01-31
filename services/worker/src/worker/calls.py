"""Voice-call bookkeeping: who is calling, the conversation record, and its turn history."""

from datetime import timedelta
from typing import Any
from uuid import UUID

from agent_common.contracts import (
    CallStartResponse,
    MemoryCard,
    MemoryQuestion,
    ResumeContext,
    TaskSummary,
)
from psycopg.types.json import Jsonb

from worker.db import Conn
from worker.memory import recall
from worker.transcripts import redact_for_storage

# A call back within this window resumes the previous conversation instead of starting fresh.
RESUME_WINDOW = timedelta(minutes=30)
MAX_ACTIVE_TASKS_SPOKEN = 5


async def start_call(conn: Conn, *, call_sid: str, caller_number: str) -> CallStartResponse | None:
    """Returns None for numbers that aren't a verified user's phone."""
    owner = await (
        await conn.execute(
            """
            select p.id, p.display_name, p.timezone
            from public.phone_numbers n join public.profiles p on p.id = n.user_id
            where n.e164 = %s and n.verified_at is not null
            """,
            (caller_number,),
        )
    ).fetchone()
    if owner is None:
        # Caller ID can be spoofed and is personal data: log only the last digits.
        await conn.execute(
            "insert into audit.events (actor, action, metadata) "
            "values ('voice', 'call_rejected', %s)",
            (Jsonb({"reason": "unknown_caller", "number_suffix": caller_number[-4:]}),),
        )
        return None

    # Upsert: LiveKit may dispatch the same call twice; it must map to one conversation.
    conversation = await (
        await conn.execute(
            """
            insert into public.conversations (user_id, channel, provider_call_sid)
            values (%s, 'voice', %s)
            on conflict (provider_call_sid)
              do update set provider_call_sid = excluded.provider_call_sid
            returning id
            """,
            (owner["id"], call_sid),
        )
    ).fetchone()
    assert conversation is not None

    tasks = await (
        await conn.execute(
            """
            select short_code, title, status from public.tasks
            where user_id = %s and status in ('queued', 'running', 'paused', 'waiting_user')
            order by priority desc, created_at desc
            limit %s
            """,
            (owner["id"], MAX_ACTIVE_TASKS_SPOKEN),
        )
    ).fetchall()

    previous = await (
        await conn.execute(
            """
            select id, ended_at, state from public.conversations
            where user_id = %s and channel = 'voice' and id <> %s
              and ended_at > now() - %s
            order by ended_at desc
            limit 1
            """,
            (owner["id"], conversation["id"], RESUME_WINDOW),
        )
    ).fetchone()

    sites = await (
        await conn.execute(
            "select distinct site_domain from public.connected_accounts "
            "where user_id = %s and status = 'active' order by site_domain",
            (owner["id"],),
        )
    ).fetchall()

    card = (
        await recall.profile_card(conn, owner["id"])
        if await recall.enabled(conn, owner["id"])
        else None
    )

    return CallStartResponse(
        conversation_id=conversation["id"],
        display_name=owner["display_name"],
        timezone=owner["timezone"],
        active_tasks=[TaskSummary(**t) for t in tasks],
        connected_sites=[s["site_domain"] for s in sites],
        memory=MemoryCard(
            lines=card.lines,
            to_confirm=[MemoryQuestion(id=q.id, text=q.text) for q in card.to_confirm],
            changes=card.changes,
        )
        if card
        else MemoryCard(),
        resume=(
            ResumeContext(
                conversation_id=previous["id"],
                ended_at=previous["ended_at"],
                state=previous["state"],
            )
            if previous
            else None
        ),
    )


async def owner_of(conn: Conn, conversation_id: UUID) -> UUID | None:
    row = await (
        await conn.execute(
            "select user_id from public.conversations where id = %s and channel = 'voice'",
            (conversation_id,),
        )
    ).fetchone()
    return row["user_id"] if row else None


async def record_turn(
    conn: Conn,
    *,
    conversation_id: UUID,
    turn_index: int,
    speaker: str,
    text: str,
    asr_confidence: float | None,
    state: dict[str, Any],
) -> bool:
    """Stores one spoken turn and the conversation state after it. False if the call is unknown.

    Retries are harmless: the turn's identity (conversation, index, speaker) is its provider_sid.
    """
    updated = await (
        await conn.execute(
            "update public.conversations set state = %s where id = %s and channel = 'voice' "
            "returning user_id",
            (Jsonb(state), conversation_id),
        )
    ).fetchone()
    if updated is None:
        return False
    await conn.execute(
        """
        insert into public.messages
          (conversation_id, user_id, direction, channel, provider_sid, body_redacted,
           asr_confidence, turn_index)
        values (%s, %s, %s, 'voice', %s, %s, %s, %s)
        on conflict (provider_sid) do nothing
        """,
        (
            conversation_id,
            updated["user_id"],
            "inbound" if speaker == "user" else "outbound",
            f"voice:{conversation_id}:{turn_index}:{speaker}",
            redact_for_storage(text),
            asr_confidence,
            turn_index,
        ),
    )
    return True


async def end_call(conn: Conn, *, conversation_id: UUID, reason: str) -> bool:
    """Ending a call never touches its tasks: hanging up is not cancelling."""
    ended = await (
        await conn.execute(
            """
            update public.conversations
            set ended_at = coalesce(ended_at, now()), end_reason = coalesce(end_reason, %s)
            where id = %s and channel = 'voice'
            returning id
            """,
            (reason, conversation_id),
        )
    ).fetchone()
    return ended is not None
