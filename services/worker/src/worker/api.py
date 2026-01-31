"""Internal HTTP API. Callers: the voice agent (LiveKit Cloud) and the web app (Vercel)."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, TypeVar
from uuid import UUID

import httpx
import psycopg
from agent_common import signing
from agent_common.contracts import (
    CallEndRequest,
    CallStartRequest,
    CallStartResponse,
    ClearMemoriesRequest,
    ConnectAccountRequest,
    ConnectAccountResponse,
    CreateTaskRequest,
    CreateTaskResponse,
    DecideApprovalRequest,
    DeleteMemoryRequest,
    EditMemoryRequest,
    ForgetRequest,
    ForgetResponse,
    InboundSmsRequest,
    MemoryEnabledRequest,
    PushSubscribeRequest,
    PushUnsubscribeRequest,
    RecalledItem,
    RecallRequest,
    RecallResponse,
    SettleMemoryRequest,
    TaskEditRequest,
    TurnRequest,
)
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from worker import approvals, broker, calls, edits, health, push, router, sms, takeover, tasks
from worker.auth import Authenticator, AuthError, Caller, SupabaseAuthenticator
from worker.browser.agent import BrowserExecutor
from worker.browser.decider import OpenAIDecider
from worker.db import Conn, Pool, create_pool
from worker.engine import Dispatcher, Engine, Runtime
from worker.logs import configure_logging
from worker.memory import manage, recall
from worker.memory.embeddings import OpenAIEmbedder
from worker.memory.jobs import Memory
from worker.memory.models import OpenAIMemoryModel
from worker.secret_store import BitwardenSecretStore, SecretStore
from worker.settings import Settings, load_settings

log = logging.getLogger(__name__)
Model = TypeVar("Model", bound=BaseModel)


def create_app(
    settings: Settings,
    pool: Pool,
    http: httpx.AsyncClient,
    engine: Dispatcher,
    authenticate: Authenticator,
    secrets: SecretStore | None = None,
    browsers: takeover.Browsers | None = None,
    memory: Memory | None = None,
) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)  # internal: no public schema
    secret = settings.service_hmac_secret.get_secret_value()
    if settings.dashboard_url:
        # The dashboard's browser calls the user-facing endpoints below directly.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[settings.dashboard_url],
            allow_methods=["POST"],
            allow_headers=["authorization", "content-type"],
        )

    async def signed(request: Request, model: type[Model]) -> Model:
        body = await request.body()
        try:
            signing.verify(
                secret,
                timestamp_header=request.headers.get(signing.TIMESTAMP_HEADER),
                signature_header=request.headers.get(signing.SIGNATURE_HEADER),
                method=request.method,
                path=request.url.path,
                body=body,
            )
        except signing.InvalidSignatureError as exc:
            log.warning(
                "rejected unsigned request", extra={"path": request.url.path, "reason": str(exc)}
            )
            raise HTTPException(status_code=401, detail="invalid signature") from None
        try:
            return model.model_validate_json(body)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors(include_input=False)) from None

    async def authenticated(request: Request, *, fresh_mfa: bool) -> Caller:
        """The signed-in owner, who must have passed MFA (and, if asked, recently)."""
        try:
            caller = await authenticate(request.headers.get("authorization"))
        except AuthError:
            raise HTTPException(status_code=401, detail="sign in required") from None
        if caller.aal != "aal2":
            raise HTTPException(status_code=403, detail="mfa_required")
        if fresh_mfa and not caller.mfa_is_fresh():
            raise HTTPException(status_code=403, detail="mfa_stale")
        return caller

    # As dependencies, these run before the request body is parsed, so a caller who is not
    # signed in learns nothing about the expected shape of a request.
    async def owner(request: Request) -> Caller:
        return await authenticated(request, fresh_mfa=False)

    async def owner_with_fresh_mfa(request: Request) -> Caller:
        return await authenticated(request, fresh_mfa=True)

    Owner = Annotated[Caller, Depends(owner)]
    FreshOwner = Annotated[Caller, Depends(owner_with_fresh_mfa)]

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        async with pool.connection() as conn:
            await conn.execute("select 1")
        return {"status": "ok"}

    @app.post("/v1/calls/start", response_model=CallStartResponse)
    async def start_call(request: Request) -> CallStartResponse:
        payload = await signed(request, CallStartRequest)
        async with pool.connection() as conn:
            started = await calls.start_call(
                conn, call_sid=payload.call_sid, caller_number=payload.caller_number
            )
        if started is None:
            raise HTTPException(status_code=403, detail="unknown caller")
        return started

    @app.post("/v1/calls/{conversation_id}/turns", status_code=204)
    async def record_turn(conversation_id: UUID, request: Request) -> Response:
        payload = await signed(request, TurnRequest)
        async with pool.connection() as conn:
            found = await calls.record_turn(
                conn, conversation_id=conversation_id, **payload.model_dump()
            )
        if not found:
            raise HTTPException(status_code=404, detail="unknown conversation")
        return Response(status_code=204)

    @app.post("/v1/calls/{conversation_id}/end", status_code=204)
    async def end_call(conversation_id: UUID, request: Request) -> Response:
        payload = await signed(request, CallEndRequest)
        async with pool.connection() as conn:
            found = await calls.end_call(
                conn, conversation_id=conversation_id, reason=payload.reason
            )
        if not found:
            raise HTTPException(status_code=404, detail="unknown conversation")
        await engine.remember_conversation(conversation_id)
        return Response(status_code=204)

    async def caller_of(conn: Conn, conversation_id: UUID) -> UUID:
        user_id = await calls.owner_of(conn, conversation_id)
        if user_id is None:
            raise HTTPException(status_code=404, detail="unknown conversation")
        return user_id

    @app.post("/v1/calls/{conversation_id}/memory/recall", response_model=RecallResponse)
    async def recall_memory(conversation_id: UUID, request: Request) -> RecallResponse:
        payload = await signed(request, RecallRequest)
        async with pool.connection() as conn:
            user_id = await caller_of(conn, conversation_id)
            if memory is None:
                return RecallResponse(items=[])
            found = await recall.recall(conn, memory.embedder, user_id, payload.query)
        return RecallResponse(
            items=[RecalledItem(id=r.id, kind=r.kind, text=r.text) for r in found]
        )

    @app.post("/v1/calls/{conversation_id}/memory/forget", response_model=ForgetResponse)
    async def forget_memory(conversation_id: UUID, request: Request) -> ForgetResponse:
        payload = await signed(request, ForgetRequest)
        async with pool.connection() as conn:
            user_id = await caller_of(conn, conversation_id)
            count = await manage.forget(conn, user_id, payload.memory_ids, actor="voice")
        return ForgetResponse(forgotten=count)

    @app.post("/v1/calls/{conversation_id}/memory/settle", status_code=204)
    async def settle_memory(conversation_id: UUID, request: Request) -> Response:
        payload = await signed(request, SettleMemoryRequest)
        async with pool.connection() as conn:
            user_id = await caller_of(conn, conversation_id)
            await manage.settle(conn, user_id, payload.memory_id, keep=payload.keep, actor="voice")
        return Response(status_code=204)

    @app.post("/v1/calls/{conversation_id}/tasks/{short_code}/edit", status_code=204)
    async def edit_task(conversation_id: UUID, short_code: str, request: Request) -> Response:
        payload = await signed(request, TaskEditRequest)
        async with pool.connection() as conn:
            user_id = await caller_of(conn, conversation_id)
            task = await (
                await conn.execute(
                    "select id, status, waiting_reason from public.tasks "
                    "where user_id = %s and short_code = %s",
                    (user_id, short_code.upper()),
                )
            ).fetchone()
            if task is None:
                raise HTTPException(status_code=404, detail="unknown task")
            if task["status"] not in edits.ACTIVE:
                raise HTTPException(status_code=409, detail=f"task is {task['status']}")
            await edits.submit(conn, user_id=user_id, task=task, text=payload.text, channel="voice")
        await engine.wake(task["id"])
        return Response(status_code=204)

    @app.post("/v1/tasks", response_model=CreateTaskResponse)
    async def create_task(
        request: Request, response: Response, background: BackgroundTasks
    ) -> CreateTaskResponse:
        payload = await signed(request, CreateTaskRequest)
        async with pool.connection() as conn:
            try:
                created = await tasks.create_task(conn, **payload.model_dump())
            except tasks.UnknownConversationError:
                raise HTTPException(status_code=404, detail="unknown conversation") from None
            except tasks.UnknownSiteError as unknown:
                raise HTTPException(
                    status_code=422, detail=f"no connected account for {unknown}"
                ) from None
            owner = await (
                await conn.execute(
                    "select user_id from public.tasks where id = %s", (created.task_id,)
                )
            ).fetchone()
        assert owner is not None
        await engine.enqueue(created.task_id)
        response.status_code = 201 if created.created else 200
        # After the response: the caller (often a live phone call) must not wait on Twilio.
        background.add_task(
            _notify,
            owner["user_id"],
            f"task:{created.task_id}:created",
            f"{created.short_code} started: {payload.title}. I'll text updates here.",
        )
        return created

    @app.post("/v1/approvals/{approval_id}/decide", status_code=204)
    async def decide_approval(
        approval_id: UUID, caller: Owner, body: DecideApprovalRequest
    ) -> Response:
        # Approving re-checks MFA freshness inside `decide`; denying needs no fresh code.
        async with pool.connection() as conn:
            try:
                await approvals.decide(
                    conn,
                    approval_id,
                    user_id=caller.user_id,
                    decision=body.decision,
                    mfa_verified_at=caller.mfa_verified_at,
                )
            except approvals.UnknownApprovalError:
                raise HTTPException(status_code=404, detail="unknown approval") from None
            except approvals.NotPendingError as late:
                raise HTTPException(status_code=409, detail=f"approval is {late.status}") from None
            except approvals.StaleMfaError:
                raise HTTPException(status_code=403, detail="mfa_stale") from None
        return Response(status_code=204)

    @app.post("/v1/emergency-stop/clear", status_code=204)
    async def clear_emergency_stop(caller: FreshOwner) -> Response:
        async with pool.connection() as conn, conn.transaction():
            await conn.execute(
                "update public.user_settings set emergency_stop = false, updated_at = now() "
                "where user_id = %s",
                (caller.user_id,),
            )
            await conn.execute(
                "insert into audit.events (user_id, actor, action) "
                "values (%s, 'user', 'emergency_stop_cleared')",
                (caller.user_id,),
            )
        return Response(status_code=204)

    @app.post("/v1/accounts", status_code=201)
    async def connect_account(
        caller: FreshOwner, body: ConnectAccountRequest
    ) -> ConnectAccountResponse:
        if secrets is None:
            raise HTTPException(status_code=503, detail="no secret store is configured")
        async with pool.connection() as conn:
            try:
                account_id = await broker.connect_account(
                    conn, secrets, user_id=caller.user_id, **body.model_dump()
                )
            except broker.InvalidAccountError as invalid:
                raise HTTPException(status_code=422, detail=str(invalid)) from None
        return ConnectAccountResponse(account_id=account_id)

    @app.post("/v1/accounts/{account_id}/disconnect", status_code=204)
    async def disconnect_account(account_id: UUID, caller: FreshOwner) -> Response:
        if secrets is None:
            raise HTTPException(status_code=503, detail="no secret store is configured")
        async with pool.connection() as conn:
            try:
                await broker.disconnect_account(
                    conn, secrets, user_id=caller.user_id, account_id=account_id
                )
            except broker.UnknownAccountError:
                raise HTTPException(status_code=404, detail="unknown account") from None
        return Response(status_code=204)

    @app.post("/v1/memories/{memory_id}/edit")
    async def edit_memory(
        memory_id: UUID, body: EditMemoryRequest, caller: Owner
    ) -> dict[str, str]:
        if memory is None:
            raise HTTPException(status_code=503, detail="memory is not configured")
        async with pool.connection() as conn:
            try:
                new_id = await manage.edit(
                    conn, memory.embedder, caller.user_id, memory_id, body.text
                )
            except manage.UnknownMemoryError:
                raise HTTPException(status_code=404, detail="unknown memory") from None
            except manage.SensitiveMemoryError as sensitive:
                raise HTTPException(status_code=422, detail=str(sensitive)) from None
        return {"id": str(new_id)}

    @app.post("/v1/memories/{memory_id}/delete", status_code=204)
    async def delete_memory(memory_id: UUID, body: DeleteMemoryRequest, caller: Owner) -> Response:
        async with pool.connection() as conn:
            erased = await manage.forget(
                conn,
                caller.user_id,
                [memory_id],
                actor="dashboard",
                immediately=body.immediately,
                with_sources=body.with_sources,
            )
        if not erased:
            raise HTTPException(status_code=404, detail="unknown memory")
        return Response(status_code=204)

    @app.post("/v1/memories/{memory_id}/restore", status_code=204)
    async def restore_memory(memory_id: UUID, caller: Owner) -> Response:
        async with pool.connection() as conn:
            if not await manage.restore(conn, caller.user_id, memory_id):
                raise HTTPException(status_code=404, detail="nothing to restore")
        return Response(status_code=204)

    @app.post("/v1/memories/{memory_id}/settle", status_code=204)
    async def settle_dashboard_memory(
        memory_id: UUID, body: SettleMemoryRequest, caller: Owner
    ) -> Response:
        async with pool.connection() as conn:
            if not await manage.settle(
                conn, caller.user_id, memory_id, keep=body.keep, actor="dashboard"
            ):
                raise HTTPException(status_code=404, detail="no such pending memory")
        return Response(status_code=204)

    @app.post("/v1/memories/clear")
    async def clear_memories(body: ClearMemoriesRequest, caller: FreshOwner) -> dict[str, int]:
        async with pool.connection() as conn:
            return {"cleared": await manage.clear(conn, caller.user_id, kind=body.type)}

    @app.post("/v1/memory/enabled", status_code=204)
    async def set_memory_enabled(body: MemoryEnabledRequest, caller: Owner) -> Response:
        async with pool.connection() as conn:
            await manage.set_enabled(conn, caller.user_id, body.enabled)
        return Response(status_code=204)

    @app.post("/v1/push/subscribe", status_code=204)
    async def push_subscribe(body: PushSubscribeRequest, caller: Owner) -> Response:
        async with pool.connection() as conn:
            await push.subscribe(conn, caller.user_id, body)
        return Response(status_code=204)

    @app.post("/v1/push/unsubscribe", status_code=204)
    async def push_unsubscribe(body: PushUnsubscribeRequest, caller: Owner) -> Response:
        async with pool.connection() as conn:
            await push.unsubscribe(conn, caller.user_id, body.endpoint)
        return Response(status_code=204)

    @app.post("/v1/tasks/{task_id}/takeover")
    async def start_takeover(task_id: UUID, caller: Owner) -> dict[str, object]:
        async with pool.connection() as conn:
            try:
                token = await takeover.issue_token(conn, user_id=caller.user_id, task_id=task_id)
            except takeover.NotAvailableError:
                raise HTTPException(status_code=409, detail="task is not waiting for you") from None
        return {"token": token, "expires_in": int(takeover.TOKEN_TTL.total_seconds())}

    active_takeovers: set[UUID] = set()

    @app.websocket("/v1/takeover/{token}")
    async def takeover_socket(websocket: WebSocket, token: str) -> None:
        # A browser always sends Origin on a WebSocket; only the dashboard's is accepted.
        if settings.dashboard_url and websocket.headers.get("origin") != settings.dashboard_url:
            await websocket.close(code=4403)
            return
        async with pool.connection() as conn:
            redeemed = await takeover.redeem_token(conn, token)
        if redeemed is None:
            await websocket.close(code=4401)
            return
        user_id, task_id = redeemed
        session = browsers.session_for(task_id) if browsers else None
        if session is None or task_id in active_takeovers:
            await websocket.close(code=4409)
            return
        active_takeovers.add(task_id)
        try:
            await websocket.accept()
            async with pool.connection() as conn:
                await conn.execute(
                    "insert into audit.events (user_id, actor, action, target_type, target_id) "
                    "values (%s, 'user', 'takeover_started', 'task', %s)",
                    (user_id, str(task_id)),
                )
            finished = await takeover.run(websocket, session)
            if finished:
                assert browsers is not None
                await browsers.checkpoint(task_id)  # keep the login the owner just made
                async with pool.connection() as conn:
                    await takeover.finish(conn, task_id, user_id)
                await engine.wake(task_id)
                await websocket.close()
        finally:
            active_takeovers.discard(task_id)

    @app.post("/v1/sms/inbound")
    async def inbound_sms(request: Request, background: BackgroundTasks) -> dict[str, bool]:
        payload = await signed(request, InboundSmsRequest)
        async with pool.connection() as conn:
            message = await sms.record_inbound(
                conn,
                message_sid=payload.message_sid,
                from_number=payload.from_number,
                body=payload.body,
            )
            if message is None:
                return {"stored": False}
            # Routed on redelivery too: every effect below is idempotent, so a webhook retry
            # after a crash completes the work instead of dropping the command.
            routed = await router.route(
                conn,
                user_id=message.user_id,
                message_id=message.id,
                text=payload.body,
                dashboard_url=settings.dashboard_url,
            )
        for task_id in routed.wake:
            await engine.wake(task_id)
        if routed.reply:
            background.add_task(
                _notify, message.user_id, f"reply:{payload.message_sid}", routed.reply
            )
        return {"stored": message.is_new}

    _notify = sms.notifier(settings, pool, http)

    return app


PURGE_INTERVAL_SECONDS = 3600
REAP_INTERVAL_SECONDS = 300


async def _reap_browsers(executor: BrowserExecutor) -> None:
    """Closes browsers whose task has ended, in case a release was skipped."""
    while True:
        await asyncio.sleep(REAP_INTERVAL_SECONDS)
        try:
            await executor.reap()
        except psycopg.Error:
            log.exception("browser reaping failed; will retry")


async def _purge_deleted_memories(pool: Pool) -> None:
    """Erases memories once their undo window has passed. Idempotent, so a missed hour or a
    restart costs nothing."""
    while True:
        try:
            async with pool.connection() as conn:
                if erased := await manage.purge_expired(conn):
                    log.info("erased forgotten memories", extra={"count": erased})
        except psycopg.Error:
            log.exception("memory purge failed; will retry")
        await asyncio.sleep(PURGE_INTERVAL_SECONDS)


def build() -> FastAPI:
    """Production entry point: `uvicorn --factory worker.api:build`."""
    settings = load_settings()
    configure_logging(settings)
    pool = create_pool(settings)
    http = httpx.AsyncClient(timeout=10)

    secrets: SecretStore | None = None
    if settings.bitwarden_access_token is not None:
        assert settings.bitwarden_organization_id and settings.bitwarden_project_id  # validated
        secrets = BitwardenSecretStore(
            access_token=settings.bitwarden_access_token.get_secret_value(),
            organization_id=settings.bitwarden_organization_id,
            project_id=settings.bitwarden_project_id,
            state_file=settings.bitwarden_state_file,
        )

    browsers: takeover.Browsers | None = None
    memory: Memory | None = None
    reaper: BrowserExecutor | None = None
    notify = sms.notifier(settings, pool, http)
    if settings.vapid_private_key is not None:
        notify = push.notifier(pool, push.web_push_sender(settings), notify)
    runtime = Runtime(
        pool=pool,
        notify=notify,
        dashboard_url=settings.dashboard_url,
    )
    if settings.openai_api_key is not None:
        openai = AsyncOpenAI(api_key=settings.openai_api_key.get_secret_value())
        memory = Memory(
            OpenAIEmbedder(openai), OpenAIMemoryModel(openai, settings.memory_model, pool)
        )
        runtime.memory = memory
        browser = BrowserExecutor(
            pool,
            OpenAIDecider(openai, settings.browser_model),
            memory=memory,
            headless=settings.browser_headless,
            store=secrets,
            session_key=settings.session_key_bytes,
        )
        runtime.executor, runtime.release = browser, browser.release
        reaper = browser
        browsers = browser
    engine = Engine(runtime, settings.database_url.get_secret_value())

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await pool.open(wait=True)
        await engine.start()
        background = [
            asyncio.create_task(_purge_deleted_memories(pool)),
            asyncio.create_task(health.watch(pool, runtime.notify, settings.dashboard_url)),
        ]
        if reaper is not None:
            background.append(asyncio.create_task(_reap_browsers(reaper)))
        try:
            yield
        finally:
            for job in background:
                job.cancel()
            engine.stop()
            await pool.close()
            await http.aclose()

    authenticate = SupabaseAuthenticator(
        http, settings.supabase_url, settings.supabase_publishable_key
    )
    app = create_app(settings, pool, http, engine, authenticate, secrets, browsers, memory)
    app.router.lifespan_context = lifespan
    return app
