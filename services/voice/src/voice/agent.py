"""LiveKit entry point: one AgentSession per inbound phone call.

Run locally:  uv run python -m voice.agent dev     (needs LIVEKIT_*, DEEPGRAM_API_KEY,
OPENAI_API_KEY and VOICE_* in the environment)
"""

import asyncio
import logging
from typing import Annotated, Literal
from uuid import UUID

import httpx
from agent_common.contracts import CallStartResponse, CreateTaskRequest, TurnRequest
from livekit import rtc
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    ChatMessage,
    CloseEvent,
    ConversationItemAddedEvent,
    JobContext,
    RunContext,
    cli,
    function_tool,
    room_io,
)
from livekit.plugins import deepgram, noise_cancellation, openai, silero
from pydantic import Field

from voice import prompts
from voice.conversation import ConversationState, SlotChange
from voice.settings import Settings, load_settings
from voice.worker_client import UnknownCallerError, UnknownTaskError, WorkerClient

log = logging.getLogger("voice")
server = AgentServer()


class PhoneAgent(Agent):
    def __init__(self, caller: CallStartResponse, worker: WorkerClient) -> None:
        super().__init__(instructions=prompts.instructions(caller))
        self._caller = caller
        self._worker = worker
        self.state = (
            ConversationState.model_validate(caller.resume.state)
            if caller.resume
            else (ConversationState())
        )

    @function_tool
    async def update_task_details(
        self, context: RunContext[None], changes: list[SlotChange]
    ) -> str:
        """Record details of the task the user is describing, or corrections to them."""
        try:
            to_confirm = self.state.apply(changes)
        except ValueError as exc:
            return f"Not recorded: {exc}"
        if to_confirm:
            return "Recorded. The line was unclear; read back and confirm: " + ", ".join(
                f"{name}={self.state.slots[name].value}" for name in to_confirm
            )
        return "Recorded."

    @function_tool
    async def start_task(
        self,
        context: RunContext[None],
        title: Annotated[str, Field(description="Short title, e.g. 'Compare laptop prices'")],
        summary: Annotated[str, Field(description="The one-sentence summary the user said yes to")],
        sites: Annotated[
            list[str] | None,
            Field(description="Websites (domains) with a saved login that the task may sign in to"),
        ] = None,
    ) -> str:
        """Start the task the user just agreed to. Only after they explicitly said yes."""
        self.state.agree(summary)
        if reason := self.state.why_not_ready():
            return f"Not started. {reason}"
        assert self.state.draft_request_id is not None
        created = await self._worker.create_task(
            CreateTaskRequest(
                request_id=self.state.draft_request_id,
                conversation_id=self._caller.conversation_id,
                title=title,
                spec={
                    "summary": summary,
                    "details": {name: slot.value for name, slot in self.state.slots.items()},
                },
                sites=sites or [],
            )
        )
        self.state.task_started(created.short_code)
        return f"Started as {created.short_code}. Tell the user they'll get texts about it."

    @function_tool
    async def edit_task(
        self,
        context: RunContext[None],
        task_code: Annotated[str, Field(description="The task's code, e.g. 'T7'")],
        instruction: Annotated[
            str, Field(description="What the user wants changed, in their words")
        ],
    ) -> str:
        """Pass a change or addition from the user to a task that is already running."""
        try:
            await self._worker.edit_task(self._caller.conversation_id, task_code, instruction)
        except UnknownTaskError as unknown:
            return f"Not passed on: {unknown}."
        return f"Passed to {task_code.upper()}; it applies the change at its next step."

    @function_tool
    async def recall_memory(
        self,
        context: RunContext[None],
        query: Annotated[str, Field(description="What to look up, e.g. 'where I buy electronics'")],
    ) -> str:
        """Look up what is remembered about the user: a preference, a fact, an earlier request."""
        items = await self._worker.recall(self._caller.conversation_id, query)
        if not items:
            return "Nothing is remembered about that."
        return "\n".join(f"{item.id} ({item.kind}) {item.text}" for item in items)

    @function_tool
    async def forget_memory(
        self,
        context: RunContext[None],
        memory_ids: Annotated[list[str], Field(description="Ids from recall_memory to forget")],
    ) -> str:
        """Forget remembered items. If several could match, read them back and get a yes first."""
        try:
            ids = [UUID(i) for i in memory_ids]
        except ValueError:
            return "Not forgotten: those are not memory ids. Use recall_memory first."
        forgotten = await self._worker.forget(self._caller.conversation_id, ids)
        return f"Forgotten: {forgotten}."

    @function_tool
    async def settle_memory(
        self,
        context: RunContext[None],
        memory_id: Annotated[str, Field(description="The id given with the question")],
        keep: Annotated[bool, Field(description="True if the user wants it remembered")],
    ) -> str:
        """Record the user's answer to a question about a habit you noticed."""
        try:
            parsed = UUID(memory_id)
        except ValueError:
            return "Not recorded: that is not a memory id."
        await self._worker.settle_memory(self._caller.conversation_id, parsed, keep=keep)
        return "Recorded."


def _call_identity(participant: rtc.RemoteParticipant) -> tuple[str, str]:
    attributes = participant.attributes
    call_sid = attributes.get("sip.twilio.callSid") or attributes.get("sip.callID", "")
    return call_sid, attributes.get("sip.phoneNumber", "")


@server.rtc_session()
async def entrypoint(ctx: JobContext) -> None:
    settings: Settings = load_settings()
    http = httpx.AsyncClient(base_url=settings.worker_url, timeout=5)
    worker = WorkerClient(http, settings.service_hmac_secret.get_secret_value())

    await ctx.connect()
    caller_participant = await ctx.wait_for_participant(
        kind=rtc.ParticipantKind.PARTICIPANT_KIND_SIP
    )
    call_sid, number = _call_identity(caller_participant)

    try:
        caller = await worker.start_call(call_sid, number)
    except UnknownCallerError:
        log.info("refusing call from unknown number")
        ctx.shutdown("unknown caller")  # room closes; nothing is said to an unverified caller
        await http.aclose()
        return

    agent = PhoneAgent(caller, worker)
    session: AgentSession[None] = AgentSession(
        stt=deepgram.STTv2(
            model="flux-general-en",
            # Deepgram's "complex pipeline" preset: patient end-of-turn, early LLM start.
            eot_threshold=0.85,
            eager_eot_threshold=0.4,
            eot_timeout_ms=7000,
        ),
        llm=openai.LLM(model=settings.llm_model),
        tts=deepgram.TTS(model=settings.tts_voice),
        vad=silero.VAD.load(),  # still needed for barge-in when Flux decides turns
        turn_handling={"turn_detection": "stt"},
    )

    pending: set[asyncio.Task[None]] = set()

    def persist(message: ChatMessage) -> None:
        if message.role == "user":
            agent.state.last_user_confidence = message.transcript_confidence
        speaker: Literal["user", "agent"] = "user" if message.role == "user" else "agent"
        turn = TurnRequest(
            turn_index=agent.state.turn_index,
            speaker=speaker,
            # On interruption LiveKit keeps only the words actually played.
            text=message.text_content or "",
            asr_confidence=message.transcript_confidence if speaker == "user" else None,
            state=agent.state.model_dump(mode="json"),
        )
        agent.state.turn_index += 1
        task = asyncio.create_task(_record(worker, caller.conversation_id, turn))
        pending.add(task)
        task.add_done_callback(pending.discard)

    @session.on("conversation_item_added")
    def _on_item(event: ConversationItemAddedEvent) -> None:
        if isinstance(event.item, ChatMessage) and event.item.role in ("user", "assistant"):
            persist(event.item)

    @session.on("close")
    def _on_close(event: CloseEvent) -> None:
        async def finish() -> None:
            # Shutdown callbacks run concurrently, so the client is closed here, after the
            # last request, rather than in a callback of its own.
            try:
                await asyncio.gather(*pending, return_exceptions=True)
                await worker.end_call(caller.conversation_id, event.reason.value)
            finally:
                await http.aclose()

        ctx.add_shutdown_callback(finish)

    await session.start(
        agent=agent,
        room=ctx.room,
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(
                noise_cancellation=noise_cancellation.BVCTelephony()
            ),
        ),
    )
    await session.say(prompts.greeting(caller))


async def _record(worker: WorkerClient, conversation_id: UUID, turn: TurnRequest) -> None:
    try:
        await worker.record_turn(conversation_id, turn)
    except httpx.HTTPError as exc:
        # The call continues even if a single turn fails to persist.
        log.warning("failed to persist turn", extra={"turn": turn.turn_index, "error": str(exc)})


if __name__ == "__main__":
    cli.run_app(server)
