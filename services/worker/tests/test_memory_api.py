"""Memory over HTTP: what the voice agent and the dashboard can ask for, and who may."""

from collections.abc import Callable
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import DictRow

from tests.conftest import RecordingDispatcher, SignedClient, User
from tests.memory_support_db import add_memory
from tests.test_api import start_call

pytestmark = pytest.mark.db

FACT = "Always buy electronics from Best Buy."


async def test_the_call_start_card_carries_standing_rules(
    api: SignedClient, admin: psycopg.Connection[DictRow], make_user: Callable[..., User]
) -> None:
    user = make_user()
    add_memory(admin, user.id, FACT)

    started = await start_call(api, user)

    assert started["memory"]["lines"] == [FACT]


async def test_a_paused_memory_gives_the_voice_agent_nothing(
    api: SignedClient, admin: psycopg.Connection[DictRow], make_user: Callable[..., User]
) -> None:
    user = make_user()
    add_memory(admin, user.id, FACT)
    token = api.auth.sign_in(user)
    assert (await api.post_as(token, "/v1/memory/enabled", {"enabled": False})).status_code == 204

    started = await start_call(api, user)

    assert started["memory"] == {"lines": [], "to_confirm": [], "changes": []}
    call = started["conversation_id"]
    recalled = await api.post(f"/v1/calls/{call}/memory/recall", {"query": "electronics"})
    assert recalled.json() == {"items": []}


async def test_ending_a_call_queues_learning_from_it(
    api: SignedClient, make_user: Callable[..., User], dispatcher: RecordingDispatcher
) -> None:
    started = await start_call(api, make_user())

    response = await api.post(f"/v1/calls/{started['conversation_id']}/end", {"reason": "hangup"})

    assert response.status_code == 204
    assert [str(c) for c in dispatcher.remembered] == [started["conversation_id"]]


async def test_the_voice_agent_can_recall_and_forget(
    api: SignedClient, admin: psycopg.Connection[DictRow], make_user: Callable[..., User]
) -> None:
    user = make_user()
    memory = add_memory(admin, user.id, FACT)
    call = (await start_call(api, user))["conversation_id"]

    recalled = await api.post(
        f"/v1/calls/{call}/memory/recall", {"query": "where to buy electronics"}
    )
    forgotten = await api.post(f"/v1/calls/{call}/memory/forget", {"memory_ids": [str(memory)]})
    after = await api.post(f"/v1/calls/{call}/memory/recall", {"query": "where to buy electronics"})

    assert recalled.json()["items"] == [{"id": str(memory), "kind": "rule", "text": FACT}]
    assert forgotten.json() == {"forgotten": 1}
    assert after.json() == {"items": []}


async def test_a_call_cannot_forget_or_settle_someone_elses_memory(
    api: SignedClient, admin: psycopg.Connection[DictRow], make_user: Callable[..., User]
) -> None:
    caller, owner = make_user(), make_user()
    theirs = add_memory(admin, owner.id, FACT)
    pending = add_memory(admin, owner.id, "Prefers Costco.", status="pending_confirm")
    call = (await start_call(api, caller))["conversation_id"]

    forgotten = await api.post(f"/v1/calls/{call}/memory/forget", {"memory_ids": [str(theirs)]})
    await api.post(f"/v1/calls/{call}/memory/settle", {"memory_id": str(pending), "keep": False})

    assert forgotten.json() == {"forgotten": 0}
    states = admin.execute(
        "select status from public.memories where user_id = %s order by status", (owner.id,)
    )
    assert [s["status"] for s in states.fetchall()] == ["active", "pending_confirm"]


async def test_memory_calls_for_an_unknown_conversation_are_404(api: SignedClient) -> None:
    unknown = uuid4()
    bodies: list[tuple[str, dict[str, object]]] = [
        ("recall", {"query": "x"}),
        ("forget", {"memory_ids": [str(uuid4())]}),
        ("settle", {"memory_id": str(uuid4()), "keep": True}),
    ]
    for path, body in bodies:
        response = await api.post(f"/v1/calls/{unknown}/memory/{path}", body)
        assert response.status_code == 404, path


async def test_memory_calls_need_the_service_signature(api: SignedClient) -> None:
    response = await api.raw.post(f"/v1/calls/{uuid4()}/memory/recall", json={"query": "x"})
    assert response.status_code == 401


async def test_the_owner_edits_deletes_and_restores_from_the_dashboard(
    api: SignedClient, admin: psycopg.Connection[DictRow], make_user: Callable[..., User]
) -> None:
    user = make_user()
    memory = add_memory(admin, user.id, FACT)
    token = api.auth.sign_in(user)

    edited = await api.post_as(
        token, f"/v1/memories/{memory}/edit", {"text": "Buy electronics from Costco."}
    )
    new_id = edited.json()["id"]
    deleted = await api.post_as(token, f"/v1/memories/{new_id}/delete", {})
    again = await api.post_as(token, f"/v1/memories/{new_id}/delete", {"immediately": True})
    restored = await api.post_as(token, f"/v1/memories/{new_id}/restore", {})

    assert edited.status_code == 200 and deleted.status_code == 204
    # Deleting hid the new wording and its older version; the immediate delete erased both.
    assert again.status_code == 204 and restored.status_code == 404
    assert admin.execute(
        "select count(*) n from public.memories where user_id = %s", (user.id,)
    ).fetchone() == {"n": 0}


async def test_a_secret_in_an_edit_is_refused_and_a_stranger_cannot_edit(
    api: SignedClient, admin: psycopg.Connection[DictRow], make_user: Callable[..., User]
) -> None:
    owner, stranger = make_user(), make_user()
    memory = add_memory(admin, owner.id, FACT)

    sensitive = await api.post_as(
        api.auth.sign_in(owner), f"/v1/memories/{memory}/edit", {"text": "my pin is 4921"}
    )
    foreign = await api.post_as(
        api.auth.sign_in(stranger), f"/v1/memories/{memory}/edit", {"text": "Use Costco."}
    )

    assert sensitive.status_code == 422 and foreign.status_code == 404
    assert "secret" in sensitive.text


async def test_the_dashboard_settles_a_habit(
    api: SignedClient, admin: psycopg.Connection[DictRow], make_user: Callable[..., User]
) -> None:
    user = make_user()
    memory = add_memory(admin, user.id, "Prefers Costco.", status="pending_confirm")
    token = api.auth.sign_in(user)

    response = await api.post_as(
        token, f"/v1/memories/{memory}/settle", {"memory_id": str(memory), "keep": True}
    )
    repeat = await api.post_as(
        token, f"/v1/memories/{memory}/settle", {"memory_id": str(memory), "keep": True}
    )

    assert (response.status_code, repeat.status_code) == (204, 404)


async def test_clearing_all_memories_needs_a_fresh_authenticator_code(
    api: SignedClient, admin: psycopg.Connection[DictRow], make_user: Callable[..., User]
) -> None:
    user = make_user()
    add_memory(admin, user.id, FACT)

    stale = await api.post_as(
        api.auth.sign_in(user, mfa_age_seconds=3600), "/v1/memories/clear", {}
    )
    fresh = await api.post_as(api.auth.sign_in(user), "/v1/memories/clear", {"type": "rule"})

    assert (stale.status_code, stale.json()["detail"]) == (403, "mfa_stale")
    assert fresh.json() == {"cleared": 1}


@pytest.mark.parametrize(
    "path", ["/v1/memories/clear", "/v1/memory/enabled", f"/v1/memories/{uuid4()}/delete"]
)
async def test_a_password_only_session_cannot_touch_memory(
    api: SignedClient, make_user: Callable[..., User], path: str
) -> None:
    weak = api.auth.sign_in(make_user(), aal="aal1")

    response = await api.post_as(weak, path, {"enabled": True})

    assert response.status_code == 403
