"""What has been agreed on the call so far. Persisted after every turn so a dropped call
can resume, and consulted by tools so rules are enforced in code, not just in the prompt."""

from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

# Below this, a detail heard in the user's turn must be read back before acting on it.
# Deepgram reports word confidence in [0, 1].
CONFIRMATION_THRESHOLD = 0.85


class Slot(BaseModel):
    value: str
    turn: int
    needs_confirmation: bool


class SlotChange(BaseModel):
    """One edit the model makes to the task being described."""

    name: str = Field(description="Short snake_case name, e.g. 'sites', 'budget', 'deadline'")
    action: Literal["set", "replace", "retract", "confirm"] = Field(
        description="set: new detail; replace: user corrected it; retract: user dropped it; "
        "confirm: user agreed after you read it back"
    )
    value: str | None = Field(default=None, description="Required for set and replace")


class ConversationState(BaseModel):
    slots: dict[str, Slot] = Field(default_factory=dict)
    open_question: str | None = None
    agreed_summary: str | None = None
    # Fixed for the task currently being described, so a retried start can't create two.
    draft_request_id: UUID | None = None
    started_tasks: list[str] = Field(default_factory=list)
    turn_index: int = 0
    last_user_confidence: float | None = None

    def apply(self, changes: list[SlotChange]) -> list[str]:
        """Applies the model's edits. Returns slot names that must be read back to the user."""
        low_confidence = (
            self.last_user_confidence is not None
            and self.last_user_confidence < CONFIRMATION_THRESHOLD
        )
        for change in changes:
            if change.action in ("set", "replace"):
                if not change.value:
                    raise ValueError(f"{change.action} of '{change.name}' needs a value")
                self.slots[change.name] = Slot(
                    value=change.value, turn=self.turn_index, needs_confirmation=low_confidence
                )
            elif change.action == "retract":
                self.slots.pop(change.name, None)
            elif change.action == "confirm" and change.name in self.slots:
                self.slots[change.name].needs_confirmation = False
        if self.draft_request_id is None and self.slots:
            self.draft_request_id = uuid4()
        # Any edit after a read-back invalidates the agreement.
        self.agreed_summary = None
        return self.unconfirmed()

    def unconfirmed(self) -> list[str]:
        return [name for name, slot in self.slots.items() if slot.needs_confirmation]

    def agree(self, summary: str) -> None:
        self.agreed_summary = summary

    def why_not_ready(self) -> str | None:
        """None if the described task may start; otherwise what is missing, for the model."""
        if not self.slots:
            return "No task has been described yet."
        if unconfirmed := self.unconfirmed():
            return "Read these back and get a yes first: " + ", ".join(unconfirmed)
        if self.agreed_summary is None:
            return "Read back a one-sentence summary and get an explicit yes first."
        return None

    def task_started(self, short_code: str) -> None:
        self.started_tasks.append(short_code)
        self.slots, self.agreed_summary, self.draft_request_id = {}, None, None
