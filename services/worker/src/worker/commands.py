"""Parses texts from the owner into task commands.

Carriers treat STOP, CANCEL, END, QUIT and UNSUBSCRIBE as opt-outs, so the verbs here avoid
them: stopping a task is `kill`, and `halt` is a synonym for `pause`.
"""

import re
from dataclasses import dataclass
from typing import Literal

Verb = Literal["status", "pause", "resume", "kill", "halt_all"]

_VERBS: dict[str, Verb] = {
    "status": "status",
    "pause": "pause",
    "halt": "pause",
    "resume": "resume",
    "kill": "kill",
}
_PATTERN = re.compile(
    r"^\s*(?P<verb>[a-z]+)(?:\s+(?P<target>t\d+|all))?\s*[.!]?\s*$", re.IGNORECASE
)

OPT_OUT_WORDS = frozenset({"stop", "cancel", "end", "quit", "unsubscribe"})

HELP = (
    "Commands: STATUS, PAUSE T7, RESUME T7, KILL T7, HALT ALL. "
    "The task code is optional when only one task is active."
)


@dataclass(frozen=True)
class Command:
    verb: Verb
    short_code: str | None  # 'T7'; None means "the only active task"


def parse(text: str) -> Command | None:
    match = _PATTERN.match(text)
    if match is None:
        return None
    verb, target = match["verb"].lower(), (match["target"] or "").upper()
    if verb == "halt" and target == "ALL":
        return Command("halt_all", None)
    if verb not in _VERBS or target == "ALL":
        return None
    return Command(_VERBS[verb], target or None)
