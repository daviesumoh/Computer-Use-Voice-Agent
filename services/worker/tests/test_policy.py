import pytest

from worker.browser.actions import Action, Back, Click, Done, Navigate, Press, Scroll, Select, Type
from worker.browser.policy import (
    DEFAULT_SCOPE,
    READ_ONLY_SCOPE,
    ActionClass,
    Scope,
    check,
    classify,
    scope_from_json,
)
from worker.browser.snapshot import Node


def node(role: str, name: str) -> Node:
    return Node("e1", role, name)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Buy now", ActionClass.PURCHASE),
        ("Add to cart", ActionClass.PURCHASE),
        ("Place your order", ActionClass.PURCHASE),
        ("Checkout", ActionClass.PURCHASE),
        ("Delete account", ActionClass.DELETE),
        ("Send message", ActionClass.SEND_MESSAGE),
        ("Sign in", ActionClass.CREDENTIAL_USE),
        ("Save changes", ActionClass.ACCOUNT_SETTINGS),
        ("Subscribe to newsletter", ActionClass.SUBMIT_FORM),
        ("Borealis 50 TV", ActionClass.READ),
        ("Next page", ActionClass.READ),
    ],
)
def test_clicks_are_classed_by_what_the_control_says(name: str, expected: ActionClass) -> None:
    assert classify(Click(kind="click", ref="e1"), node("button", name), None) == expected


def test_a_click_on_an_unknown_element_is_not_waved_through() -> None:
    # Pre-flight would have rejected it, but classification must not assume it is harmless.
    assert classify(Type(kind="type", ref="e9", text="x"), None, None) == ActionClass.SUBMIT_FORM


@pytest.mark.parametrize("field", ["Password", "Card number", "CVV"])
def test_typing_into_credential_or_payment_fields_is_never_plain_form_filling(field: str) -> None:
    action = Type(kind="type", ref="e1", text="x")
    assert classify(action, node("textbox", field), None) == ActionClass.CREDENTIAL_USE


def test_enter_is_harmless_only_in_a_search_box() -> None:
    enter = Press(kind="press", key="Enter")
    assert classify(enter, None, node("searchbox", "Find")) == ActionClass.FILL_FORM
    assert classify(enter, None, node("textbox", "Search products")) == ActionClass.FILL_FORM
    assert classify(enter, None, node("textbox", "Email address")) == ActionClass.SUBMIT_FORM
    assert classify(enter, None, None) == ActionClass.SUBMIT_FORM


def test_read_only_scope_allows_browsing_and_searching_but_nothing_that_changes_a_site() -> None:
    allowed: list[tuple[Action, Node | None]] = [
        (Navigate(kind="navigate", url="https://x.ca"), None),
        (Scroll(kind="scroll", direction="down"), None),
        (Back(kind="back"), None),
        (Select(kind="select", ref="e1", option="Newest"), node("combobox", "Sort")),
        (Click(kind="click", ref="e1"), node("link", "Aurora TV")),
        (Done(kind="done", result="ok"), None),
    ]
    for action, target in allowed:
        assert check(action, target, None, READ_ONLY_SCOPE).effect == "allow", action

    denied = check(Click(kind="click", ref="e1"), node("button", "Buy now"), None, READ_ONLY_SCOPE)
    assert denied.effect == "deny"
    assert denied.action_class == ActionClass.PURCHASE
    assert "Buy now" in denied.reason


def test_the_default_scope_lets_changes_through_only_with_approval() -> None:
    buy, target = Click(kind="click", ref="e1"), node("button", "Add to cart")
    assert check(buy, target, None, DEFAULT_SCOPE).effect == "approve"
    assert check(buy, target, None, READ_ONLY_SCOPE).effect == "deny"
    assert (
        check(buy, target, None, Scope(allow=frozenset({ActionClass.PURCHASE}))).effect == "allow"
    )


def test_credentials_are_never_typed_by_the_model_even_in_the_default_scope() -> None:
    typed = Type(kind="type", ref="e1", text="hunter2")
    verdict = check(typed, node("textbox", "Password"), None, DEFAULT_SCOPE)
    assert verdict.effect == "deny"
    assert verdict.action_class == ActionClass.CREDENTIAL_USE


def test_a_stored_scope_round_trips_and_empty_means_default() -> None:
    assert scope_from_json({}) == DEFAULT_SCOPE
    custom = scope_from_json({"allow": ["read", "navigate"], "approve": ["purchase"]})
    assert custom.allow == {ActionClass.READ, ActionClass.NAVIGATE}
    assert custom.approve == {ActionClass.PURCHASE}


def test_the_action_schema_avoids_keywords_openai_strict_mode_rejects() -> None:
    from worker.browser.decider import Choice

    schema = str(Choice.model_json_schema())
    assert "oneOf" not in schema
    assert "anyOf" in schema
