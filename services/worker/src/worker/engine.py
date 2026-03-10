"""Durable task execution: one DBOS workflow per task, stored in the same Postgres.

A workflow runs a task step by step. Between steps (the safe points) it applies the owner's
pause / resume / kill instructions and enforces the budget. After a crash or redeploy DBOS
resumes the workflow from its last completed step.
"""

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from uuid import UUID

from dbos import DBOS, Queue, SetWorkflowID
from dbos._error import DBOSNonExistentWorkflowError
from psycopg.types.json import Jsonb

from worker import approvals, edits, limits
from worker.db import Conn, Pool
from worker.memory import jobs as memory_jobs
from worker.transitions import TERMINAL, InvalidTransitionError, record_event, transition

log = logging.getLogger(__name__)

QUEUE_NAME = "tasks"
MEMORY_QUEUE_NAME = "memory"
WAKE_TOPIC = "wake"
MAX_STEPS = 150
Notify = Callable[[UUID, str, str], Awaitable[None]]  # user_id, dedupe_key, text


@dataclass(frozen=True)
class StepResult:
    cost_cents: int
    finished: bool = False
    summary: str = ""


class TaskFailure(Exception):
    """An executor's way to end a task as failed, with a class for the dashboard and a reason."""

    def __init__(self, error_class: str, reason: str) -> None:
        super().__init__(reason)
        self.error_class = error_class
        self.reason = reason


class WaitForUser(Exception):
    """An executor's way to park the task until the owner acts (approve, log in, answer)."""

    def __init__(self, reason: Literal["approval", "login", "clarify"], message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


Executor = Callable[[UUID], Awaitable[StepResult]]


async def no_executor(task_id: UUID) -> StepResult:
    raise TaskFailure("executor_unavailable", "browser execution is not configured")


async def no_release(task_id: UUID) -> None:
    return None


@dataclass
class Runtime:
    pool: Pool
    notify: Notify
    executor: Executor = no_executor
    # Frees what the executor holds for a task (its browser) when the task stops running.
    release: Callable[[UUID], Awaitable[None]] = no_release
    dashboard_url: str | None = None  # where texts send the owner to approve or take over
    poll_seconds: float = 60.0  # paused tasks re-check their inbox this often even without a wake
    memory: memory_jobs.Memory | None = None  # None: nothing is learned from calls and tasks


class Dispatcher(Protocol):
    """What the HTTP layer needs from the engine."""

    async def enqueue(self, task_id: UUID) -> None: ...

    async def wake(self, task_id: UUID) -> None: ...

    async def remember_conversation(self, conversation_id: UUID) -> None: ...


_runtime: Runtime | None = None
_memory_queue: Queue | None = None


def _rt() -> Runtime:
    if _runtime is None:
        raise RuntimeError("engine used before start()")
    return _runtime


class Engine:
    """Owns the DBOS lifecycle for this process."""

    def __init__(self, runtime: Runtime, system_database_url: str) -> None:
        global _runtime
        _runtime = runtime
        DBOS(config={"name": "agent-worker", "system_database_url": system_database_url})
        self._queue: Queue | None = None

    async def start(self) -> None:
        global _memory_queue
        DBOS.launch()
        # One browser at a time on the small free-tier worker.
        self._queue = await DBOS.register_queue_async(QUEUE_NAME, global_concurrency=1)
        # Memory jobs run one at a time so two never compare a new statement with the same
        # known ones and each store it.
        _memory_queue = await DBOS.register_queue_async(MEMORY_QUEUE_NAME, global_concurrency=1)

    def stop(self) -> None:
        DBOS.destroy()

    async def enqueue(self, task_id: UUID) -> None:
        """Idempotent: the workflow id is the task id, so a repeat attaches to the first run."""
        assert self._queue is not None
        with SetWorkflowID(str(task_id)):
            await self._queue.enqueue_async(run_task, str(task_id))
        async with _rt().pool.connection() as conn:
            await conn.execute(
                "update public.tasks set workflow_id = %s where id = %s and workflow_id is null",
                (str(task_id), task_id),
            )

    async def wake(self, task_id: UUID) -> None:
        try:
            await DBOS.send_async(str(task_id), "wake", WAKE_TOPIC)
        except DBOSNonExistentWorkflowError:
            log.info("no workflow to wake", extra={"task_id": str(task_id)})

    async def remember_conversation(self, conversation_id: UUID) -> None:
        """Idempotent like enqueue: the workflow id is derived from the conversation."""
        if _rt().memory is None or _memory_queue is None:
            return
        with SetWorkflowID(f"learn-call:{conversation_id}"):
            await _memory_queue.enqueue_async(learn_from_conversation, str(conversation_id))


async def _remember_task(task_id: str) -> None:
    if _rt().memory is None or _memory_queue is None:
        return
    with SetWorkflowID(f"learn-task:{task_id}"):
        await _memory_queue.enqueue_async(learn_from_task, task_id)


# Model calls can fail briefly; the job is safe to repeat (see memory.jobs).
@DBOS.step(retries_allowed=True, max_attempts=3, interval_seconds=5)
async def _learn_from_conversation(conversation_id: str) -> str:
    memory = _rt().memory
    assert memory is not None
    async with _rt().pool.connection() as conn:
        return await memory_jobs.learn_from_conversation(conn, memory, UUID(conversation_id))


@DBOS.workflow()
async def learn_from_conversation(conversation_id: str) -> str:
    return await _learn_from_conversation(conversation_id)


@DBOS.step(retries_allowed=True, max_attempts=3, interval_seconds=5)
async def _learn_from_task(task_id: str) -> str:
    memory = _rt().memory
    assert memory is not None
    async with _rt().pool.connection() as conn:
        return await memory_jobs.learn_from_task(conn, memory, UUID(task_id))


@DBOS.workflow()
async def learn_from_task(task_id: str) -> str:
    return await _learn_from_task(task_id)


async def _task(conn: Conn, task_id: str) -> dict[str, Any]:
    row = await (
        await conn.execute(
            "select id, user_id, short_code, title, status, waiting_reason, version, spent_cents, "
            "budget_cents, spec "
            "from public.tasks where id = %s",
            (task_id,),
        )
    ).fetchone()
    assert row is not None, f"task {task_id} vanished"
    return row


def _dollars(cents: int) -> str:
    return f"${cents / 100:.2f}"


@DBOS.step()
async def _begin(task_id: str) -> bool:
    """Moves queued -> running. False when the task was cancelled while waiting its turn."""
    async with _rt().pool.connection() as conn:
        task = await _task(conn, task_id)
        if task["status"] == "queued":
            try:
                await transition(conn, task["id"], "running", event_type="started")
            except InvalidTransitionError as lost:
                return lost.current not in TERMINAL
        return task["status"] not in TERMINAL


@DBOS.step()
async def _apply_instructions(task_id: str) -> Literal["run", "wait", "hold", "stop"]:
    """The safe point: applies pending instructions in order, then reports what to do next."""
    runtime = _rt()
    async with runtime.pool.connection() as conn:
        task = await _task(conn, task_id)
        pending = await (
            await conn.execute(
                "select id, seq, command, channel, body, received_at from public.instructions "
                "where task_id = %s and applied_at is null order by seq",
                (task_id,),
            )
        ).fetchall()
        for instruction in pending:
            outcome = await _apply(conn, runtime, task, instruction)
            await conn.execute(
                "update public.instructions set applied_at = now(), outcome = %s where id = %s",
                (outcome, instruction["id"]),
            )

        await _release_expired_wait(conn, runtime, task)

        stopped = await (
            await conn.execute(
                "select emergency_stop from public.user_settings where user_id = %s",
                (task["user_id"],),
            )
        ).fetchone()
        status = (await _task(conn, task_id))["status"]
        if stopped and stopped["emergency_stop"] and status == "running":
            await transition(conn, task["id"], "paused", event_type="emergency_stop")
            status = "paused"
    if status in TERMINAL:
        return "stop"
    if status == "running":
        return "run"
    # A task waiting on the owner keeps its browser (the page they must act on is in it);
    # a paused one gives it back.
    return "hold" if status == "waiting_user" else "wait"


async def _release_expired_wait(conn: Conn, runtime: Runtime, task: dict[str, Any]) -> None:
    """A task waiting on an approval nobody answered goes back to work instead of hanging."""
    if (task["status"], task["waiting_reason"]) != ("waiting_user", "approval"):
        return
    if not await approvals.expire_stale(conn, task["id"]):
        return
    await transition(
        conn,
        task["id"],
        "running",
        event_type="approval_expired",
        payload={"summary": "the approval request expired unanswered"},
    )
    await runtime.notify(
        task["user_id"],
        f"task:{task['id']}:approval_expired:{task['version']}",
        f"{task['short_code']}: the approval request expired; I'll look for another way.",
    )


async def _apply(
    conn: Conn, runtime: Runtime, task: dict[str, Any], instruction: dict[str, Any]
) -> str:
    command = instruction["command"]
    if command == "edit":
        return await _apply_edit(conn, runtime, task, instruction)
    if command == "answer":
        return await _apply_answer(conn, runtime, task, instruction)
    code, user_id = task["short_code"], task["user_id"]
    key = f"instruction:{instruction['id']}"
    current = (await _task(conn, str(task["id"])))["status"]
    try:
        if command == "pause":
            await transition(conn, task["id"], "paused", event_type="paused")
            await runtime.notify(user_id, key, f"{code} paused. Reply RESUME {code} to continue.")
        elif command == "resume":
            return await _resume(conn, runtime, task, key)
        else:
            await transition(conn, task["id"], "cancelled", event_type="cancelled")
            await runtime.notify(user_id, key, f"{code} cancelled.")
    except InvalidTransitionError:
        await runtime.notify(user_id, key, f"{code} is {current}; {command} had no effect.")
        return f"ignored: task was {current}"
    return "applied"


CHANNEL_WORDS = {"sms": "by text", "voice": "by voice", "dashboard": "in the dashboard"}


async def _apply_edit(
    conn: Conn, runtime: Runtime, task: dict[str, Any], instruction: dict[str, Any]
) -> str:
    """Adds the owner's note to the task, unless another channel said something close in time:
    then the task stops and asks which one stands, because nothing here can tell whether two
    free-text notes contradict each other."""
    task_id, code = task["id"], task["short_code"]
    earlier = await (
        await conn.execute(
            "select id, channel, body from public.instructions "
            "where task_id = %s and command = 'edit' and channel <> %s and seq < %s "
            "and received_at between %s - make_interval(secs => %s) "
            "and %s + make_interval(secs => %s) order by seq limit 1",
            (
                task_id,
                instruction["channel"],
                instruction["seq"],
                instruction["received_at"],
                edits.CONFLICT_WINDOW_SECONDS,
                instruction["received_at"],
                edits.CONFLICT_WINDOW_SECONDS,
            ),
        )
    ).fetchone()
    current = (await _task(conn, str(task_id)))["status"]
    if earlier is None or current in TERMINAL:
        await _add_note(conn, task, instruction)
        return "applied" if current not in TERMINAL else f"ignored: task was {current}"

    # Nothing from either note applies until the owner chooses.
    await conn.execute(
        "update public.tasks set spec = jsonb_set(spec, '{owner_edits}', coalesce((select "
        "jsonb_agg(n) from jsonb_array_elements(spec -> 'owner_edits') n "
        "where n ->> 'instruction_id' <> %s), '[]'::jsonb)) where id = %s",
        (str(earlier["id"]), task_id),
    )
    options = [
        {"id": str(row["id"]), "channel": row["channel"], "text": row["body"]}
        for row in (earlier, instruction)
    ]
    first, second = (f"{CHANNEL_WORDS[o['channel']]}: {o['text'][:120]}" for o in options)
    await transition(
        conn,
        task_id,
        "waiting_user",
        event_type="edit_conflict",
        payload={"summary": f"two notes close together: {first} / {second}", "options": options},
        waiting_reason="clarify",
    )
    await runtime.notify(
        task["user_id"],
        f"instruction:{instruction['id']}",
        f"{code}: two notes came close together. 1) {first}. 2) {second}. "
        "Reply 1 for the first, 2 for the second, or 3 for both.",
    )
    return "held: conflicting notes"


async def _add_note(conn: Conn, task: dict[str, Any], instruction: dict[str, Any]) -> None:
    note = {
        "instruction_id": str(instruction["id"]),
        "channel": instruction["channel"],
        "text": instruction["body"],
    }
    await conn.execute(
        "update public.tasks set spec = jsonb_set(spec, '{owner_edits}', "
        "coalesce(spec -> 'owner_edits', '[]'::jsonb) || %s::jsonb) where id = %s",
        (Jsonb([note]), task["id"]),
    )
    await record_event(
        conn,
        task["id"],
        "owner_edit",
        {"summary": f"the owner said {CHANNEL_WORDS[note['channel']]}: {note['text']}"},
    )


async def _apply_answer(
    conn: Conn, runtime: Runtime, task: dict[str, Any], instruction: dict[str, Any]
) -> str:
    """The owner's reply to a question: either the agent's own, or which of two conflicting
    notes stands."""
    task_id, code = task["id"], task["short_code"]
    now = await _task(conn, str(task_id))
    if (now["status"], now["waiting_reason"]) != ("waiting_user", "clarify"):
        return f"ignored: task was {now['status']}"
    asked = await (
        await conn.execute(
            "select type, payload from public.task_events where task_id = %s "
            "and type in ('needs_clarify', 'edit_conflict') order by seq desc limit 1",
            (task_id,),
        )
    ).fetchone()
    reply = instruction["body"].strip()
    if asked is not None and asked["type"] == "edit_conflict":
        choice = reply.rstrip(".!").lower()
        options = asked["payload"]["options"]
        if choice not in {"1", "2", "3"}:
            await runtime.notify(
                task["user_id"], f"instruction:{instruction['id']}", f"{code}: reply 1, 2 or 3."
            )
            return "ignored: not a choice"
        keep = {"1": options[:1], "2": options[1:], "3": options}[choice]
        for option in keep:
            await _add_note(
                conn,
                task,
                {"id": option["id"], "channel": option["channel"], "body": option["text"]},
            )
        await transition(
            conn, task_id, "running", event_type="edit_conflict_resolved",
            payload={"summary": f"the owner chose {choice}"},
        )  # fmt: skip
        return "applied"
    await transition(
        conn,
        task_id,
        "running",
        event_type="owner_answer",
        payload={"summary": f"the owner answered: {reply}"},
    )
    return "applied"


async def _resume(conn: Conn, runtime: Runtime, task: dict[str, Any], key: str) -> str:
    code, user_id = task["short_code"], task["user_id"]
    settings = await (
        await conn.execute(
            "select emergency_stop, default_task_budget_cents from public.user_settings "
            "where user_id = %s",
            (user_id,),
        )
    ).fetchone()
    if settings and settings["emergency_stop"]:
        await runtime.notify(user_id, key, f"Emergency stop is on; {code} stays paused.")
        return "blocked: emergency stop"

    if await limits.over_monthly_cap(conn, user_id):
        await runtime.notify(
            user_id, key, f"The monthly spending cap is reached; {code} stays paused."
        )
        return "blocked: monthly cap"

    fresh = await _task(conn, str(task["id"]))
    await conn.execute(
        "update public.tasks set active_seconds = 0 where id = %s and active_seconds >= %s",
        (task["id"], limits.MAX_ACTIVE_SECONDS),
    )  # resuming after the time limit grants another allowance
    extended = fresh["spent_cents"] >= fresh["budget_cents"]
    if extended:
        # A budget pause can only continue with more budget: grant one more default allowance.
        allowance = settings["default_task_budget_cents"] if settings else 300
        await conn.execute(
            "update public.tasks set budget_cents = spent_cents + %s where id = %s",
            (allowance, task["id"]),
        )
    await transition(
        conn, task["id"], "running", event_type="resumed", payload={"budget_extended": extended}
    )
    note = f" Budget extended by {_dollars(allowance)}." if extended else ""
    await runtime.notify(user_id, key, f"{code} resumed.{note}")
    return "applied"


@DBOS.step()
async def _run_step(task_id: str) -> Literal["continue", "stop"]:
    runtime = _rt()
    async with runtime.pool.connection() as conn:
        task = await _task(conn, task_id)
        code, user_id = task["short_code"], task["user_id"]
        if await limits.over_monthly_cap(conn, user_id):
            await transition(conn, task["id"], "paused", event_type="monthly_cap")
            await runtime.notify(
                user_id,
                f"task:{task_id}:monthly_cap",
                f"{code} paused: the monthly spending cap is reached. Raise it in settings, "
                "then reply RESUME.",
            )
            return "continue"
        began = time.monotonic()
        try:
            result = await runtime.executor(task["id"])
        except WaitForUser as wait:
            seq = await transition(
                conn,
                task["id"],
                "waiting_user",
                event_type=f"needs_{wait.reason}",
                payload={"summary": wait.message},
                waiting_reason=wait.reason,
            )
            link = f" {runtime.dashboard_url}/tasks/{task_id}" if runtime.dashboard_url else ""
            await runtime.notify(
                user_id, f"task:{task_id}:waiting:{seq}", f"{code} needs you: {wait.message}{link}"
            )
            return "continue"
        except TaskFailure as failure:
            await transition(
                conn,
                task["id"],
                "failed",
                event_type="failed",
                payload={"reason": failure.reason},
                error_class=failure.error_class,
            )
            await runtime.notify(
                user_id, f"task:{task_id}:failed", f"{code} failed: {failure.reason}"
            )
            return "stop"

        spent = await (
            await conn.execute(
                "update public.tasks set spent_cents = spent_cents + %s, "
                "active_seconds = active_seconds + %s where id = %s "
                "returning spent_cents, budget_cents, active_seconds",
                (result.cost_cents, round(time.monotonic() - began), task["id"]),
            )
        ).fetchone()
        assert spent is not None
        if result.finished:
            await transition(conn, task["id"], "completed", event_type="completed")
            await runtime.notify(
                user_id, f"task:{task_id}:completed", f"{code} done. {result.summary}".strip()
            )
            return "stop"
        if spent["spent_cents"] >= spent["budget_cents"]:
            await transition(
                conn,
                task["id"],
                "paused",
                event_type="budget_exhausted",
                payload={"spent_cents": spent["spent_cents"]},
            )
            await runtime.notify(
                user_id,
                f"task:{task_id}:budget:{spent['budget_cents']}",
                f"{code} paused at its {_dollars(spent['budget_cents'])} budget. "
                f"Reply RESUME {code} to allow more, or KILL {code}.",
            )
        elif spent["active_seconds"] >= limits.MAX_ACTIVE_SECONDS:
            await transition(
                conn,
                task["id"],
                "paused",
                event_type="time_limit",
                payload={"active_seconds": spent["active_seconds"]},
            )
            await runtime.notify(
                user_id,
                f"task:{task_id}:time_limit",
                f"{code} paused after {limits.MAX_ACTIVE_SECONDS // 3600} hours of work. "
                f"Reply RESUME {code} to allow more, or KILL {code}.",
            )
    return "continue"


@DBOS.step()
async def _fail_unexpectedly(task_id: str) -> None:
    async with _rt().pool.connection() as conn:
        task = await _task(conn, task_id)
        try:
            await transition(
                conn, task["id"], "failed", event_type="failed", error_class="internal"
            )
        except InvalidTransitionError:
            return  # already finished or cancelled
        await _rt().notify(
            task["user_id"],
            f"task:{task_id}:failed",
            f"{task['short_code']} failed unexpectedly; details are in the dashboard.",
        )


@DBOS.step()
async def _release(task_id: str) -> None:
    await _rt().release(UUID(task_id))


@DBOS.workflow()
async def run_task(task_id: str) -> str:
    try:
        outcome = await _drive(task_id)
    except Exception:
        await _fail_unexpectedly(task_id)
        await _release(task_id)
        await _remember_task(task_id)
        raise
    await _remember_task(task_id)
    return outcome


async def _drive(task_id: str) -> str:
    steps = 0
    if not await _begin(task_id):
        return "not_started"
    while steps < MAX_STEPS:  # only executed steps count; waiting while paused does not
        state = await _apply_instructions(task_id)
        if state in {"wait", "stop"}:
            await _release(task_id)  # a paused or finished task holds no browser
        if state == "stop":
            return "stopped"
        if state in {"wait", "hold"}:
            await DBOS.recv_async(WAKE_TOPIC, timeout_seconds=_rt().poll_seconds)
            continue
        steps += 1
        if await _run_step(task_id) == "stop":
            await _release(task_id)
            return "stopped"
    await _step_limit(task_id)
    return "step_limit"


@DBOS.step()
async def _step_limit(task_id: str) -> None:
    async with _rt().pool.connection() as conn:
        task = await _task(conn, task_id)
        await transition(
            conn,
            task["id"],
            "failed",
            event_type="failed",
            error_class="step_limit",
            payload={"max_steps": MAX_STEPS},
        )
        await _rt().notify(
            task["user_id"],
            f"task:{task_id}:failed",
            f"{task['short_code']} stopped after {MAX_STEPS} steps without finishing.",
        )
