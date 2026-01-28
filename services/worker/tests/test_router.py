"""Texting commands to the worker, through the signed webhook endpoint."""

from collections.abc import Callable
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import DictRow

from tests.conftest import FakeTwilio, RecordingDispatcher, SignedClient, User

pytestmark = pytest.mark.db
Admin = psycopg.Connection[DictRow]


def add_task(admin: Admin, user: User, code: str, status: str = "running") -> UUID:
    row = admin.execute(
        "insert into public.tasks (user_id, short_code, title, spec, status, budget_cents) "
        "values (%s, %s, %s, '{}', %s, 300) returning id",
        (user.id, code, f"Job {code}", status),
    ).fetchone()
    assert row is not None
    return UUID(str(row["id"]))


async def text(api: SignedClient, user: User, body: str, sid: str | None = None) -> str:
    sid = sid or f"SM{uuid4().hex}"
    response = await api.post(
        "/v1/sms/inbound", {"message_sid": sid, "from_number": user.phone, "body": body}
    )
    assert response.status_code == 200
    return sid


def bodies(twilio: FakeTwilio) -> list[str]:
    return [sent["Body"] for sent in twilio.sent]


def instructions(admin: Admin, task_id: UUID) -> list[str]:
    rows = admin.execute(
        "select command from public.instructions where task_id = %s order by seq", (task_id,)
    )
    return [r["command"] for r in rows.fetchall()]


async def test_status_lists_the_active_task_with_spend(
    api: SignedClient, twilio: FakeTwilio, make_user: Callable[..., User], admin: Admin
) -> None:
    user = make_user()
    add_task(admin, user, "T4")
    await text(api, user, "status")
    assert bodies(twilio) == ["T4 running: Job T4 [$0.00 of $3.00]"]


async def test_pause_is_recorded_for_the_workflow_and_wakes_it(
    api: SignedClient,
    twilio: FakeTwilio,
    dispatcher: RecordingDispatcher,
    make_user: Callable[..., User],
    admin: Admin,
) -> None:
    user = make_user()
    task_id = add_task(admin, user, "T4")

    await text(api, user, "pause")

    assert instructions(admin, task_id) == ["pause"]
    assert dispatcher.woken == [task_id]
    assert bodies(twilio) == []  # the workflow confirms once it has actually paused


async def test_a_redelivered_command_is_recorded_once(
    api: SignedClient, make_user: Callable[..., User], admin: Admin
) -> None:
    user = make_user()
    task_id = add_task(admin, user, "T4")
    sid = await text(api, user, "kill T4")
    await text(api, user, "kill T4", sid=sid)
    assert instructions(admin, task_id) == ["cancel"]


async def test_carrier_opt_out_words_are_not_task_commands(
    api: SignedClient, twilio: FakeTwilio, make_user: Callable[..., User], admin: Admin
) -> None:
    user = make_user()
    task_id = add_task(admin, user, "T4")
    await text(api, user, "cancel T4")
    assert instructions(admin, task_id) == []
    assert "Commands:" in bodies(twilio)[0]


async def test_killing_a_queued_task_cancels_it_without_waiting_for_its_turn(
    api: SignedClient, twilio: FakeTwilio, make_user: Callable[..., User], admin: Admin
) -> None:
    user = make_user()
    task_id = add_task(admin, user, "T4", status="queued")
    await text(api, user, "kill T4")
    row = admin.execute("select status from public.tasks where id = %s", (task_id,)).fetchone()
    assert row == {"status": "cancelled"}
    assert bodies(twilio) == ["T4 cancelled."]


async def test_without_a_code_several_active_tasks_need_disambiguation(
    api: SignedClient, twilio: FakeTwilio, make_user: Callable[..., User], admin: Admin
) -> None:
    user = make_user()
    first, second = add_task(admin, user, "T1"), add_task(admin, user, "T2", status="paused")
    await text(api, user, "resume")
    assert bodies(twilio) == ["Which task? Active: T1, T2."]
    assert instructions(admin, first) == instructions(admin, second) == []


async def test_a_finished_task_cannot_be_commanded(
    api: SignedClient, twilio: FakeTwilio, make_user: Callable[..., User], admin: Admin
) -> None:
    user = make_user()
    task_id = add_task(admin, user, "T4", status="completed")
    await text(api, user, "pause T4")
    assert bodies(twilio) == ["T4 is already completed."]
    assert instructions(admin, task_id) == []


async def test_a_task_code_never_reaches_another_users_task(
    api: SignedClient, twilio: FakeTwilio, make_user: Callable[..., User], admin: Admin
) -> None:
    owner, other = make_user(), make_user()
    task_id = add_task(admin, owner, "T1", status="queued")
    await text(api, other, "kill T1")
    row = admin.execute("select status from public.tasks where id = %s", (task_id,)).fetchone()
    assert row == {"status": "queued"}
    assert bodies(twilio) == ["No task T1."]


async def test_halt_all_sets_the_emergency_stop_and_leaves_an_audit_trail(
    api: SignedClient,
    twilio: FakeTwilio,
    dispatcher: RecordingDispatcher,
    make_user: Callable[..., User],
    admin: Admin,
) -> None:
    user = make_user()
    running = add_task(admin, user, "T1")
    add_task(admin, user, "T2", status="paused")

    await text(api, user, "halt all")

    flag = admin.execute(
        "select emergency_stop from public.user_settings where user_id = %s", (user.id,)
    ).fetchone()
    assert flag == {"emergency_stop": True}
    assert dispatcher.woken == [running]
    assert "Pausing 1 running task" in bodies(twilio)[0]
    audit = admin.execute(
        "select count(*) as n from audit.events where user_id = %s and action = 'emergency_stop'",
        (user.id,),
    ).fetchone()
    assert audit == {"n": 1}
