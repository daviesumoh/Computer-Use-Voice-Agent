"""What may become a memory. Code decides this, not the model: the model proposes, these
rules dispose."""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from worker.memory.models import Candidate, Utterance
from worker.transcripts import CARD_NUMBER

_SIN = re.compile(r"(?<!\d)\d{3}[ -]?\d{3}[ -]?\d{3}(?!\d)")
_LONG_TOKEN = re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")
_SECRET_WORDS = re.compile(
    r"\b(password|passcode|pin|cvv|cvc|security code|secret key|api key|access token|otp|"
    r"one[- ]time code|verification code)\b",
    re.I,
)
_ID_WORDS = re.compile(
    r"\b(social insurance|sin number|ssn|social security|passport|driver'?s licen[cs]e|"
    r"health card|ohip|account number|routing number|transit number|iban)\b",
    re.I,
)
# A blunt list on purpose: dropping a harmless health-adjacent preference costs little,
# keeping a diagnosis in a searchable table costs a lot.
_HEALTH = re.compile(
    r"\b(diagnos\w*|prescri\w*|medicat\w*|medical|illness|disease|cancer|diabet\w*|therap\w*|"
    r"surgery|allerg\w*|pregnan\w*|hiv|depress\w*|anxiety|symptom\w*|disabilit\w*)\b",
    re.I,
)
_SENSITIVE: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("card_number", CARD_NUMBER),
    ("id_number", _SIN),
    ("secret", _SECRET_WORDS),
    ("long_token", _LONG_TOKEN),
    ("identity_document", _ID_WORDS),
    ("health", _HEALTH),
)


@dataclass(frozen=True)
class Rejection:
    reason: str


def sensitive_kind(text: str) -> str | None:
    """Which kind of sensitive data the text holds, if any."""
    return next((name for name, pattern in _SENSITIVE if pattern.search(text)), None)


def _normal(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


def screen(candidate: Candidate, said: Sequence[Utterance]) -> Utterance | Rejection:
    """The owner's utterance a candidate rests on, or why it must not be stored."""
    if candidate.cue == "one_off":
        return Rejection("one_off")
    if kind := sensitive_kind(f"{candidate.text} {candidate.span}"):
        return Rejection(kind)
    span = _normal(candidate.span)
    if not span:
        return Rejection("no_span")
    for utterance in said:
        if span in _normal(utterance.text):
            return utterance
    # Not found in anything the owner said: the model made it up or took it from elsewhere.
    return Rejection("span_not_from_owner")
