"""Approvals: the owner's explicit yes (or no) to one specific action on one specific page."""

import contextlib
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from worker.db import Conn
from worker.transitions import InvalidTransitionError, transition

APPROVAL_TTL = timedelta(minutes=30)
MFA_MAX_AGE = timedelta(minutes=5)  # a yes must follow a TOTP code entered this recently
_MONEY = re.compile(r"\$\s?\d[\d,]*(?:\.\d{1,2})?")


class ApprovalError(Exception):
    pass


class UnknownApprovalError(ApprovalError):
    pass


class NotPendingError(ApprovalError):
    """Already decided, used or expired; carries the status it has."""

    def __init__(self, status: str) -> None:
        super().__init__(f"approval is {status}")
        self.status = status


class StaleMfaError(ApprovalError):
    pass


def intent_hash(
    task_id: UUID, action_class: str, kind: str, role: str, name: str, url: str, snapshot: str
) -> str:
    """Identifies this action on this page. If a price or the page changes, so does the hash,
    and an earlier approval no longer applies."""
    parts = urlsplit(url)
    figures = sorted(set(_MONEY.findall(snapshot)))
    material = [str(task_id), action_class, kind, role, name, parts.netloc + parts.path, figures]
    return hashlib.sha256(json.dumps(material).encode()).hexdigest()


async def find_live(conn: Conn, task_id: UUID, hash_: str) -> dict[str, Any] | None:
    """The pending or approved request for this exact action, if one exists and has not expired."""
    return await (
        await conn.execute(
            "select id, status from public.approvals where task_id = %s and intent_hash = %s "
            "and status in ('pending', 'approved') and expires_at > now()",
            (task_id, hash_),
        )
    ).fetchone()


async def request(
    conn: Conn, *, user_id: UUID, task_id: UUID, action_class: str, summary: str, hash_: str
) -> UUID:
    row = await (
        await conn.execute(
            "insert into public.approvals (user_id, task_id, action_class, summary, intent_hash, "
            "expires_at) values (%s, %s, %s, %s, %s, now() + %s) returning id",
            (user_id, task_id, action_class, summary, hash_, APPROVAL_TTL),
        )
    ).fetchone()
    assert row is not None
    return UUID(str(row["id"]))


async def consume(conn: Conn, approval_id: UUID) -> bool:
    """Spends an approval. True only for the one caller that gets to act on it."""
    row = await (
        await conn.execute(
            "update public.approvals set status = 'used' "
            "where id = %s and status = 'approved' and expires_at > now() returning id",
            (approval_id,),
        )
    ).fetchone()
    return row is not None


async def decide(
    conn: Conn,
    approval_id: UUID,
    *,
    user_id: UUID,
    decision: Literal["approved", "denied"],
    mfa_verified_at: datetime | None,
) -> None:
    """Records the owner's decision and returns the waiting task to running."""
    now = datetime.now(UTC)
    if decision == "approved" and (
        mfa_verified_at is None
        or not now - MFA_MAX_AGE <= mfa_verified_at <= now + timedelta(seconds=30)
    ):
        raise StaleMfaError("approving needs a TOTP code entered in the last 5 minutes")
    async with conn.transaction():
        row = await (
            await conn.execute(
                "select task_id, status, expires_at <= now() as expired, summary "
                "from public.approvals where id = %s and user_id = %s for update",
                (approval_id, user_id),
            )
        ).fetchone()
        if row is None:
            raise UnknownApprovalError(str(approval_id))
        if row["status"] == "pending" and row["expired"]:
            await conn.execute(
                "update public.approvals set status = 'expired' where id = %s", (approval_id,)
            )
            lapsed = True
        else:
            lapsed = False
            if row["status"] == "pending":
                await conn.execute(
                    "update public.approvals set status = %s, decided_at = now(), "
                    "mfa_verified_at = %s where id = %s",
                    (decision, mfa_verified_at, approval_id),
                )
                await conn.execute(
                    "insert into audit.events (user_id, actor, action, target_type, target_id) "
                    "values (%s, 'user', %s, 'approval', %s)",
                    (user_id, f"approval_{decision}", str(approval_id)),
                )
                # If the task was cancelled or paused meanwhile, the decision still stands.
                with contextlib.suppress(InvalidTransitionError):
                    await transition(
                        conn,
                        row["task_id"],
                        "running",
                        event_type=f"approval_{decision}",
                        payload={"summary": f"owner {decision}: {row['summary']}"},
                    )
    # Raised after the transaction commits, so recording the expiry is not rolled back.
    if lapsed:
        raise NotPendingError("expired")
    if row["status"] != "pending":
        raise NotPendingError(row["status"])


async def expire_stale(conn: Conn, task_id: UUID) -> bool:
    """Marks overdue pending approvals expired. True if the task has nothing left to wait for."""
    await conn.execute(
        "update public.approvals set status = 'expired' "
        "where task_id = %s and status = 'pending' and expires_at <= now()",
        (task_id,),
    )
    still_open = await (
        await conn.execute(
            "select 1 from public.approvals where task_id = %s and status = 'pending'", (task_id,)
        )
    ).fetchone()
    return still_open is None
