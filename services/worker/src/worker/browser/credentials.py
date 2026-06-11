"""Typing a stored credential into a page, only where the account's owner said it belongs."""

import json
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from worker.browser.errors import BrowserError, IrrecoverableError
from worker.browser.session import BrowserSession
from worker.browser.snapshot import Node
from worker.secret_store import SecretStore

_USERNAME_TYPES = frozenset({"text", "email", "tel", ""})


class CredentialRefusedError(BrowserError):
    """The request does not make sense (not a login field); the model is told and may adjust."""


class OriginMismatchError(IrrecoverableError):
    """The page or frame asking for the credential is not one the owner approved. This is how
    phishing and hostile redirects look, so the task stops."""


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


async def fill_credential(
    session: BrowserSession,
    store: SecretStore,
    *,
    secret_ref: str,
    allowed_origins: list[str],
    account_id: UUID,
    node: Node,
    field: str,
) -> str:
    """Fills one login field. Returns a description that contains no secret."""
    page_origin = origin_of(session.page.url)
    if page_origin not in allowed_origins:
        raise OriginMismatchError(f"this page ({page_origin}) is not approved for that account")

    target = session.page.locator(f"aria-ref={node.ref}")
    facts: dict[str, Any] = await target.evaluate(
        "el => ({tag: el.tagName, type: el.type ?? '', origin: el.ownerDocument.location.origin})"
    )
    # The field may sit in an iframe from another site even though the page itself is fine.
    if str(facts["origin"]).lower() not in allowed_origins:
        raise OriginMismatchError(
            f"the form is served from {facts['origin']}, which is not approved"
        )
    if facts["tag"] != "INPUT":
        raise CredentialRefusedError(f"'{node.name}' is not an input field")
    wanted_ok = (
        facts["type"] == "password" if field == "password" else facts["type"] in _USERNAME_TYPES
    )
    if not wanted_ok:
        raise CredentialRefusedError(f"'{node.name}' is not a {field} field")

    stored = json.loads(await store.read(secret_ref))
    value = str(stored[field])
    if field == "password":  # usernames are not secret, and pages legitimately greet by name
        session.remember_secret(value)
    await target.fill(value)
    del value, stored  # not kept past this call
    return f"filled the {field} field '{node.name}' for account {account_id}"
