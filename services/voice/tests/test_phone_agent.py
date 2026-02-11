"""PhoneAgent tools against the real worker API (in-process) and the local Supabase Postgres."""

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from secrets import randbelow
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from agent_common.contracts import TurnRequest
from livekit.agents import RunContext
from psycopg.rows import DictRow, dict_row
from worker.api import create_app
from worker.auth import AuthError, Caller
from worker.db import create_pool
from worker.memory.jobs import Memory
from worker.settings import Settings as WorkerSettings

from voice.agent import PhoneAgent
from voice.conversation import SlotChange
from voice.worker_client import UnknownCallerError, WorkerClient

ADMIN_DSN = "postgresql://postgres:postgres@127.0.0.1:55322/postgres"
SECRET = "voice-test-hmac-secret-" + "y" * 32


class _NoAuth:
    """These tests never call an owner endpoint."""

    async def __call__(self, authorization: str | None) -> Caller:
        raise AuthError("not used")


class _NoEngine:
    """These tests stop at task creation; they never run the task engine."""

    async def enqueue(self, task_id: object) -> None:
        pass

    async def wake(self, task_id: object) -> None:
        pass

    async def remember_conversation(self, conversation_id: object) -> None:
        pass


class _UnitEmbedder:
    """Every text gets the same vector: the tests find memories by keyword."""

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[1.0] + [0.0] * 1535 for _ in texts]


# The tools ignore their RunContext; LiveKit supplies one only inside a live session.
NO_CONTEXT = cast(RunContext[None], None)


@pytest.fixture
def admin() -> Any:
    with psycopg.connect(ADMIN_DSN, autocommit=True, row_factory=dict_row) as conn:
        yield conn


@pytest.fixture
def phone(admin: psycopg.Connection[DictRow]) -> str:
    return make_owner(admin)[1]


def make_owner(admin: psycopg.Connection[DictRow]) -> tuple[UUID, str]:
    user_id, number = uuid4(), "+1647555" + str(randbelow(10_000)).zfill(4)
    admin.execute("delete from public.phone_numbers where e164 = %s", (number,))
    admin.execute("insert into auth.users (id, email) values (%s, %s)", (user_id, f"{user_id}@t"))
    admin.execute("insert into public.profiles (id, display_name) values (%s, 'Dave')", (user_id,))
    admin.execute(
        "insert into public.phone_numbers (user_id, e164, verified_at, is_primary) "
        "values (%s, %s, %s, true)",
        (user_id, number, datetime.now(UTC)),
    )
    return user_id, number


@pytest.fixture
async def worker() -> AsyncIterator[WorkerClient]:
    settings = WorkerSettings(
        database_url="postgresql://agent_worker:local-dev-only@127.0.0.1:55322/postgres",  # type: ignore[arg-type]
        service_hmac_secret=SECRET,  # type: ignore[arg-type]
        twilio_account_sid="AC" + "0" * 32,
        twilio_api_key_sid="SK" + "0" * 32,
        twilio_api_key_secret="fake",  # type: ignore[arg-type]
        twilio_from_number="+14165550100",
        supabase_url="http://127.0.0.1:55321",
        supabase_publishable_key="sb_publishable_test_key",
        _env_file=None,  # type: ignore[call-arg]
    )
    pool = create_pool(settings)
    await pool.open(wait=True)
    twilio = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(201, json={"sid": f"SM{uuid4().hex}"})
        )
    )
    memory = Memory(_UnitEmbedder(), cast(Any, None))  # recall needs only the embedder
    app = create_app(settings, pool, twilio, _NoEngine(), _NoAuth(), memory=memory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker"
    ) as http:
        yield WorkerClient(http, SECRET)
    await twilio.aclose()
    await pool.close()


def tasks_for(admin: psycopg.Connection[DictRow], conversation_id: object) -> list[DictRow]:
    return admin.execute(
        "select short_code, title, spec from public.tasks where created_from_conversation = %s",
        (conversation_id,),
    ).fetchall()


async def test_unknown_caller_is_refused(worker: WorkerClient) -> None:
    with pytest.raises(UnknownCallerError):
        await worker.start_call("CA-unknown", "+19995550111")


async def test_task_starts_only_after_details_are_agreed(
    worker: WorkerClient, phone: str, admin: psycopg.Connection[DictRow]
) -> None:
    caller = await worker.start_call(f"CA{uuid4().hex}", phone)
    agent = PhoneAgent(caller, worker)
    agent.state.last_user_confidence = 0.96

    # Nothing described yet: refused, nothing created.
    refused = await agent.start_task(NO_CONTEXT, title="Compare laptops", summary="Compare laptops")
    assert refused.startswith("Not started")
    assert tasks_for(admin, caller.conversation_id) == []

    await agent.update_task_details(
        NO_CONTEXT, [SlotChange(name="sites", action="set", value="Best Buy and Canada Computers")]
    )
    started = await agent.start_task(
        NO_CONTEXT,
        title="Compare laptops",
        summary="Compare laptop prices at two stores, text the top 3",
    )

    assert started.startswith("Started as T1")
    (task,) = tasks_for(admin, caller.conversation_id)
    assert task["title"] == "Compare laptops"
    assert task["spec"] == {
        "summary": "Compare laptop prices at two stores, text the top 3",
        "details": {"sites": "Best Buy and Canada Computers"},
    }


async def test_misheard_detail_blocks_start_until_confirmed(
    worker: WorkerClient, phone: str, admin: psycopg.Connection[DictRow]
) -> None:
    caller = await worker.start_call(f"CA{uuid4().hex}", phone)
    agent = PhoneAgent(caller, worker)

    agent.state.last_user_confidence = 0.55  # noisy line
    reply = await agent.update_task_details(
        NO_CONTEXT, [SlotChange(name="email", action="set", value="dave@exmple.com")]
    )
    assert "confirm" in reply and "dave@exmple.com" in reply

    blocked = await agent.start_task(NO_CONTEXT, title="Email list", summary="Email the list")
    assert blocked == "Not started. Read these back and get a yes first: email"
    assert tasks_for(admin, caller.conversation_id) == []

    agent.state.last_user_confidence = 0.97
    await agent.update_task_details(NO_CONTEXT, [SlotChange(name="email", action="confirm")])
    started = await agent.start_task(NO_CONTEXT, title="Email list", summary="Email the list")
    assert started.startswith("Started as")


async def test_resumed_call_continues_the_dropped_draft(worker: WorkerClient, phone: str) -> None:
    first = await worker.start_call(f"CA{uuid4().hex}", phone)
    agent = PhoneAgent(first, worker)
    agent.state.last_user_confidence = 0.95
    await agent.update_task_details(
        NO_CONTEXT, [SlotChange(name="dates", action="set", value="May 3-5")]
    )
    await worker.record_turn(
        first.conversation_id,
        TurnRequest(
            turn_index=0,
            speaker="user",
            text="May 3 to 5",
            state=agent.state.model_dump(mode="json"),
        ),
    )
    await worker.end_call(first.conversation_id, "participant_disconnected")

    second = await worker.start_call(f"CA{uuid4().hex}", phone)
    resumed = PhoneAgent(second, worker)

    assert resumed.state.slots["dates"].value == "May 3-5"
    assert resumed.state.draft_request_id == agent.state.draft_request_id


async def test_naming_a_site_without_a_saved_login_is_reported_not_swallowed(
    worker: WorkerClient, phone: str, admin: psycopg.Connection[DictRow]
) -> None:
    caller = await worker.start_call(f"CA{uuid4().hex}", phone)
    agent = PhoneAgent(caller, worker)
    agent.state.last_user_confidence = 0.95
    await agent.update_task_details(
        NO_CONTEXT, [SlotChange(name="what", action="set", value="check my order")]
    )
    with pytest.raises(httpx.HTTPStatusError, match="422"):
        await agent.start_task(
            NO_CONTEXT, title="Check order", summary="Check my order", sites=["amazon.ca"]
        )
    assert tasks_for(admin, caller.conversation_id) == []


def remember(admin: psycopg.Connection[DictRow], user_id: UUID, text: str) -> UUID:
    row = admin.execute(
        "insert into public.memories (user_id, type, canonical_text, origin, cue) "
        "values (%s, 'rule', %s, 'user_stated', 'explicit_rule') returning id",
        (user_id, text),
    ).fetchone()
    assert row is not None
    return UUID(str(row["id"]))


async def test_the_agent_can_look_up_and_forget_a_memory(
    worker: WorkerClient, admin: psycopg.Connection[DictRow]
) -> None:
    user_id, number = make_owner(admin)
    memory_id = remember(admin, user_id, "Always buy electronics from Best Buy.")
    caller = await worker.start_call(f"CA{uuid4().hex}", number)
    agent = PhoneAgent(caller, worker)

    found = await agent.recall_memory(NO_CONTEXT, "electronics")
    nothing = await agent.recall_memory(NO_CONTEXT, "flight seats")
    forgotten = await agent.forget_memory(NO_CONTEXT, [str(memory_id)])
    after = await agent.recall_memory(NO_CONTEXT, "electronics")

    assert found == f"{memory_id} (rule) Always buy electronics from Best Buy."
    assert nothing == "Nothing is remembered about that."
    assert forgotten == "Forgotten: 1."
    assert after == "Nothing is remembered about that."


async def test_the_agent_rejects_ids_that_are_not_memory_ids(
    worker: WorkerClient, phone: str
) -> None:
    agent = PhoneAgent(await worker.start_call(f"CA{uuid4().hex}", phone), worker)

    assert (await agent.forget_memory(NO_CONTEXT, ["whatever"])).startswith("Not forgotten")
    assert (await agent.settle_memory(NO_CONTEXT, "whatever", keep=True)).startswith("Not recorded")


async def test_a_new_call_opens_with_the_standing_rules_in_the_prompt(
    worker: WorkerClient, admin: psycopg.Connection[DictRow]
) -> None:
    user_id, number = make_owner(admin)
    remember(admin, user_id, "Always buy electronics from Best Buy.")

    caller = await worker.start_call(f"CA{uuid4().hex}", number)

    assert "- Always buy electronics from Best Buy." in str(PhoneAgent(caller, worker).instructions)


async def test_the_agent_passes_a_change_to_a_running_task_and_reports_a_finished_one(
    worker: WorkerClient, admin: psycopg.Connection[DictRow]
) -> None:
    user_id, number = make_owner(admin)
    for code, status in (("T1", "running"), ("T2", "completed")):
        admin.execute(
            "insert into public.tasks (user_id, short_code, title, spec, status, budget_cents) "
            "values (%s, %s, 'Compare TVs', '{}', %s, 300)",
            (user_id, code, status),
        )
    agent = PhoneAgent(await worker.start_call(f"CA{uuid4().hex}", number), worker)

    passed = await agent.edit_task(NO_CONTEXT, "t1", "make it three results")
    finished = await agent.edit_task(NO_CONTEXT, "T2", "make it three results")

    assert passed == "Passed to T1; it applies the change at its next step."
    assert finished == "Not passed on: task is completed."
    stored = admin.execute(
        "select channel, body from public.instructions where user_id = %s", (user_id,)
    ).fetchall()
    assert stored == [{"channel": "voice", "body": "make it three results"}]
