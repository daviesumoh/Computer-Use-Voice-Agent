"""Answering a task's question and adding to a running task, by text and by voice."""

from collections.abc import Callable
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import DictRow

from tests.conftest import FakeTwilio, RecordingDispatcher, SignedClient, User
from tests.engine_harness import Harness, eventually, scripted
from tests.test_router import add_task, bodies, text
from worker.engine import StepResult, WaitForUser

pytestmark = pytest.mark.db
Admin = psycopg.Connection[DictRow]


def waiting(admin: Admin, user: User, reason: str) -> UUID:
    task_id = add_task(admin, user, "T4")
    admin.execute(
        "update public.tasks set status = 'waiting_user', waiting_reason = %s where id = %s",
        (reason, task_id),
    )
    return task_id


def stored(admin: Admin, task_id: UUID) -> list[dict[str, object]]:
    return admin.execute(
        "select command, channel, body from public.instructions where task_id = %s order by seq",
        (task_id,),
    ).fetchall()


# -- routing a text ------------------------------------------------------------------------


async def test_a_plain_text_is_a_note_for_the_only_active_task(
    api: SignedClient, twilio: FakeTwilio, dispatcher: RecordingDispatcher,
    make_user: Callable[..., User], admin: Admin,
) -> None:  # fmt: skip
    user = make_user()
    task_id = add_task(admin, user, "T4")

    await text(api, user, "skip Amazon, add Best Buy")

    assert stored(admin, task_id) == [
        {"command": "edit", "channel": "sms", "body": "skip Amazon, add Best Buy"}
    ]
    assert bodies(twilio) == ["Noted for T4."] and dispatcher.woken == [task_id]


async def test_a_task_code_prefix_addresses_one_of_several_tasks(
    api: SignedClient, make_user: Callable[..., User], admin: Admin, twilio: FakeTwilio
) -> None:
    user = make_user()
    first, second = add_task(admin, user, "T4"), add_task(admin, user, "T5")

    await text(api, user, "t5: only look at Costco")
    await text(api, user, "only look at Costco")  # no code, two tasks: nothing is guessed

    assert stored(admin, first) == []
    assert stored(admin, second) == [
        {"command": "edit", "channel": "sms", "body": "only look at Costco"}
    ]
    assert bodies(twilio) == ["Noted for T5.", "Which task? Active: T4, T5."]


async def test_with_no_active_task_an_unknown_text_gets_the_command_list(
    api: SignedClient, make_user: Callable[..., User], twilio: FakeTwilio
) -> None:
    await text(api, make_user(), "hello there")

    assert "Commands:" in bodies(twilio)[0]


async def test_a_text_cannot_approve_it_points_to_the_dashboard(
    api: SignedClient, make_user: Callable[..., User], admin: Admin, twilio: FakeTwilio
) -> None:
    user = make_user()
    task_id = waiting(admin, user, "approval")

    await text(api, user, "yes")

    assert stored(admin, task_id) == []
    assert bodies(twilio) == [
        "Approvals are only given in the dashboard, with your authenticator. "
        "https://dash.example/approvals"
    ]


async def test_a_reply_to_a_waiting_question_is_stored_as_the_answer(
    api: SignedClient, make_user: Callable[..., User], admin: Admin
) -> None:
    user = make_user()
    task_id = waiting(admin, user, "clarify")

    await text(api, user, "No, it did not go through")

    assert [i["command"] for i in stored(admin, task_id)] == ["answer"]


async def test_a_redelivered_text_is_one_note(
    api: SignedClient, make_user: Callable[..., User], admin: Admin
) -> None:
    user = make_user()
    task_id = add_task(admin, user, "T4")
    sid = await text(api, user, "add Best Buy")

    await text(api, user, "add Best Buy", sid=sid)

    assert len(stored(admin, task_id)) == 1


# -- by voice ------------------------------------------------------------------------------


async def test_the_voice_agent_can_add_to_a_running_task(
    api: SignedClient, dispatcher: RecordingDispatcher, make_user: Callable[..., User], admin: Admin
) -> None:
    user = make_user()
    task_id = add_task(admin, user, "T4")
    call = (
        await api.post(
            "/v1/calls/start", {"call_sid": f"CA{uuid4().hex}", "caller_number": user.phone}
        )
    ).json()["conversation_id"]

    response = await api.post(f"/v1/calls/{call}/tasks/t4/edit", {"text": "make it three results"})

    assert response.status_code == 204
    assert stored(admin, task_id) == [
        {"command": "edit", "channel": "voice", "body": "make it three results"}
    ]
    assert dispatcher.woken == [task_id]
