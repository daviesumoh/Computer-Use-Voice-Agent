from datetime import UTC, datetime
from uuid import uuid4

from agent_common.contracts import CallStartResponse, ResumeContext, TaskSummary

from voice.prompts import greeting, instructions


def caller(**overrides: object) -> CallStartResponse:
    base: dict[str, object] = {
        "conversation_id": uuid4(),
        "display_name": "Dave Umoh",
        "timezone": "America/Toronto",
        "active_tasks": [],
        "resume": None,
    }
    return CallStartResponse.model_validate(base | overrides)


def test_plain_greeting_uses_first_name() -> None:
    assert greeting(caller()) == "Hi Dave. What can I do for you?"


def test_greeting_mentions_running_tasks() -> None:
    tasks = [TaskSummary(short_code="T1", title="Flights", status="running")]
    assert greeting(caller(active_tasks=tasks)) == "Hi Dave. 1 task running. What can I do?"


def test_dropped_call_greeting_offers_to_resume_and_prompt_carries_state() -> None:
    resume = ResumeContext(
        conversation_id=uuid4(), ended_at=datetime.now(UTC), state={"open_question": "Which dates?"}
    )
    c = caller(resume=resume)
    assert greeting(c) == "Hi Dave. We got cut off. Want to pick up where we left off?"
    assert "Which dates?" in instructions(c)


def test_instructions_list_running_tasks_and_forbid_spoken_approvals() -> None:
    tasks = [TaskSummary(short_code="T2", title="Compare laptops", status="queued")]
    text = instructions(caller(active_tasks=tasks))
    assert "T2 Compare laptops (queued)" in text
    assert "can't approve purchases" in text


def test_standing_rules_are_given_as_data_and_the_card_questions_are_asked_once() -> None:
    card = {
        "lines": ["Always buy electronics from Best Buy."],
        "changes": ["was: Use Best Buy / now: Use Costco"],
        "to_confirm": [{"id": str(uuid4()), "text": "Prefers Costco for tires."}],
    }
    text = instructions(caller(memory=card))

    assert "<user_memory>\n- Always buy electronics from Best Buy.\n</user_memory>" in text
    assert "was: Use Best Buy / now: Use Costco" in text
    assert "Prefers Costco for tires." in text and "settle_memory" in text


def test_without_memories_the_prompt_has_no_memory_block() -> None:
    assert "<user_memory>" not in instructions(caller())
