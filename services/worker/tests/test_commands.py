import pytest

from worker.commands import Command, parse


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("status", Command("status", None)),
        ("STATUS t7", Command("status", "T7")),
        ("  Pause T12. ", Command("pause", "T12")),
        ("halt t3", Command("pause", "T3")),
        ("resume", Command("resume", None)),
        ("kill T7!", Command("kill", "T7")),
        ("Halt all", Command("halt_all", None)),
    ],
)
def test_recognised_commands(text: str, expected: Command) -> None:
    assert parse(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "cancel T7",  # carrier opt-out keyword: must never be treated as a task command
        "stop",
        "pause all",  # only HALT ALL addresses every task
        "kill all",
        "please pause T7",
        "pause T7 and T8",
        "status T",
        "",
    ],
)
def test_everything_else_is_not_a_command(text: str) -> None:
    assert parse(text) is None
