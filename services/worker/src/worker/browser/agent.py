"""One browser step per call: observe, decide, check policy, act, record."""

import asyncio
import hashlib
import logging
import math
from collections.abc import Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from playwright.async_api import Error as PlaywrightError

from worker import approvals, ledger
from worker.browser import sessions
from worker.browser.actions import (
    Action,
    Click,
    Done,
    Extract,
    FillSecret,
    Look,
    Navigate,
    Select,
    Type,
    fingerprint,
)
from worker.browser.credentials import CredentialRefusedError, OriginMismatchError, fill_credential
from worker.browser.decider import Brief, Decider, Usage
from worker.browser.errors import (
    IrrecoverableError,
    NeedsUserError,
    PictureRefusedError,
    RecoverableError,
    SecretLeakError,
)
from worker.browser.policy import (
    IRREVERSIBLE,
    ActionClass,
    Scope,
    check,
    scope_from_json,
)
from worker.browser.session import ACTION_TIMEOUT_MS, BrowserSession, Observation
from worker.browser.snapshot import Node
from worker.db import Pool
from worker.engine import StepResult, TaskFailure, WaitForUser
from worker.memory import procedures, recall
from worker.memory.jobs import Memory
from worker.secret_store import SecretStore
from worker.transitions import record_event

log = logging.getLogger(__name__)

RETRY_DELAYS = (1.0, 4.0, 15.0)  # seconds between attempts at a recoverable failure
HISTORY_WINDOW = 15  # recent steps shown to the model
HISTORY_EVENTS = (
    "step",
    "step_failed",
    "denied",
    "approval_denied",
    "approval_expired",
    "owner_answer",
    "owner_edit",
)
REPEAT_LIMIT = 3  # the same action this many times means it is going nowhere
FAILURE_STREAK_LIMIT = 5  # consecutive steps that could not act
MAX_LOOKS = 6  # pictures per task: each costs several ordinary steps


@dataclass
class _Run:
    """What lives in memory for one task: its browser. Everything else is in task_events."""

    stack: AsyncExitStack
    session: BrowserSession
    user_id: UUID
    accounts: list[dict[str, Any]]
    notes: tuple[str, ...] = ()  # the owner's preferences that bear on this task
    route: str = ""  # a remembered route for the site the browser is on
    route_domain: str | None = None  # the site `route` was looked up for


@dataclass
class BrowserExecutor:
    pool: Pool
    decider: Decider
    headless: bool
    store: SecretStore | None = None  # where website logins live; None means no logins
    session_key: bytes | None = None  # encrypts saved logins; None means none are saved
    scope: Scope | None = None  # overrides each task's own scope; for tests and fixed setups
    allow_private_hosts: bool = False
    memory: Memory | None = None  # where preferences and remembered routes come from
    retry_delays: Sequence[float] = RETRY_DELAYS
    action_timeout_ms: int = ACTION_TIMEOUT_MS
    _runs: dict[UUID, _Run] = field(default_factory=dict)

    async def __call__(self, task_id: UUID) -> StepResult:
        task = await self._load(task_id)
        history = await self._history(task_id)
        self._guard_against_stalling(history)

        accounts = await self._accounts(task)
        run = await self._run(task, history, accounts)
        try:
            observation = await run.session.observe()
        except RecoverableError as lost:
            # The page or browser went away. Start over from the last known URL next step.
            await self.release(task_id)
            await self._record(task_id, "step_failed", {"summary": f"lost the page: {lost}"})
            return StepResult(0)

        await self._remember_route(task, run, observation.url)
        decision = await self.decider.decide(
            Brief(
                task["title"],
                task["spec"],
                [h["summary"] for h in history],
                observation,
                tuple(f"{a['id']} - {a['label']} ({a['site_domain']})" for a in accounts),
                run.notes,
                run.route,
            )
        )
        cost = await self._bill(task, decision.usage, "browser_decide")
        action = decision.action

        if (stuck := self._repeats(action, history)) >= REPEAT_LIMIT:
            raise TaskFailure("loop_detected", f"kept repeating the same action ({stuck} times)")

        if isinstance(action, Done):
            await self._record(task_id, "step", {"summary": "finished", "action": "done"})
            await self.release(task_id)
            return StepResult(cost, finished=True, summary=action.result)

        if isinstance(action, Extract):
            extraction = await self.decider.extract(action.question, await run.session.page_text())
            cost += await self._bill(task, extraction.usage, "browser_extract")
            await self._record(
                task_id,
                "step",
                {
                    "summary": f"read the page for '{action.question}': {extraction.text}",
                    "action": "extract",
                    "fingerprint": fingerprint(action),
                },
            )
            return StepResult(cost)

        if isinstance(action, Look):
            return StepResult(cost + await self._look(task, run, action, observation))

        await self._act(task, run, action, observation, accounts)
        return StepResult(cost)

    async def _look(
        self, task: dict[str, Any], run: _Run, action: Look, observation: Observation
    ) -> int:
        """Takes one picture and has the tool-less model answer the question about it. Only its
        answer, as text, goes into the history. Returns the cost of that call."""
        task_id = task["id"]
        page = hashlib.sha256(f"{observation.url}\n{observation.snapshot}".encode()).hexdigest()[
            :16
        ]
        failed = {"fingerprint": fingerprint(action)}
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "select count(*) as looks, coalesce(bool_or(payload->>'page' = %s), false) "
                    "as same_page from public.task_events where task_id = %s and type = 'step' "
                    "and payload->>'action' = 'look'",
                    (page, task_id),
                )
            ).fetchone()
        assert row is not None
        if row["looks"] >= MAX_LOOKS:
            reason = f"this task has used its {MAX_LOOKS} pictures"
        elif row["same_page"]:
            reason = "the page has not changed since the last picture of it"
        else:
            reason = ""
        if not reason:
            try:
                picture = await run.session.screenshot(action.detail)
            except (PictureRefusedError, RecoverableError) as refused:
                reason = str(refused)
        if reason:
            await self._record(
                task_id, "step_failed", {"summary": f"did not look: {reason}", **failed}
            )
            return 0
        reading = await self.decider.look(action.question, picture, action.detail)
        await self._record(
            task_id,
            "step",
            {
                "summary": f"looked at the page for '{action.question}': {reading.text}",
                "action": "look",
                "page": page,
                **failed,
            },
        )
        return await self._bill(task, reading.usage, "browser_look")

    def session_for(self, task_id: UUID) -> BrowserSession | None:
        """The live browser of a task, for the owner's takeover."""
        run = self._runs.get(task_id)
        return run.session if run else None

    async def checkpoint(self, task_id: UUID) -> None:
        """Saves the task's logins now, e.g. right after the owner signed in by hand."""
        run = self._runs.get(task_id)
        if run is not None:
            await self._save_sessions(run)

    async def release(self, task_id: UUID) -> None:
        """Closes the task's browser (paused, finished or cancelled tasks hold no Chromium)."""
        run = self._runs.pop(task_id, None)
        if run is None:
            return
        try:
            await self._save_sessions(run)
        finally:
            await run.stack.aclose()

    async def reap(self) -> int:
        """Closes browsers whose task is no longer running or waiting for the owner (it was
        cancelled or failed some way that skipped the release). A leaked Chromium holds
        several hundred MB of a 1.5 GB host. Returns how many were closed."""
        if not self._runs:
            return 0
        async with self.pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "select id from public.tasks where id = any(%s) "
                    "and status in ('running', 'waiting_user')",
                    (list(self._runs),),
                )
            ).fetchall()
        alive = {r["id"] for r in rows}
        orphaned = [task_id for task_id in list(self._runs) if task_id not in alive]
        for task_id in orphaned:
            log.warning("closing a browser left behind", extra={"task_id": str(task_id)})
            await self.release(task_id)
        return len(orphaned)

    async def _save_sessions(self, run: _Run) -> None:
        """Keeps what the browser learned (a login) for the next task, encrypted."""
        if not (run.accounts and self.session_key):
            return
        try:
            state = await run.session.export_state()
        except PlaywrightError:
            log.info("browser already gone; saved logins left as they were")
            return
        async with self.pool.connection() as conn:
            for account in run.accounts:
                await sessions.save(
                    conn,
                    self.session_key,
                    account_id=account["id"],
                    user_id=run.user_id,
                    allowed_origins=account["allowed_origins"],
                    state=state,
                )

    # -- one action ---------------------------------------------------------------

    async def _act(
        self,
        task: dict[str, Any],
        run: _Run,
        action: Action,
        observation: Observation,
        accounts: list[dict[str, Any]],
    ) -> None:
        task_id, session = task["id"], run.session
        scope = self.scope or scope_from_json(task["capability_scope"])
        if accounts:  # logging in is part of the task the owner confirmed
            scope = replace(scope, allow=scope.allow | {ActionClass.CREDENTIAL_USE})
        known: Node | None = None
        if isinstance(action, Click | Type | Select | FillSecret):
            known = (await session.nodes()).get(action.ref)
            if known is None:
                await self._record(
                    task_id,
                    "step_failed",
                    {
                        "summary": f"could not {action.kind}: no element {action.ref} on the page",
                        "fingerprint": fingerprint(action),
                    },
                )
                return

        submits_post = (
            isinstance(action, Click)
            and known is not None
            and await session.submits_post_form(known)
        )
        verdict = check(action, known, session.last_typed, scope, submits_post)
        if verdict.effect == "deny":
            await self._record(
                task_id,
                "denied",
                {
                    "summary": f"refused: {verdict.reason}",
                    "action": action.kind,
                    "action_class": verdict.action_class.value,
                    "fingerprint": fingerprint(action),
                },
            )
            return

        if isinstance(action, FillSecret):
            assert known is not None  # ref actions without a known node returned above
            await self._fill(task, session, action, known, accounts)
            return

        entry: int | None = None
        if verdict.effect == "approve":
            hash_ = approvals.intent_hash(
                task_id,
                verdict.action_class.value,
                action.kind,
                known.role if known else "",
                known.name if known else "",
                observation.url,
                observation.snapshot,
            )
            await self._require_approval(task, verdict.reason, verdict.action_class.value, hash_)
            entry = await self._claim(task, verdict.action_class.value, verdict.reason, hash_)
            if entry is None:
                await self._record(
                    task_id,
                    "step_failed",
                    {
                        "summary": f"not repeated: {verdict.reason} was already attempted",
                        "fingerprint": fingerprint(action),
                    },
                )
                return

        # An action that may have changed something is attempted once; only reads are retried.
        delays = (0.0,) if entry else (0.0, *self.retry_delays)
        failure = ""
        for attempt, delay in enumerate(delays):
            if delay:
                await asyncio.sleep(delay)
            reached_page = False
            try:
                node = known
                if isinstance(action, Click | Type | Select | FillSecret):
                    node = await session.resolve(action.ref, known)
                reached_page = True
                outcome = await session.perform(action, node)
            except RecoverableError as error:
                failure = str(error)
                log.info("recoverable failure", extra={"attempt": attempt, "error": failure})
                if entry and not reached_page:
                    # Pre-flight failed, so the page was never touched: provably not done.
                    await self._settle(entry, "not_happened")
                    await self._record(
                        task_id,
                        "step_failed",
                        {"summary": f"could not {action.kind}: {failure}",
                         "fingerprint": fingerprint(action)},
                    )  # fmt: skip
                    return
                continue
            except NeedsUserError as blocked:
                await self._settle(entry, "unknown")
                # The browser stays open on the page that needs the owner (see takeover).
                raise WaitForUser(
                    "login", f"{blocked}. Open the dashboard to take over the browser."
                ) from blocked
            except SecretLeakError as leak:
                await self._audit(task, "secret_leak_blocked", str(leak))
                raise TaskFailure("security_incident", f"stopped: {leak}") from leak
            except IrrecoverableError as unreachable:
                await self._settle(entry, "unknown")
                raise TaskFailure("unreachable", str(unreachable)) from unreachable
            await self._settle(entry, "confirmed")
            await self._record(
                task_id,
                "step",
                {"summary": outcome, "action": action.kind, "url": session.page.url,
                 "fingerprint": fingerprint(action)},
            )  # fmt: skip
            return

        if entry:
            # The outcome is unknown: it may or may not have happened. Never guess; ask.
            await self._settle(entry, "unknown")
            raise WaitForUser(
                "clarify",
                f"I could not confirm whether this went through: {verdict.reason} ({failure}). "
                "Please check the site and tell me.",
            )
        await self._record(
            task_id,
            "step_failed",
            {"summary": f"could not {action.kind}: {failure}", "fingerprint": fingerprint(action)},
        )

    async def _fill(
        self,
        task: dict[str, Any],
        session: BrowserSession,
        action: FillSecret,
        known: Node,
        accounts: list[dict[str, Any]],
    ) -> None:
        account = next((a for a in accounts if a["id"] == action.account_id), None)
        if account is None or self.store is None:
            reason = "that account is not attached to this task"
            await self._record(
                task["id"], "denied", {"summary": f"refused: {reason}", "action": "fill_secret"}
            )
            return
        try:
            node = await session.resolve(action.ref, known)
            outcome = await fill_credential(
                session,
                self.store,
                secret_ref=account["secret_ref"],
                allowed_origins=account["allowed_origins"],
                account_id=action.account_id,
                node=node,
                field=action.field,
            )
        except CredentialRefusedError as refused:
            await self._record(task["id"], "step_failed", {"summary": f"could not fill: {refused}"})
            return
        except OriginMismatchError as foreign:
            await self._audit(task, "credential_origin_mismatch", str(foreign))
            raise TaskFailure("security_incident", f"stopped: {foreign}") from foreign
        await self._record(
            task["id"],
            "step",
            {"summary": outcome, "action": "fill_secret", "url": session.page.url},
        )

    async def _audit(self, task: dict[str, Any], action: str, detail: str) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "insert into audit.events (user_id, actor, action, target_type, target_id, "
                "metadata) values (%s, 'worker', %s, 'task', %s, "
                "jsonb_build_object('detail', %s::text))",
                (task["user_id"], action, str(task["id"]), detail),
            )

    async def _accounts(self, task: dict[str, Any]) -> list[dict[str, Any]]:
        if not task["account_ids"]:
            return []
        async with self.pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "select id, label, site_domain, allowed_origins, secret_ref "
                    "from public.connected_accounts where user_id = %s and id = any(%s)",
                    (task["user_id"], task["account_ids"]),
                )
            ).fetchall()
        return list(rows)

    async def _require_approval(
        self, task: dict[str, Any], what: str, action_class: str, hash_: str
    ) -> None:
        """Returns once an approved request for exactly this action has been spent.

        Otherwise parks the task (WaitForUser) until the owner decides.
        """
        async with self.pool.connection() as conn:
            live = await approvals.find_live(conn, task["id"], hash_)
            if live and live["status"] == "approved" and await approvals.consume(conn, live["id"]):
                return
            if live is None:
                await approvals.request(
                    conn,
                    user_id=task["user_id"],
                    task_id=task["id"],
                    action_class=action_class,
                    summary=f"{task['title']}: {what}",
                    hash_=hash_,
                )
        raise WaitForUser("approval", f"approve or deny: {what}")

    async def _claim(
        self, task: dict[str, Any], action_class: str, what: str, hash_: str
    ) -> int | None:
        if action_class not in {c.value for c in IRREVERSIBLE}:
            return None
        async with self.pool.connection() as conn:
            return await ledger.start(
                conn,
                task_id=task["id"],
                user_id=task["user_id"],
                action_class=action_class,
                description=what,
                intent_hash=hash_,
            )

    async def _settle(self, entry: int | None, phase: str) -> None:
        if entry is not None:
            async with self.pool.connection() as conn:
                await ledger.finish(conn, entry, phase)

    async def _remember_route(self, task: dict[str, Any], run: _Run, url: str) -> None:
        """Looks up a remembered route when the browser arrives on a new site."""
        domain = urlsplit(url).hostname
        if self.memory is None or not domain or domain == run.route_domain:
            return
        run.route_domain = domain
        async with self.pool.connection() as conn:
            found = None
            if await recall.enabled(conn, run.user_id):
                found = await procedures.route_hint(conn, run.user_id, task["id"], domain)
        run.route = found or ""

    # -- state held in the database ----------------------------------------------

    async def _run(
        self, task: dict[str, Any], history: list[dict[str, Any]], accounts: list[dict[str, Any]]
    ) -> _Run:
        task_id = task["id"]
        run = self._runs.get(task_id)
        if run is None:
            saved = None
            if accounts and self.session_key:
                async with self.pool.connection() as conn:
                    saved = await sessions.load(conn, self.session_key, [a["id"] for a in accounts])
            stack = AsyncExitStack()
            session = await stack.enter_async_context(
                BrowserSession(
                    headless=self.headless,
                    allow_private_hosts=self.allow_private_hosts,
                    action_timeout_ms=self.action_timeout_ms,
                    storage_state=saved,
                )
            )
            run = self._runs[task_id] = _Run(
                stack, session, task["user_id"], accounts, await self._preferences(task)
            )
            last_url = next((h["url"] for h in reversed(history) if h.get("url")), None)
            if last_url:
                await session.perform(Navigate(kind="navigate", url=last_url), None)
        return run

    async def _preferences(self, task: dict[str, Any]) -> tuple[str, ...]:
        if self.memory is None:
            return ()
        asked = task["spec"].get("summary", "") if isinstance(task["spec"], dict) else ""
        async with self.pool.connection() as conn:
            found = await recall.recall(
                conn,
                self.memory.embedder,
                task["user_id"],
                f"{task['title']} {asked}",
                types=("rule", "preference"),
                with_episodes=False,
                limit=5,
            )
        return tuple(r.text for r in found)

    async def _load(self, task_id: UUID) -> dict[str, Any]:
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "select id, user_id, short_code, title, spec, capability_scope, account_ids "
                    "from public.tasks where id = %s",
                    (task_id,),
                )
            ).fetchone()
        assert row is not None, f"task {task_id} vanished"
        return row

    async def _history(self, task_id: UUID) -> list[dict[str, Any]]:
        """The last steps, oldest first, newest event last. Survives a worker restart."""
        async with self.pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "select type, payload from public.task_events where task_id = %s "
                    "and type = any(%s) order by seq desc limit %s",
                    (task_id, list(HISTORY_EVENTS), HISTORY_WINDOW),
                )
            ).fetchall()
        return [{"type": r["type"], **r["payload"]} for r in reversed(rows)]

    @staticmethod
    def _repeats(action: Action, history: list[dict[str, Any]]) -> int:
        wanted = fingerprint(action)
        return sum(1 for h in history if h.get("fingerprint") == wanted)

    @staticmethod
    def _guard_against_stalling(history: list[dict[str, Any]]) -> None:
        streak = 0
        for event in reversed(history):
            if event["type"] == "step":
                break
            if event["type"] in {"step_failed", "denied"}:
                streak += 1
        if streak >= FAILURE_STREAK_LIMIT:
            raise TaskFailure("stuck", f"{streak} steps in a row could not make progress")

    async def _record(self, task_id: UUID, event_type: str, payload: dict[str, Any]) -> None:
        async with self.pool.connection() as conn:
            await record_event(conn, task_id, event_type, payload)

    async def _bill(self, task: dict[str, Any], usage: Usage, route: str) -> int:
        """Writes the llm_calls row and returns the cost rounded up to whole cents."""
        async with self.pool.connection() as conn:
            await conn.execute(
                "insert into public.llm_calls (user_id, task_id, route, model, input_tokens, "
                "cached_tokens, output_tokens, latency_ms, cost_cents) "
                "values (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    task["user_id"], task["id"], route, usage.model, usage.input_tokens,
                    usage.cached_tokens, usage.output_tokens, usage.latency_ms,
                    round(usage.cost_cents, 4),
                ),
            )  # fmt: skip
        return math.ceil(usage.cost_cents)
