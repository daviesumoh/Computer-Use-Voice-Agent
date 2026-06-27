"""The owner driving a task's browser by hand, over a real WebSocket and a real Chromium."""

import asyncio
import base64
import json
import logging
import socket
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from uuid import UUID

import httpx
import psycopg
import pytest
import uvicorn
from playwright.async_api import Page
from psycopg.rows import DictRow
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.typing import Origin

from tests.browser.conftest import Site
from tests.browser.support import InMemorySecretStore, ScriptedDecider
from tests.conftest import FakeAuthenticator, RecordingDispatcher, User
from tests.engine_harness import eventually
from worker.api import create_app
from worker.browser.actions import Navigate
from worker.browser.agent import BrowserExecutor
from worker.db import Pool
from worker.logs import quiet_content_loggers
from worker.settings import Settings

pytestmark = [pytest.mark.browser, pytest.mark.db]
Admin = psycopg.Connection[DictRow]
DASHBOARD = "https://dash.example"
PASSWORD = "correct-horse-battery"


@dataclass
class Live:
    port: int
    auth: FakeAuthenticator
    dispatcher: RecordingDispatcher
    executor: BrowserExecutor
    admin: Admin
    site: Site

    def ws(self, token: str, origin: str = DASHBOARD) -> "connect":
        return connect(f"ws://127.0.0.1:{self.port}/v1/takeover/{token}", origin=Origin(origin))

    async def token_for(self, user: User, task_id: UUID) -> httpx.Response:
        async with httpx.AsyncClient() as client:
            return await client.post(
                f"http://127.0.0.1:{self.port}/v1/tasks/{task_id}/takeover",
                headers={"authorization": f"Bearer {self.auth.sign_in(user)}"},
            )

    def waiting_task(self, user: User, status: str = "waiting_user") -> UUID:
        reason = "login" if status == "waiting_user" else None
        row = self.admin.execute(
            "insert into public.tasks (user_id, short_code, title, spec, status, waiting_reason, "
            "budget_cents) values (%s, 'T1', 'Check orders', '{}', %s, %s, 300) returning id",
            (user.id, status, reason),
        ).fetchone()
        assert row is not None
        return UUID(str(row["id"]))

    def page(self, task_id: UUID) -> Page:
        session = self.executor.session_for(task_id)
        assert session is not None, "the task has no open browser"
        return session.page


@pytest.fixture
async def live(
    pool: Pool,
    settings: Settings,
    admin: Admin,
    site: Site,
    make_executor: Callable[..., BrowserExecutor],
) -> AsyncIterator[Live]:
    auth = FakeAuthenticator()
    dispatcher = RecordingDispatcher()
    executor = make_executor(
        ScriptedDecider([Navigate(kind="navigate", url=site.url("/login"))] * 4)
    )
    async with httpx.AsyncClient() as http:
        app = create_app(
            settings, pool, http, dispatcher, auth, InMemorySecretStore(), browsers=executor
        )
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, port=port, log_level="warning"))
        serving = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.05)
        yield Live(port, auth, dispatcher, executor, admin, site)
        server.should_exit = True
        await serving


async def next_frame(socket_: ClientConnection) -> dict[str, object]:
    while True:
        message = json.loads(await asyncio.wait_for(socket_.recv(), timeout=10))
        if message["type"] == "frame":
            return dict(message)


async def wait_for_message(socket_: ClientConnection, kind: str) -> dict[str, object]:
    while True:
        message = json.loads(await asyncio.wait_for(socket_.recv(), timeout=10))
        if message["type"] == kind:
            return dict(message)


async def click_on(socket_: ClientConnection, page: Page, selector: str) -> None:
    box = await page.locator(selector).bounding_box()
    assert box is not None
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    for action in ("press", "release"):
        await socket_.send(
            json.dumps({"type": "mouse", "action": action, "x": x, "y": y, "button": "left"})
        )


async def type_text(socket_: ClientConnection, text: str) -> None:
    await socket_.send(json.dumps({"type": "text", "text": text}))


async def test_the_owner_signs_in_by_hand_and_the_agent_resumes_on_that_page(
    live: Live, make_user: Callable[..., User], caplog: pytest.LogCaptureFixture
) -> None:
    user = make_user()
    task_id = live.waiting_task(user)
    await live.executor(task_id)  # the agent opened the login page, then needed the owner
    token = (await live.token_for(user, task_id)).json()["token"]
    quiet_content_loggers()  # what configure_logging does in the real worker
    caplog.set_level(logging.DEBUG)
    logins_before = len(live.site.app.state.logins)

    async with live.ws(token) as owner:
        frame = await next_frame(owner)
        assert base64.b64decode(str(frame["data"]))[:2] == b"\xff\xd8"  # a JPEG picture
        await click_on(owner, live.page(task_id), "input[aria-label=Email]")
        await type_text(owner, "alice@mail.example")
        await click_on(owner, live.page(task_id), "input[aria-label=Password]")
        await type_text(owner, PASSWORD)
        await click_on(owner, live.page(task_id), "button")
        await eventually(
            lambda: len(live.site.app.state.logins) > logins_before, "the site to get the login"
        )
        await owner.send(json.dumps({"type": "done"}))
        with pytest.raises(ConnectionClosed):
            while True:  # frames may still be in flight; the server then closes the socket
                await asyncio.wait_for(owner.recv(), timeout=10)

    assert live.site.app.state.logins[-1] == ("alice@mail.example", PASSWORD)
    status = live.admin.execute("select status from public.tasks where id = %s", (task_id,))
    assert status.fetchone() == {"status": "running"}
    assert live.dispatcher.woken == [task_id]
    actions = live.admin.execute(
        "select action from audit.events where target_id = %s order by id", (str(task_id),)
    )
    assert [a["action"] for a in actions.fetchall()] == ["takeover_started", "takeover_ended"]
    # What the owner typed is never recorded anywhere.
    everything = json.dumps(
        [
            live.admin.execute("select * from public.task_events").fetchall(),
            live.admin.execute("select * from audit.events").fetchall(),
        ],
        default=str,
    )
    assert PASSWORD not in everything
    # (The test's own WebSocket client is not part of the worker; its loggers are skipped.)
    worker_side = [r.getMessage() for r in caplog.records if r.name != "websockets.client"]
    assert PASSWORD not in "\n".join(worker_side)


async def test_done_is_refused_while_the_page_still_asks_for_a_password_unless_insisted_on(
    live: Live, make_user: Callable[..., User]
) -> None:
    user = make_user()
    task_id = live.waiting_task(user)
    await live.executor(task_id)  # still on the login page: nothing was typed
    token = (await live.token_for(user, task_id)).json()["token"]

    async with live.ws(token) as owner:
        await next_frame(owner)
        await owner.send(json.dumps({"type": "done"}))
        refusal = await wait_for_message(owner, "not_done")
        status = live.admin.execute("select status from public.tasks where id = %s", (task_id,))
        assert status.fetchone() == {"status": "waiting_user"}  # the agent was not woken

        await owner.send(json.dumps({"type": "done", "anyway": True}))
        with pytest.raises(ConnectionClosed):
            while True:
                await asyncio.wait_for(owner.recv(), timeout=10)

    assert refusal["reason"] == "the page still shows a password field"
    status = live.admin.execute("select status from public.tasks where id = %s", (task_id,))
    assert status.fetchone() == {"status": "running"}


async def test_closing_the_window_without_finishing_leaves_the_task_waiting(
    live: Live, make_user: Callable[..., User]
) -> None:
    user = make_user()
    task_id = live.waiting_task(user)
    await live.executor(task_id)
    token = (await live.token_for(user, task_id)).json()["token"]
    async with live.ws(token) as owner:
        await next_frame(owner)
    await asyncio.sleep(0.5)
    status = live.admin.execute("select status from public.tasks where id = %s", (task_id,))
    assert status.fetchone() == {"status": "waiting_user"}
    assert live.dispatcher.woken == []
    # They can come back with a new token.
    assert (await live.token_for(user, task_id)).status_code == 200


async def test_tokens_are_issued_only_for_the_owners_waiting_tasks(
    live: Live, make_user: Callable[..., User]
) -> None:
    owner, stranger = make_user(), make_user()
    waiting = live.waiting_task(owner)
    running = live.waiting_task(make_user(), status="running")

    assert (await live.token_for(owner, waiting)).status_code == 200
    assert (await live.token_for(stranger, waiting)).status_code == 409
    assert (await live.token_for(owner, running)).status_code == 409


async def test_a_token_works_once_expires_and_is_stored_only_as_a_hash(
    live: Live, make_user: Callable[..., User]
) -> None:
    user = make_user()
    task_id = live.waiting_task(user)
    await live.executor(task_id)

    token = (await live.token_for(user, task_id)).json()["token"]
    stored = live.admin.execute(
        "select count(*) as n from public.takeover_tokens where token_hash = %s", (token,)
    )
    assert stored.fetchone() == {"n": 0}

    async with live.ws(token) as first:
        await next_frame(first)
    with pytest.raises(InvalidStatus):  # the token was spent by the first connection
        async with live.ws(token):
            pass

    late = (await live.token_for(user, task_id)).json()["token"]
    live.admin.execute(
        "update public.takeover_tokens set expires_at = now() - interval '1 second' "
        "where task_id = %s and used_at is null",
        (task_id,),
    )
    with pytest.raises(InvalidStatus):
        async with live.ws(late):
            pass


async def test_a_connection_from_another_website_is_refused(
    live: Live, make_user: Callable[..., User]
) -> None:
    user = make_user()
    task_id = live.waiting_task(user)
    await live.executor(task_id)
    token = (await live.token_for(user, task_id)).json()["token"]
    with pytest.raises(InvalidStatus):
        async with live.ws(token, origin="https://evil.example"):
            pass


async def test_only_one_takeover_of_a_task_at_a_time(
    live: Live, make_user: Callable[..., User]
) -> None:
    user = make_user()
    task_id = live.waiting_task(user)
    await live.executor(task_id)
    first_token = (await live.token_for(user, task_id)).json()["token"]
    second_token = (await live.token_for(user, task_id)).json()["token"]

    async with live.ws(first_token) as first:
        await next_frame(first)
        with pytest.raises(InvalidStatus):
            async with live.ws(second_token):
                pass
