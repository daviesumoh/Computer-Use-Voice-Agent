"""Signed calls to the worker API. The voice agent holds no database credentials."""

from typing import TypeVar
from uuid import UUID

import httpx
from agent_common.contracts import (
    CallEndRequest,
    CallStartRequest,
    CallStartResponse,
    CreateTaskRequest,
    CreateTaskResponse,
    ForgetRequest,
    ForgetResponse,
    RecalledItem,
    RecallRequest,
    RecallResponse,
    SettleMemoryRequest,
    TaskEditRequest,
    TurnRequest,
)
from agent_common.signing import signed_headers
from pydantic import BaseModel

Response = TypeVar("Response", bound=BaseModel)


class UnknownCallerError(Exception):
    pass


class UnknownTaskError(Exception):
    """The task does not exist, or has already finished."""


class WorkerClient:
    def __init__(self, http: httpx.AsyncClient, secret: str) -> None:
        self._http = http  # base_url = worker
        self._secret = secret

    async def _post(self, path: str, payload: BaseModel) -> httpx.Response:
        body = payload.model_dump_json().encode()
        headers = signed_headers(self._secret, "POST", path, body)
        headers["content-type"] = "application/json"
        return await self._http.post(path, content=body, headers=headers)

    async def _post_for(self, path: str, payload: BaseModel, model: type[Response]) -> Response:
        response = await self._post(path, payload)
        response.raise_for_status()
        return model.model_validate_json(response.content)

    async def start_call(self, call_sid: str, caller_number: str) -> CallStartResponse:
        response = await self._post(
            "/v1/calls/start", CallStartRequest(call_sid=call_sid, caller_number=caller_number)
        )
        if response.status_code == 403:
            raise UnknownCallerError(caller_number[-4:])
        response.raise_for_status()
        return CallStartResponse.model_validate_json(response.content)

    async def record_turn(self, conversation_id: UUID, turn: TurnRequest) -> None:
        (await self._post(f"/v1/calls/{conversation_id}/turns", turn)).raise_for_status()

    async def end_call(self, conversation_id: UUID, reason: str) -> None:
        response = await self._post(
            f"/v1/calls/{conversation_id}/end", CallEndRequest(reason=reason)
        )
        response.raise_for_status()

    async def create_task(self, request: CreateTaskRequest) -> CreateTaskResponse:
        return await self._post_for("/v1/tasks", request, CreateTaskResponse)

    async def recall(self, conversation_id: UUID, query: str) -> list[RecalledItem]:
        found = await self._post_for(
            f"/v1/calls/{conversation_id}/memory/recall", RecallRequest(query=query), RecallResponse
        )
        return found.items

    async def forget(self, conversation_id: UUID, memory_ids: list[UUID]) -> int:
        result = await self._post_for(
            f"/v1/calls/{conversation_id}/memory/forget",
            ForgetRequest(memory_ids=memory_ids),
            ForgetResponse,
        )
        return result.forgotten

    async def settle_memory(self, conversation_id: UUID, memory_id: UUID, *, keep: bool) -> None:
        response = await self._post(
            f"/v1/calls/{conversation_id}/memory/settle",
            SettleMemoryRequest(memory_id=memory_id, keep=keep),
        )
        response.raise_for_status()

    async def edit_task(self, conversation_id: UUID, short_code: str, text: str) -> None:
        response = await self._post(
            f"/v1/calls/{conversation_id}/tasks/{short_code}/edit", TaskEditRequest(text=text)
        )
        if response.status_code in (404, 409):
            raise UnknownTaskError(response.json().get("detail", short_code))
        response.raise_for_status()
