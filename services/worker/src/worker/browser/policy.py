"""Decides, in code, whether an action is allowed. The model's wording never enters into it."""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from worker.browser.actions import Action, Back, Click, FillSecret, Press, Select, Type
from worker.browser.snapshot import Node


class ActionClass(StrEnum):
    READ = "read"
    NAVIGATE = "navigate"
    FILL_FORM = "fill_form"
    SUBMIT_FORM = "submit_form"
    PURCHASE = "purchase"
    SEND_MESSAGE = "send_message"
    DELETE = "delete"
    ACCOUNT_SETTINGS = "account_settings"
    CREDENTIAL_USE = "credential_use"


@dataclass(frozen=True)
class Scope:
    """What a task may do. Anything in neither set is refused outright."""

    allow: frozenset[ActionClass]
    approve: frozenset[ActionClass] = frozenset()  # allowed only after the owner says yes


READ_ONLY_SCOPE = Scope(
    allow=frozenset({ActionClass.READ, ActionClass.NAVIGATE, ActionClass.FILL_FORM})
)
# The default for a confirmed task: browse freely; anything that changes something needs a yes.
DEFAULT_SCOPE = Scope(
    allow=READ_ONLY_SCOPE.allow,
    approve=frozenset(
        {
            ActionClass.SUBMIT_FORM,
            ActionClass.PURCHASE,
            ActionClass.SEND_MESSAGE,
            ActionClass.DELETE,
            ActionClass.ACCOUNT_SETTINGS,
        }
    ),
)
# Classes whose effects cannot be undone by going back: these go through the action ledger.
IRREVERSIBLE = DEFAULT_SCOPE.approve


def scope_from_json(raw: dict[str, list[str]]) -> Scope:
    """A task's stored scope; an empty object means the default."""
    if not raw:
        return DEFAULT_SCOPE
    return Scope(
        allow=frozenset(ActionClass(c) for c in raw.get("allow", [])),
        approve=frozenset(ActionClass(c) for c in raw.get("approve", [])),
    )


# Checked in this order; the first match wins, so the riskiest classes come first.
_NAME_RULES: dict[ActionClass, str] = {
    ActionClass.PURCHASE: r"buy|purchase|check ?out|place (your )?order|pay|add to (cart|bag)",
    ActionClass.DELETE: r"delete|remove|cancel (order|subscription)|unsubscribe",
    ActionClass.SEND_MESSAGE: r"send|post|reply|comment|tweet|publish",
    ActionClass.CREDENTIAL_USE: r"sign in|log ?in|password|card number|cvv|cvc|security code",
    ActionClass.ACCOUNT_SETTINGS: r"settings|change (email|password)|save changes",
    ActionClass.SUBMIT_FORM: r"submit|confirm|subscribe|sign up|register|apply|continue to",
}
_PATTERNS = tuple(
    (action_class, re.compile(rf"\b({words})\b", re.IGNORECASE))
    for action_class, words in _NAME_RULES.items()
)


def _by_name(name: str) -> ActionClass | None:
    for action_class, pattern in _PATTERNS:
        if pattern.search(name):
            return action_class
    return None


@dataclass(frozen=True)
class Verdict:
    effect: Literal["allow", "approve", "deny"]
    action_class: ActionClass
    reason: str = ""


def classify(
    action: Action, node: Node | None, last_typed: Node | None, submits_post: bool = False
) -> ActionClass:
    """The strictest class that fits. When unsure it rounds up, never down."""
    match action:
        case Click():
            by_name = _by_name(node.name) if node else None
            # A harmless label ("Yes!", "Continue") on a button that posts a form is not harmless.
            return by_name or (ActionClass.SUBMIT_FORM if submits_post else ActionClass.READ)
        case Type():
            if node is None:
                return ActionClass.SUBMIT_FORM
            return _by_name(node.name) or ActionClass.FILL_FORM
        case Select():
            return ActionClass.FILL_FORM
        case FillSecret():
            return ActionClass.CREDENTIAL_USE
        case Press(key="Enter"):
            # Enter submits whatever form has focus. Only a search box is plainly harmless.
            is_search = last_typed is not None and (
                last_typed.role == "searchbox" or "search" in last_typed.name.lower()
            )
            return ActionClass.FILL_FORM if is_search else ActionClass.SUBMIT_FORM
        case Back():
            return ActionClass.NAVIGATE
        case _:
            return ActionClass.READ if action.kind != "navigate" else ActionClass.NAVIGATE


def check(
    action: Action,
    node: Node | None,
    last_typed: Node | None,
    scope: Scope,
    submits_post: bool = False,
) -> Verdict:
    action_class = classify(action, node, last_typed, submits_post)
    if action_class in scope.allow:
        return Verdict("allow", action_class)
    target = f" on '{node.name}'" if node else ""
    if action_class in scope.approve:
        return Verdict("approve", action_class, f"{action.kind}{target} ({action_class.value})")
    allowed = ", ".join(sorted(c.value for c in scope.allow | scope.approve))
    return Verdict(
        "deny",
        action_class,
        f"{action.kind}{target} is a {action_class.value} action; this task may only {allowed}",
    )
