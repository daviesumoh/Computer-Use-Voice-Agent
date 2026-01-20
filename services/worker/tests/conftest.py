"""Fixtures for tests against the local Supabase Postgres (`supabase start`, port 55322)."""

import secrets
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from agent_common.signing import signed_headers
from psycopg.rows import DictRow, dict_row

from tests.browser.support import InMemorySecretStore
from tests.engine_harness import Harness
from tests.memory_support import FakeEmbedder, ScriptedMemoryModel
from worker.api import create_app
from worker.auth import AuthError, Caller
from worker.db import Pool, create_pool
from worker.engine import Engine, Runtime
from worker.memory.jobs import Memory
from worker.settings import Settings

ADMIN_DSN = "postgresql://postgres:postgres@127.0.0.1:55322/postgres"
# Login enabled by supabase/seed.sql (local stack only).
WORKER_DSN = "postgresql://agent_worker:local-dev-only@127.0.0.1:55322/postgres"
HMAC_SECRET = "test-hmac-secret-" + "x" * 32


@pytest.fixture
def admin() -> Iterator[psycopg.Connection[DictRow]]:
    """Superuser connection for setting up and inspecting state; never used by the app."""
    with psycopg.connect(ADMIN_DSN, autocommit=True, row_factory=dict_row) as conn:
        yield conn


@dataclass
class User:
    id: UUID
    phone: str


@pytest.fixture
def make_user(admin: psycopg.Connection[DictRow]) -> Callable[..., User]:
    def make(*, verified: bool = True) -> User:
        user_id = uuid4()
        phone = "+1416555" + str(secrets.randbelow(10_000)).zfill(4)
        admin.execute("delete from public.phone_numbers where e164 = %s", (phone,))
        admin.execute(
            "insert into auth.users (id, email) values (%s, %s)", (user_id, f"{user_id}@test")
        )
        admin.execute(
            "insert into public.profiles (id, display_name) values (%s, 'Test')", (user_id,)
        )
        admin.execute(
            "insert into public.phone_numbers (user_id, e164, verified_at, is_primary) "
            "values (%s, %s, %s, true)",
            (user_id, phone, datetime.now(UTC) if verified else None),
        )
        return User(user_id, phone)

    return make


@dataclass
class FakeTwilio:
    """Stands in for api.twilio.com; records sends and can be told to fail."""

    sent: list[dict[str, str]] = field(default_factory=list)
    fail_with: tuple[int, dict[str, object]] | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.fail_with:
            status, body = self.fail_with
            return httpx.Response(status, json=body)
        form = dict(httpx.QueryParams(request.content.decode()))
        self.sent.append(form)
        return httpx.Response(201, json={"sid": f"SM{uuid4().hex}"})


@pytest.fixture
def twilio() -> FakeTwilio:
    return FakeTwilio()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url=WORKER_DSN,  # type: ignore[arg-type]
        service_hmac_secret=HMAC_SECRET,  # type: ignore[arg-type]
        supabase_url="http://127.0.0.1:55321",
        supabase_publishable_key="sb_publishable_test_key",
        dashboard_url="https://dash.example",
        twilio_account_sid="AC" + "0" * 32,
        twilio_api_key_sid="SK" + "0" * 32,
        twilio_api_key_secret="fake",  # type: ignore[arg-type]
        twilio_from_number="+14165550100",
        _env_file=None,  # type: ignore[call-arg]
    )


@pytest.fixture
async def pool(settings: Settings) -> AsyncIterator[Pool]:
    pool = create_pool(settings)
    await pool.open(wait=True)
    yield pool
    await pool.close()


class FakeAuthenticator:
    """Stands in for Supabase Auth: tokens are whatever the test registered."""

    def __init__(self) -> None:
        self.callers: dict[str, Caller] = {}

    def sign_in(self, user: "User", *, aal: str = "aal2", mfa_age_seconds: int | None = 30) -> str:
        token = f"token-{uuid4()}"
        mfa = (
            None
            if mfa_age_seconds is None
            else datetime.now(UTC) - timedelta(seconds=mfa_age_seconds)
        )
        self.callers[token] = Caller(user.id, aal, mfa)
        return token

    async def __call__(self, authorization: str | None) -> Caller:
        token = (authorization or "").removeprefix("Bearer ")
        if token not in self.callers:
            raise AuthError("unknown token")
        return self.callers[token]


class SignedClient:
    def __init__(
        self, client: httpx.AsyncClient, auth: FakeAuthenticator, secrets: InMemorySecretStore
    ) -> None:
        self.raw = client
        self.auth = auth
        self.secrets = secrets

    async def post_as(
        self, token: str | None, path: str, payload: Mapping[str, object] | None = None
    ) -> httpx.Response:
        headers = {"authorization": f"Bearer {token}"} if token else {}
        return await self.raw.post(path, json=payload or {}, headers=headers)

    async def post(self, path: str, payload: Mapping[str, object]) -> httpx.Response:
        body = httpx.Request("POST", "http://x", json=payload).content
        headers = signed_headers(HMAC_SECRET, "POST", path, body)
        headers["content-type"] = "application/json"
        return await self.raw.post(path, content=body, headers=headers)


@dataclass
class RecordingDispatcher:
    """Stands in for the task engine in HTTP-level tests; engine behaviour has its own tests."""

    enqueued: list[UUID] = field(default_factory=list)
    woken: list[UUID] = field(default_factory=list)
    remembered: list[UUID] = field(default_factory=list)

    async def enqueue(self, task_id: UUID) -> None:
        self.enqueued.append(task_id)

    async def wake(self, task_id: UUID) -> None:
        self.woken.append(task_id)

    async def remember_conversation(self, conversation_id: UUID) -> None:
        self.remembered.append(conversation_id)


@pytest.fixture
def dispatcher() -> RecordingDispatcher:
    return RecordingDispatcher()


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def scripted() -> ScriptedMemoryModel:
    return ScriptedMemoryModel()


@pytest.fixture
def memory(embedder: FakeEmbedder, scripted: ScriptedMemoryModel) -> Memory:
    return Memory(embedder, scripted)


@pytest.fixture
async def api(
    settings: Settings,
    pool: Pool,
    twilio: FakeTwilio,
    dispatcher: RecordingDispatcher,
    memory: Memory,
) -> AsyncIterator[SignedClient]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(twilio.handler)) as twilio_http:
        auth, secrets = FakeAuthenticator(), InMemorySecretStore()
        app = create_app(settings, pool, twilio_http, dispatcher, auth, secrets, memory=memory)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://worker") as client:
            yield SignedClient(client, auth, secrets)


@pytest.fixture
async def harness(pool: Pool, admin: psycopg.Connection[DictRow]) -> AsyncIterator[Harness]:
    # Workflows left by earlier tests would be recovered by this launch.
    if admin.execute("select to_regclass('dbos.workflow_status') as t").fetchone() == {
        "t": "dbos.workflow_status"
    }:
        admin.execute("truncate dbos.workflow_status cascade")
    texts: list[tuple[str, str]] = []

    async def notify(user_id: UUID, dedupe_key: str, body: str) -> None:
        texts.append((dedupe_key, body))

    runtime = Runtime(pool=pool, notify=notify, poll_seconds=0.2)
    engine = Engine(runtime, WORKER_DSN)
    await engine.start()
    yield Harness(engine, runtime, admin, texts)
    engine.stop()
