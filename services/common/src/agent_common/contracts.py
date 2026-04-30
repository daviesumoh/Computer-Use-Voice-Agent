"""Request and response bodies of the worker's internal API (callers: voice agent, web app)."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

E164 = Field(pattern=r"^\+[1-9][0-9]{6,14}$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskSummary(_Strict):
    short_code: str
    title: str
    status: str


class CallStartRequest(_Strict):
    call_sid: str = Field(min_length=1, max_length=128)
    caller_number: str = E164


class ResumeContext(_Strict):
    """The previous call, if it ended recently, so the agent can pick up where it left off."""

    conversation_id: UUID
    ended_at: datetime
    state: dict[str, Any]


class MemoryQuestion(_Strict):
    """A habit worth asking the owner about, once: 'You often pick X. Remember that?'"""

    id: UUID
    text: str


class MemoryCard(_Strict):
    """What memory tells the voice agent at the start of a call."""

    lines: list[str] = []  # standing rules and preferences
    to_confirm: list[MemoryQuestion] = []
    changes: list[str] = []  # statements that replaced an older one, to mention once


class CallStartResponse(_Strict):
    conversation_id: UUID
    display_name: str
    timezone: str
    active_tasks: list[TaskSummary]
    resume: ResumeContext | None
    connected_sites: list[str] = []  # domains the caller has saved a login for
    memory: MemoryCard = MemoryCard()


class TurnRequest(_Strict):
    turn_index: int = Field(ge=0)
    speaker: Literal["user", "agent"]
    text: str = Field(max_length=10_000)
    asr_confidence: float | None = Field(default=None, ge=0, le=1)
    state: dict[str, Any]


class CallEndRequest(_Strict):
    reason: str = Field(max_length=64)


class CreateTaskRequest(_Strict):
    request_id: UUID
    conversation_id: UUID
    title: str = Field(min_length=1, max_length=200)
    spec: dict[str, Any]
    # Domains of connected accounts the task may log in to; fixed when the caller confirms it.
    sites: list[str] = []


class CreateTaskResponse(_Strict):
    task_id: UUID
    short_code: str
    created: bool  # False when request_id matched an existing task (a retried request)


class InboundSmsRequest(_Strict):
    message_sid: str = Field(min_length=1, max_length=64)
    from_number: str = E164
    body: str = Field(max_length=1600)


class DecideApprovalRequest(_Strict):
    decision: Literal["approved", "denied"]


class ConnectAccountRequest(_Strict):
    site_domain: str = Field(min_length=4, max_length=253)
    label: str = Field(min_length=1, max_length=60)
    username: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=512)
    allowed_origins: list[str] | None = None  # default: https://<domain> and https://www.<domain>


class ConnectAccountResponse(_Strict):
    account_id: UUID


class RecallRequest(_Strict):
    query: str = Field(min_length=1, max_length=500)


class RecalledItem(_Strict):
    id: UUID
    kind: str  # fact, preference, rule, or an episode: call, task
    text: str


class RecallResponse(_Strict):
    items: list[RecalledItem]


class ForgetRequest(_Strict):
    memory_ids: list[UUID] = Field(min_length=1, max_length=20)


class ForgetResponse(_Strict):
    forgotten: int


class SettleMemoryRequest(_Strict):
    memory_id: UUID
    keep: bool


class EditMemoryRequest(_Strict):
    text: str = Field(min_length=3, max_length=500)


class DeleteMemoryRequest(_Strict):
    immediately: bool = False
    with_sources: bool = False  # also delete the messages it was learned from


class ClearMemoriesRequest(_Strict):
    type: Literal["fact", "preference", "rule"] | None = None


class MemoryEnabledRequest(_Strict):
    enabled: bool


class PushSubscribeRequest(_Strict):
    """A browser's push subscription, as `PushSubscription.toJSON()` gives it."""

    endpoint: str = Field(pattern=r"^https://", max_length=2048)
    p256dh: str = Field(min_length=1, max_length=256)
    auth: str = Field(min_length=1, max_length=64)


class PushUnsubscribeRequest(_Strict):
    endpoint: str = Field(pattern=r"^https://", max_length=2048)


class TaskEditRequest(_Strict):
    """Something the owner says to a running task during a call."""

    text: str = Field(min_length=1, max_length=1000)
