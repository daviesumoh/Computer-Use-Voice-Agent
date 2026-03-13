"""A worker process for the crash test: runs the engine with a slow scripted executor.

Usage: python -m tests.durable_runner STEP_LOG [TASK_ID]
With a TASK_ID it enqueues that task; without one it only launches, which recovers any
workflow a previous (killed) process left unfinished.
"""

import asyncio
import sys
from pathlib import Path
from uuid import UUID

from tests.conftest import WORKER_DSN
from worker.db import create_pool
from worker.engine import Engine, Runtime, StepResult
from worker.settings import load_settings

TOTAL_STEPS = 6


async def main(step_log: Path, task_id: UUID | None) -> None:
    pool = create_pool(load_settings())
    await pool.open(wait=True)

    async def notify(user_id: UUID, dedupe_key: str, body: str) -> None:
        pass

    async def executor(_: UUID) -> StepResult:
        with step_log.open("a") as log:
            log.write("step\n")
        done = len(step_log.read_text().splitlines())
        await asyncio.sleep(0.4)
        return StepResult(1, finished=done >= TOTAL_STEPS)

    engine = Engine(Runtime(pool=pool, notify=notify, executor=executor), WORKER_DSN)
    await engine.start()
    if task_id:
        await engine.enqueue(task_id)
    await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]), UUID(sys.argv[2]) if len(sys.argv) > 2 else None))
