"""Shared test doubles and helpers for the browser tests."""

from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import DictRow

from worker.browser.actions import Action, Click, FillSecret
from worker.browser.decider import Brief, Decision, Extraction, Usage
from worker.browser.snapshot import parse
from worker.secret_store import SecretStoreError

USAGE = Usage("gpt-6.1-sol", input_tokens=2_000, cached_tokens=0, output_tokens=100, latency_ms=5)
Plan = Action | Callable[[Brief], Action]


class ScriptedDecider:
    """Plays a fixed list of actions; a callable entry can pick its action from the page."""

    def __init__(self, plan: list[Plan]) -> None:
        self._plan = list(plan)
        self.briefs: list[Brief] = []
        self.pictures: list[tuple[str, bytes, str]] = []  # (question, JPEG, detail) per look

    async def decide(self, brief: Brief) -> Decision:
        self.briefs.append(brief)
        step = self._plan.pop(0)
        return Decision(step(brief) if callable(step) else step, USAGE)

    async def extract(self, question: str, page_text: str) -> Extraction:
        return Extraction(page_text[:400], USAGE)

    async def look(self, question: str, picture: bytes, detail: str) -> Extraction:
        self.pictures.append((question, picture, detail))
        return Extraction("a bar chart: Q3 revenue is 42", USAGE)


def ref_of(role: str, name: str) -> Callable[[Brief], str]:
    def find(brief: Brief) -> str:
        for node in parse(brief.observation.snapshot).values():
            if (node.role, node.name) == (role, name):
                return node.ref
        raise AssertionError(f"no {role} '{name}' in:\n{brief.observation.snapshot}")

    return find


def click(role: str, name: str) -> Callable[[Brief], Action]:
    finder = ref_of(role, name)
    return lambda brief: Click(kind="click", ref=finder(brief))


def events(admin: psycopg.Connection[DictRow], task_id: UUID, *types: str) -> list[dict[str, Any]]:
    rows = admin.execute(
        "select type, payload from public.task_events where task_id = %s and type = any(%s) "
        "order by seq",
        (task_id, list(types)),
    )
    return [{"type": r["type"], **r["payload"]} for r in rows.fetchall()]


class InMemorySecretStore:
    """Stands in for Bitwarden; keeps what was stored so tests can inspect it."""

    def __init__(self) -> None:
        self.secrets: dict[str, str] = {}
        self.fail_delete = False

    async def create(self, name: str, value: str) -> str:
        ref = str(uuid4())
        self.secrets[ref] = value
        return ref

    async def read(self, ref: str) -> str:
        return self.secrets[ref]

    async def delete(self, ref: str) -> None:
        if self.fail_delete:
            raise SecretStoreError("simulated outage")
        self.secrets.pop(ref, None)


def fill(role: str, name: str, account_id: UUID, field: str) -> Callable[[Brief], Action]:
    finder = ref_of(role, name)
    return lambda brief: FillSecret.model_validate(
        {"kind": "fill_secret", "ref": finder(brief), "account_id": account_id, "field": field}
    )
