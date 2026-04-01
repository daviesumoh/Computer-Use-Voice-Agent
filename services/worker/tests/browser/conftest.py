import socket
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from uuid import UUID

import psycopg
import pytest
import uvicorn
from fastapi import FastAPI
from psycopg.rows import DictRow

from tests.browser.chaos_site import create_site
from tests.browser.support import ScriptedDecider
from tests.conftest import User
from worker.browser.agent import BrowserExecutor
from worker.browser.session import BrowserSession
from worker.db import Pool
from worker.memory.jobs import Memory
from worker.secret_store import SecretStore


@dataclass
class Site:
    base: str
    app: FastAPI

    def url(self, path: str) -> str:
        return self.base + path


def serve() -> Iterator[Site]:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    app = create_site()
    server = uvicorn.Server(uvicorn.Config(app, port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    yield Site(f"http://127.0.0.1:{port}", app)
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="session")
def site() -> Iterator[Site]:
    yield from serve()


@pytest.fixture(scope="session")
def evil_site() -> Iterator[Site]:
    """The same pages on another port, which is a different origin: where a phisher would be."""
    yield from serve()


@pytest.fixture
async def browser() -> AsyncIterator[BrowserSession]:
    # Short action timeout so a blocked click fails fast; the local site is "private".
    async with BrowserSession(
        headless=True, allow_private_hosts=True, action_timeout_ms=2_000
    ) as session:
        yield session


@pytest.fixture
async def make_executor(pool: Pool) -> AsyncIterator[Callable[..., BrowserExecutor]]:
    made: list[BrowserExecutor] = []

    def make(
        decider: ScriptedDecider,
        store: SecretStore | None = None,
        session_key: bytes | None = None,
        memory: Memory | None = None,
    ) -> BrowserExecutor:
        executor = BrowserExecutor(
            pool,
            decider,
            store=store,
            session_key=session_key,
            memory=memory,
            headless=True,
            allow_private_hosts=True,
            retry_delays=(0, 0),
            action_timeout_ms=2_000,
        )
        made.append(executor)
        return executor

    yield make
    for executor in made:
        for task_id in list(executor._runs):
            await executor.release(task_id)


@pytest.fixture
def new_task(
    admin: psycopg.Connection[DictRow], make_user: Callable[..., User]
) -> Callable[..., UUID]:
    def make(owner: User | None = None) -> UUID:
        row = admin.execute(
            "insert into public.tasks (user_id, short_code, title, spec, status, budget_cents) "
            "values (%s, 'T1', 'Compare TVs', '{\"max_results\": 3}', 'running', 300) returning id",
            ((owner or make_user()).id,),
        ).fetchone()
        assert row is not None
        return UUID(str(row["id"]))

    return make
