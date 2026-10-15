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


async def test_voice_edits_to_unknown_or_finished_tasks_are_refused(
    api: SignedClient, make_user: Callable[..., User], admin: Admin
) -> None:
    user = make_user()
    add_task(admin, user, "T4", status="completed")
    call = (
        await api.post(
            "/v1/calls/start", {"call_sid": f"CA{uuid4().hex}", "caller_number": user.phone}
        )
    ).json()["conversation_id"]

    unknown = await api.post(f"/v1/calls/{call}/tasks/T99/edit", {"text": "x"})
    finished = await api.post(f"/v1/calls/{call}/tasks/T4/edit", {"text": "x"})

    assert (unknown.status_code, finished.status_code) == (404, 409)


async def test_one_user_cannot_edit_anothers_task_by_code(
    api: SignedClient, make_user: Callable[..., User], admin: Admin
) -> None:
    caller, owner = make_user(), make_user()
    theirs = add_task(admin, owner, "T4")
    call = (
        await api.post(
            "/v1/calls/start", {"call_sid": f"CA{uuid4().hex}", "caller_number": caller.phone}
        )
    ).json()["conversation_id"]

    response = await api.post(f"/v1/calls/{call}/tasks/T4/edit", {"text": "send me the money"})

    assert response.status_code == 404 and stored(admin, theirs) == []


# -- applied by the engine ------------------------------------------------------------------


def instruct(
    h: Harness, task_id: UUID, command: str, body: str, channel: str = "sms", age: str = "0 seconds"
) -> UUID:
    row = h.admin.execute(
        "insert into public.instructions (user_id, task_id, command, channel, body, received_at) "
        "select user_id, id, %s, %s, %s, now() - %s::interval from public.tasks where id = %s "
        "returning id",
        (command, channel, body, age, task_id),
    ).fetchone()
    assert row is not None
    return UUID(str(row["id"]))


def notes(h: Harness, task_id: UUID) -> list[str]:
    row = h.admin.execute("select spec from public.tasks where id = %s", (task_id,)).fetchone()
    assert row is not None
    return [n["text"] for n in row["spec"].get("owner_edits", [])]


def slow_steps(h: Harness) -> None:
    scripted(h, [StepResult(1)] * 100)


async def test_a_note_lands_in_the_tasks_spec_and_history_at_the_next_safe_point(
    harness: Harness, make_user: Callable[..., User]
) -> None:
    slow_steps(harness)
    task_id = harness.new_task(make_user())
    await harness.engine.enqueue(task_id)
    await eventually(lambda: len(harness.executed) >= 1, "the first step")

    instruct(harness, task_id, "edit", "skip Amazon")
    await harness.engine.wake(task_id)

    await eventually(lambda: notes(harness, task_id) == ["skip Amazon"], "the note")
    assert "owner_edit" in harness.events(task_id)


async def test_notes_from_different_channels_close_together_make_the_task_ask(
    harness: Harness, make_user: Callable[..., User]
) -> None:
    slow_steps(harness)
    task_id = harness.new_task(make_user())
    await harness.engine.enqueue(task_id)
    await eventually(lambda: len(harness.executed) >= 1, "the first step")

    instruct(harness, task_id, "edit", "use Best Buy", channel="voice", age="20 seconds")
    instruct(harness, task_id, "edit", "use Costco", channel="sms")
    await harness.engine.wake(task_id)

    await eventually(lambda: harness.status(task_id)["status"] == "waiting_user", "the question")
    await eventually(lambda: bool(harness.texts), "the text")
    asked = harness.texts[-1][1]
    assert "1) by voice: use Best Buy. 2) by text: use Costco." in asked and "Reply 1" in asked
    assert notes(harness, task_id) == []  # nothing applied until the owner chooses
    assert "edit_conflict" in harness.events(task_id)


@pytest.mark.parametrize(
    ("choice", "kept"),
    [("1", ["use Best Buy"]), ("2", ["use Costco"]), ("3", ["use Best Buy", "use Costco"])],
)
async def test_the_owners_choice_decides_which_note_stands(
    harness: Harness, make_user: Callable[..., User], choice: str, kept: list[str]
) -> None:
    slow_steps(harness)
    task_id = harness.new_task(make_user())
    await harness.engine.enqueue(task_id)
    await eventually(lambda: len(harness.executed) >= 1, "the first step")
    instruct(harness, task_id, "edit", "use Best Buy", channel="voice", age="20 seconds")
    instruct(harness, task_id, "edit", "use Costco", channel="sms")
    await harness.engine.wake(task_id)
    await eventually(lambda: harness.status(task_id)["status"] == "waiting_user", "the question")

    instruct(harness, task_id, "answer", choice)
    await harness.engine.wake(task_id)

    await eventually(lambda: harness.status(task_id)["status"] == "running", "carrying on")
    assert notes(harness, task_id) == kept
    assert "edit_conflict_resolved" in harness.events(task_id)


async def test_an_answer_that_is_not_a_choice_leaves_the_question_open(
    harness: Harness, make_user: Callable[..., User]
) -> None:
    slow_steps(harness)
    task_id = harness.new_task(make_user())
    await harness.engine.enqueue(task_id)
    await eventually(lambda: len(harness.executed) >= 1, "the first step")
    instruct(harness, task_id, "edit", "use Best Buy", channel="voice", age="20 seconds")
    instruct(harness, task_id, "edit", "use Costco", channel="sms")
    await harness.engine.wake(task_id)
    await eventually(lambda: harness.status(task_id)["status"] == "waiting_user", "the question")

    instruct(harness, task_id, "answer", "whichever")
    await harness.engine.wake(task_id)

    await eventually(lambda: any("reply 1, 2 or 3" in b for _, b in harness.texts), "the nudge")
    assert harness.status(task_id)["status"] == "waiting_user"


async def test_notes_from_the_same_channel_or_far_apart_do_not_conflict(
    harness: Harness, make_user: Callable[..., User]
) -> None:
    slow_steps(harness)
    task_id = harness.new_task(make_user())
    await harness.engine.enqueue(task_id)
    await eventually(lambda: len(harness.executed) >= 1, "the first step")

    instruct(harness, task_id, "edit", "add Best Buy", channel="sms", age="10 seconds")
    instruct(harness, task_id, "edit", "make it three", channel="sms")
    instruct(harness, task_id, "edit", "by Friday", channel="voice", age="5 minutes")
    await harness.engine.wake(task_id)

    await eventually(lambda: len(notes(harness, task_id)) == 3, "all three notes")
    assert harness.status(task_id)["status"] == "running"


async def test_the_owner_answers_the_agents_own_question_and_the_task_goes_on(
    harness: Harness, make_user: Callable[..., User]
) -> None:
    asked = False

    async def executor(task_id: UUID) -> StepResult:
        nonlocal asked
        harness.executed.append(task_id)
        if not asked:
            asked = True
            raise WaitForUser("clarify", "Did the order go through?")
        return StepResult(1, finished=True)

    harness.runtime.executor = executor
    task_id = harness.new_task(make_user())
    await harness.engine.enqueue(task_id)
    await eventually(lambda: harness.status(task_id)["status"] == "waiting_user", "the question")

    instruct(harness, task_id, "answer", "No, it failed")
    await harness.engine.wake(task_id)

    await eventually(lambda: harness.status(task_id)["status"] == "completed", "completion")
    assert "owner_answer" in harness.events(task_id)


async def test_an_answer_to_a_task_that_is_not_asking_is_ignored(
    harness: Harness, make_user: Callable[..., User]
) -> None:
    slow_steps(harness)
    task_id = harness.new_task(make_user())
    await harness.engine.enqueue(task_id)
    await eventually(lambda: len(harness.executed) >= 1, "the first step")

    instruct(harness, task_id, "answer", "yes")
    await harness.engine.wake(task_id)

    await eventually(
        lambda: (
            harness.admin.execute(
                "select outcome from public.instructions where task_id = %s", (task_id,)
            ).fetchone()
            == {"outcome": "ignored: task was running"}
        ),
        "the instruction to be dealt with",
    )
    assert harness.status(task_id)["status"] == "running"
