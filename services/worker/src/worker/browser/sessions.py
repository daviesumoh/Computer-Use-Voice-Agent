"""Saved logins: browser state, encrypted, kept per connected account."""

import logging
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from worker.db import Conn
from worker.session_crypto import StateDecryptionError, decrypt_state, encrypt_state

log = logging.getLogger(__name__)


def keep_only(state: dict[str, Any], allowed_origins: list[str]) -> dict[str, Any]:
    """The part of a browser's state that belongs to one account's sites, and nothing else."""
    hosts = {urlsplit(origin).hostname for origin in allowed_origins}

    def ours(domain: str) -> bool:
        domain = domain.lstrip(".")
        return any(h == domain or (h or "").endswith("." + domain) for h in hosts)

    return {
        "cookies": [c for c in state.get("cookies", []) if ours(c["domain"])],
        "origins": [o for o in state.get("origins", []) if o["origin"].lower() in allowed_origins],
    }


def merge(states: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "cookies": [c for s in states for c in s.get("cookies", [])],
        "origins": [o for s in states for o in s.get("origins", [])],
    }


async def load(conn: Conn, key: bytes, account_ids: list[UUID]) -> dict[str, Any] | None:
    """The accounts' saved state combined, or None when there is nothing usable."""
    rows = await (
        await conn.execute(
            "select account_id, state_ciphertext from public.account_sessions "
            "where account_id = any(%s)",
            (account_ids,),
        )
    ).fetchall()
    states: list[dict[str, Any]] = []
    for row in rows:
        try:
            states.append(decrypt_state(key, row["account_id"], bytes(row["state_ciphertext"])))
        except StateDecryptionError:
            # A rotated key or a damaged row: carry on logged out rather than fail the task.
            log.warning("saved session unreadable", extra={"account_id": str(row["account_id"])})
    return merge(states) if states else None


async def save(
    conn: Conn,
    key: bytes,
    *,
    account_id: UUID,
    user_id: UUID,
    allowed_origins: list[str],
    state: dict[str, Any],
) -> None:
    mine = keep_only(state, allowed_origins)
    if not mine["cookies"] and not mine["origins"]:
        return
    await conn.execute(
        "insert into public.account_sessions (account_id, user_id, state_ciphertext) "
        "values (%s, %s, %s) on conflict (account_id) do update "
        "set state_ciphertext = excluded.state_ciphertext, updated_at = now()",
        (account_id, user_id, encrypt_state(key, account_id, mine)),
    )
