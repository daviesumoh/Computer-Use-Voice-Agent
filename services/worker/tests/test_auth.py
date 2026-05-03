"""The authenticator against the local Supabase Auth service, with a real TOTP enrolment."""

import base64
import hashlib
import hmac
import os
import struct
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from worker.auth import AuthError, SupabaseAuthenticator

pytestmark = pytest.mark.db
AUTH_URL = "http://127.0.0.1:55321"


def local_keys() -> tuple[str, str]:
    env = dict(
        line.split("=", 1)
        for line in os.popen("supabase status -o env 2>/dev/null")
        .read()
        .replace('"', "")
        .splitlines()
        if "=" in line
    )
    return env["PUBLISHABLE_KEY"], env["SECRET_KEY"]


def totp_code(secret: str) -> str:
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", int(time.time()) // 30), hashlib.sha1).digest()
    offset = digest[-1] & 15
    return str(
        (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    ).zfill(6)


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


class Tokens:
    def __init__(self, aal1: str, aal2: str, user_id: str) -> None:
        self.aal1, self.aal2, self.user_id = aal1, aal2, user_id


@pytest.fixture
async def tokens(http: httpx.AsyncClient) -> Tokens:
    publishable, secret = local_keys()
    email, password = f"auth{uuid.uuid4().hex[:8]}@t.test", "Aa1!aaaaaaaaaaaa"
    created = await http.post(
        f"{AUTH_URL}/auth/v1/admin/users",
        headers={"apikey": secret, "Authorization": f"Bearer {secret}"},
        json={"email": email, "password": password, "email_confirm": True},
    )
    signed_in = await http.post(
        f"{AUTH_URL}/auth/v1/token?grant_type=password",
        headers={"apikey": publishable},
        json={
            "email": email,
            "password": password,
            "gotrue_meta_security": {"captcha_token": "XXXX.DUMMY.TOKEN.XXXX"},  # Turnstile test
        },
    )
    aal1 = signed_in.json()["access_token"]
    headers = {"apikey": publishable, "Authorization": f"Bearer {aal1}"}
    factor = (
        await http.post(
            f"{AUTH_URL}/auth/v1/factors", headers=headers, json={"factor_type": "totp"}
        )
    ).json()
    challenge = (
        await http.post(f"{AUTH_URL}/auth/v1/factors/{factor['id']}/challenge", headers=headers)
    ).json()
    verified = await http.post(
        f"{AUTH_URL}/auth/v1/factors/{factor['id']}/verify",
        headers=headers,
        json={"challenge_id": challenge["id"], "code": totp_code(factor["totp"]["secret"])},
    )
    return Tokens(aal1, verified.json()["access_token"], created.json()["id"])


async def test_a_totp_verified_session_is_aal2_with_a_recent_verification_time(
    http: httpx.AsyncClient, tokens: Tokens
) -> None:
    authenticate = SupabaseAuthenticator(http, AUTH_URL, local_keys()[0])
    caller = await authenticate(f"Bearer {tokens.aal2}")
    assert str(caller.user_id) == tokens.user_id
    assert caller.aal == "aal2"
    assert caller.mfa_verified_at is not None
    assert datetime.now(UTC) - caller.mfa_verified_at < timedelta(minutes=1)


async def test_a_password_only_session_is_aal1_with_no_mfa_time(
    http: httpx.AsyncClient, tokens: Tokens
) -> None:
    caller = await SupabaseAuthenticator(http, AUTH_URL, local_keys()[0])(f"Bearer {tokens.aal1}")
    assert (caller.aal, caller.mfa_verified_at) == ("aal1", None)


@pytest.mark.parametrize("kind", ["missing", "not_bearer", "tampered", "garbage"])
async def test_anything_but_a_valid_supabase_token_is_rejected(
    http: httpx.AsyncClient, tokens: Tokens, kind: str
) -> None:
    header = {
        "missing": None,
        "not_bearer": f"Basic {tokens.aal2}",
        "tampered": f"Bearer {tokens.aal2[:-4]}AAAA",
        "garbage": "Bearer not.a.token",
    }[kind]
    with pytest.raises(AuthError):
        await SupabaseAuthenticator(http, AUTH_URL, local_keys()[0])(header)


async def test_an_unreachable_auth_service_fails_closed() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(_refuse)) as down:
        with pytest.raises(AuthError, match="could not reach"):
            await SupabaseAuthenticator(down, AUTH_URL, "key")("Bearer a.b.c")


def _refuse(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("down", request=request)
