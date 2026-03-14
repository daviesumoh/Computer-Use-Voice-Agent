"""A task survives its worker process being killed mid-run."""

import asyncio
import os
import signal
import sys
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import DictRow

from tests.conftest import HMAC_SECRET, WORKER_DSN, User
from tests.durable_runner import TOTAL_STEPS

pytestmark = pytest.mark.db

ENV = {
    **os.environ,
    "WORKER_DATABASE_URL": WORKER_DSN,
    "WORKER_SERVICE_HMAC_SECRET": HMAC_SECRET,
    "WORKER_TWILIO_ACCOUNT_SID": "AC" + "0" * 32,
    "WORKER_TWILIO_API_KEY_SID": "SK" + "0" * 32,
    "WORKER_TWILIO_API_KEY_SECRET": "unused-in-this-test",
    "WORKER_TWILIO_FROM_NUMBER": "+14165550100",
    "PYTHONPATH": os.pathsep.join(["src", "../common/src", "."]),
}


async def spawn(*args: str) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        sys.executable, "-m", "tests.durable_runner", *args, env=ENV
    )


async def test_killing_the_worker_mid_task_loses_no_progress(
    make_user: Callable[..., User], admin: psycopg.Connection[DictRow], tmp_path: Path
) -> None:
    if admin.execute("select to_regclass('dbos.workflow_status') as t").fetchone() == {
        "t": "dbos.workflow_status"
    }:
        admin.execute("truncate dbos.workflow_status cascade")
    row = admin.execute(
        "insert into public.tasks (user_id, short_code, title, spec, status, budget_cents) "
        "values (%s, 'T1', 'Long job', '{}', 'queued', 300) returning id",
        (make_user().id,),
    ).fetchone()
    assert row is not None
    task_id = UUID(str(row["id"]))
    step_log = tmp_path / "steps.log"
    step_log.touch()

    first = await spawn(str(step_log), str(task_id))
    for _ in range(200):
        if len(step_log.read_text().splitlines()) >= 2:
            break
        await asyncio.sleep(0.1)
    else:
        first.kill()
        pytest.fail("the first worker never ran two steps")
    first.send_signal(signal.SIGKILL)
    await first.wait()
    ran_before_kill = len(step_log.read_text().splitlines())
    assert admin.execute(
        "select status from public.tasks where id = %s", (task_id,)
    ).fetchone() == {"status": "running"}

    second = await spawn(str(step_log))
    try:
        for _ in range(300):
            status = admin.execute(
                "select status from public.tasks where id = %s", (task_id,)
            ).fetchone()
            if status == {"status": "completed"}:
                break
            await asyncio.sleep(0.1)
        else:
            pytest.fail("the restarted worker did not finish the task")
    finally:
        second.kill()
        await second.wait()

    executed = len(step_log.read_text().splitlines())
    # Completed steps are not repeated; only the step in flight at the kill may run twice.
    assert TOTAL_STEPS <= executed <= TOTAL_STEPS + 1, (ran_before_kill, executed)
