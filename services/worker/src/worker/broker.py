"""Connected accounts: storing a website login in the secret store and removing it again."""

import json
import re
from urllib.parse import urlsplit
from uuid import UUID

from worker.db import Conn
from worker.secret_store import SecretStore

_DOMAIN = re.compile(r"^(?=.{4,253}$)([a-z0-9-]+\.)+[a-z]{2,}$")


class InvalidAccountError(Exception):
    pass


class UnknownAccountError(Exception):
    pass


def default_origins(site_domain: str) -> list[str]:
    return [f"https://{site_domain}", f"https://www.{site_domain}"]


def validate_origins(site_domain: str, origins: list[str], *, allow_insecure: bool) -> list[str]:
    """Every origin must be https on the site's own domain (or a subdomain of it)."""
    if not _DOMAIN.match(site_domain):
        raise InvalidAccountError(f"{site_domain!r} is not a domain name")
    checked: list[str] = []
    for origin in origins:
        parts = urlsplit(origin)
        host = parts.hostname or ""
        scheme_ok = parts.scheme == "https" or (allow_insecure and parts.scheme == "http")
        if not scheme_ok or parts.path not in {"", "/"} or parts.query or parts.fragment:
            raise InvalidAccountError(f"{origin!r} must be a plain https origin")
        if not allow_insecure and not (host == site_domain or host.endswith("." + site_domain)):
            raise InvalidAccountError(f"{origin!r} is not on {site_domain}")
        checked.append(f"{parts.scheme}://{parts.netloc}".lower())
    if not checked:
        raise InvalidAccountError("at least one allowed origin is required")
    return checked


async def connect_account(
    conn: Conn,
    store: SecretStore,
    *,
    user_id: UUID,
    site_domain: str,
    label: str,
    username: str,
    password: str,
    allowed_origins: list[str] | None = None,
    allow_insecure_origins: bool = False,
) -> UUID:
    """Saves the credential in the secret store, then records the account. Returns its id."""
    site_domain = site_domain.strip().lower()
    origins = validate_origins(
        site_domain,
        default_origins(site_domain) if allowed_origins is None else allowed_origins,
        allow_insecure=allow_insecure_origins,
    )
    ref = await store.create(
        f"account:{site_domain}:{label}", json.dumps({"username": username, "password": password})
    )
    try:
        async with conn.transaction():
            row = await (
                await conn.execute(
                    "insert into public.connected_accounts (user_id, site_domain, "
                    "allowed_origins, label, username_hint, secret_ref) "
                    "values (%s, %s, %s, %s, %s, %s) returning id",
                    (user_id, site_domain, origins, label, _hint(username), ref),
                )
            ).fetchone()
            assert row is not None
            await conn.execute(
                "insert into audit.events (user_id, actor, action, target_type, target_id) "
                "values (%s, 'user', 'account_connected', 'account', %s)",
                (user_id, str(row["id"])),
            )
    except Exception:
        # The credential must not outlive a failed save; then let the real error through.
        await store.delete(ref)
        raise
    return UUID(str(row["id"]))


async def disconnect_account(
    conn: Conn, store: SecretStore, *, user_id: UUID, account_id: UUID
) -> None:
    """Deletes the credential first, so a failure leaves the account visible and retryable."""
    row = await (
        await conn.execute(
            "select secret_ref from public.connected_accounts where id = %s and user_id = %s",
            (account_id, user_id),
        )
    ).fetchone()
    if row is None:
        raise UnknownAccountError(str(account_id))
    await store.delete(row["secret_ref"])
    async with conn.transaction():
        await conn.execute("delete from public.connected_accounts where id = %s", (account_id,))
        await conn.execute(
            "insert into audit.events (user_id, actor, action, target_type, target_id) "
            "values (%s, 'user', 'account_disconnected', 'account', %s)",
            (user_id, str(account_id)),
        )


def _hint(username: str) -> str:
    """Enough of the username to recognise the account, not enough to impersonate it."""
    name, _, domain = username.partition("@")
    visible = name[:2] + "***"
    return f"{visible}@{domain}" if domain else visible
