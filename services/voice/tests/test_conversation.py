import pytest

from voice.conversation import ConversationState, SlotChange


def heard(state: ConversationState, confidence: float) -> ConversationState:
    state.turn_index += 1
    state.last_user_confidence = confidence
    return state


def test_clear_speech_needs_no_read_back() -> None:
    state = heard(ConversationState(), 0.97)
    assert (
        state.apply([SlotChange(name="sites", action="set", value="Best Buy, Canada Computers")])
        == []
    )


def test_low_confidence_detail_must_be_confirmed_before_start() -> None:
    state = heard(ConversationState(), 0.62)
    assert state.apply([SlotChange(name="email", action="set", value="dave@exmple.com")]) == [
        "email"
    ]
    state.agree("Send the price list to dave@exmple.com")
    assert state.why_not_ready() == "Read these back and get a yes first: email"

    heard(state, 0.95)
    state.apply([SlotChange(name="email", action="confirm")])
    state.agree("Send the price list to dave@exmple.com")
    assert state.why_not_ready() is None


def test_correction_replaces_value_and_reopens_agreement() -> None:
    state = heard(ConversationState(), 0.95)
    state.apply([SlotChange(name="day", action="set", value="Tuesday")])
    state.agree("Book for Tuesday")

    heard(state, 0.95)
    state.apply([SlotChange(name="day", action="replace", value="Wednesday")])

    assert state.slots["day"].value == "Wednesday"
    assert (
        state.why_not_ready() == "Read back a one-sentence summary and get an explicit yes first."
    )


def test_retract_removes_detail() -> None:
    state = heard(ConversationState(), 0.95)
    state.apply([SlotChange(name="site", action="set", value="Amazon")])
    state.apply([SlotChange(name="site", action="retract")])
    assert state.slots == {}


def test_set_without_value_is_rejected() -> None:
    with pytest.raises(ValueError, match="needs a value"):
        heard(ConversationState(), 0.95).apply([SlotChange(name="budget", action="set")])


def test_request_id_is_stable_while_describing_and_resets_after_start() -> None:
    state = heard(ConversationState(), 0.95)
    state.apply([SlotChange(name="sites", action="set", value="Best Buy")])
    request_id = state.draft_request_id
    state.apply([SlotChange(name="budget", action="set", value="$1200")])
    assert state.draft_request_id == request_id is not None

    state.task_started("T4")
    assert state.started_tasks == ["T4"]
    assert state.draft_request_id is None
    assert state.why_not_ready() == "No task has been described yet."


def test_state_survives_json_round_trip_for_resume() -> None:
    state = heard(ConversationState(), 0.7)
    state.apply([SlotChange(name="sites", action="set", value="Best Buy")])
    state.open_question = "Which dates?"
    restored = ConversationState.model_validate(state.model_dump(mode="json"))
    assert restored == state
