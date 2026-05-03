"""Who is calling the user-facing endpoints, and have they passed MFA recently?

The dashboard sends the owner's Supabase access token. Supabase itself validates it
(signature, expiry, revocation) through its /user endpoint; the claims we then read from the
same token say how the session was authenticated.
"""

import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

import httpx


class AuthError(Exception):
    pass


@dataclass(frozen=True)
class Caller:
    user_id: UUID
    aal: str  # 'aal1' after a password, 'aal2' after a TOTP code as well
    mfa_verified_at: datetime | None  # the latest TOTP verification in this session

    def mfa_is_fresh(self) -> bool:
        if self.mfa_verified_at is None:
            return False
        now = datetime.now(UTC)
        return now - MFA_MAX_AGE <= self.mfa_verified_at <= now + timedelta(seconds=30)


MFA_MAX_AGE = timedelta(minutes=5)  # sensitive actions need a TOTP code entered this recently


class Authenticator(Protocol):
    async def __call__(self, authorization: str | None) -> Caller: ...


class SupabaseAuthenticator:
    def __init__(self, http: httpx.AsyncClient, supabase_url: str, publishable_key: str) -> None:
        self._http = http
        self._url = supabase_url.rstrip("/")
        self._key = publishable_key

    async def __call__(self, authorization: str | None) -> Caller:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise AuthError("missing bearer token")
        try:
            response = await self._http.get(
                f"{self._url}/auth/v1/user",
                headers={"apikey": self._key, "Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as unreachable:
            raise AuthError("could not reach the authentication service") from unreachable
        if response.status_code != 200:
            raise AuthError("token rejected")

        claims = _claims(token)
        user_id = UUID(response.json()["id"])
        if claims.get("sub") != str(user_id) or claims.get("aud") != "authenticated":
            raise AuthError("token does not match the user")
        totp_times = [
            entry["timestamp"] for entry in claims.get("amr", []) if entry.get("method") == "totp"
        ]
        return Caller(
            user_id=user_id,
            aal=str(claims.get("aal", "aal1")),
            mfa_verified_at=datetime.fromtimestamp(max(totp_times), UTC) if totp_times else None,
        )


def _claims(token: str) -> dict[str, Any]:
    """The payload of a token Supabase has just validated; it is not trusted on its own."""
    try:
        payload = token.split(".")[1]
        claims: dict[str, Any] = json.loads(
            base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        )
    except (IndexError, ValueError) as malformed:
        raise AuthError("malformed token") from malformed
    return claims
