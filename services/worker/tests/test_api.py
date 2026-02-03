import asyncio
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from agent_common.signing import signed_headers
from psycopg.rows import DictRow

from tests.conftest import HMAC_SECRET, FakeTwilio, SignedClient, User

pytestmark = pytest.mark.db
Admin = psycopg.Connection[DictRow]


async def start_call(api: SignedClient, user: User, call_sid: str | None = None) -> dict[str, Any]:
    response = await api.post(
        "/v1/calls/start", {"call_sid": call_sid or f"CA{uuid4().hex}", "caller_number": user.phone}
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


async def create_task(
    api: SignedClient,
    conversation_id: object,
    request_id: UUID | None = None,
    title: str = "Compare laptops",
) -> dict[str, Any]:
    response = await api.post(
        "/v1/tasks",
        {
            "request_id": str(request_id or uuid4()),
            "conversation_id": str(conversation_id),
            "title": title,
            "spec": {"sites": ["bestbuy.ca"]},
        },
    )
    assert response.status_code in (200, 201), response.text
    return dict(response.json())


# --- request authentication ---------------------------------------------------


async def test_unsigned_request_is_rejected(api: SignedClient) -> None:
    response = await api.raw.post("/v1/tasks", json={"anything": True})
    assert response.status_code == 401


async def test_body_changed_after_signing_is_rejected(api: SignedClient) -> None:
    headers = signed_headers(HMAC_SECRET, "POST", "/v1/sms/inbound", b'{"body":"pause T3"}')
    response = await api.raw.post(
        "/v1/sms/inbound", content=b'{"body":"cancel T3"}', headers=headers
    )
    assert response.status_code == 401


async def test_signed_but_malformed_body_is_422(api: SignedClient) -> None:
    response = await api.post("/v1/calls/start", {"call_sid": "CA1", "caller_number": "416-555"})
    assert response.status_code == 422


async def test_healthz_needs_no_signature(api: SignedClient) -> None:
    assert (await api.raw.get("/healthz")).json() == {"status": "ok"}


# --- calls ----------------------------------------------------------------------


async def test_unknown_caller_is_refused_and_audited_without_full_number(
    api: SignedClient, admin: Admin
) -> None:
    response = await api.post(
        "/v1/calls/start", {"call_sid": "CAx", "caller_number": "+19995550123"}
    )

    assert response.status_code == 403
    event = admin.execute(
        "select metadata from audit.events where action = 'call_rejected' order by id desc limit 1"
    ).fetchone()
    assert event is not None
    assert event["metadata"] == {"reason": "unknown_caller", "number_suffix": "0123"}


async def test_unverified_number_is_treated_as_unknown(
    api: SignedClient, make_user: Callable[..., User]
) -> None:
    user = make_user(verified=False)
    response = await api.post("/v1/calls/start", {"call_sid": "CAy", "caller_number": user.phone})
    assert response.status_code == 403


async def test_redispatched_call_maps_to_one_conversation(
    api: SignedClient, make_user: Callable[..., User]
) -> None:
    user = make_user()
    first = await start_call(api, user, call_sid="CA-same")
    second = await start_call(api, user, call_sid="CA-same")
    assert first["conversation_id"] == second["conversation_id"]


async def test_call_start_lists_active_tasks(
    api: SignedClient, make_user: Callable[..., User]
) -> None:
    user = make_user()
    call = await start_call(api, user)
    await create_task(api, call["conversation_id"], title="Find flights")

    again = await start_call(api, user)

    assert again["active_tasks"] == [
        {"short_code": "T1", "title": "Find flights", "status": "queued"}
    ]


async def test_recent_dropped_call_is_offered_for_resume(
    api: SignedClient, make_user: Callable[..., User], admin: Admin
) -> None:
    user = make_user()
    first = await start_call(api, user)
    state = {"open_questions": [{"id": "q1", "text": "Which dates?"}]}
    await api.post(
        f"/v1/calls/{first['conversation_id']}/turns",
        {"turn_index": 0, "speaker": "agent", "text": "Which dates?", "state": state},
    )
    await api.post(f"/v1/calls/{first['conversation_id']}/end", {"reason": "dropped"})

    second = await start_call(api, user)
    assert second["resume"] is not None
    assert second["resume"]["state"] == state

    # Outside the 30-minute window it is a fresh conversation.
    admin.execute(
        "update public.conversations set ended_at = now() - interval '31 minutes' where id = %s",
        (first["conversation_id"],),
    )
    third = await start_call(api, user)
    assert third["resume"] is None or third["resume"]["conversation_id"] != first["conversation_id"]


async def test_turn_retries_store_one_message_and_redact_card_numbers(
    api: SignedClient, make_user: Callable[..., User], admin: Admin
) -> None:
    call = await start_call(api, make_user())
    turn = {
        "turn_index": 3,
        "speaker": "user",
        "text": "my card is 4111 1111 1111 1111 and the budget is 1200 dollars",
        "asr_confidence": 0.91,
        "state": {"intent": "shopping"},
    }
    for _ in range(2):
        response = await api.post(f"/v1/calls/{call['conversation_id']}/turns", turn)
        assert response.status_code == 204

    rows = admin.execute(
        "select body_redacted from public.messages where conversation_id = %s",
        (call["conversation_id"],),
    ).fetchall()
    assert [r["body_redacted"] for r in rows] == [
        "my card is [card number removed] and the budget is 1200 dollars"
    ]


async def test_turn_for_unknown_conversation_is_404(api: SignedClient) -> None:
    response = await api.post(
        f"/v1/calls/{uuid4()}/turns",
        {"turn_index": 0, "speaker": "user", "text": "hi", "state": {}},
    )
    assert response.status_code == 404


async def test_ending_a_call_leaves_its_tasks_running(
    api: SignedClient, make_user: Callable[..., User], admin: Admin
) -> None:
    call = await start_call(api, make_user())
    task = await create_task(api, call["conversation_id"])

    await api.post(f"/v1/calls/{call['conversation_id']}/end", {"reason": "caller_hung_up"})

    row = admin.execute(
        "select status from public.tasks where id = %s", (task["task_id"],)
    ).fetchone()
    assert row == {"status": "queued"}


# --- tasks ----------------------------------------------------------------------


async def test_short_codes_count_up_per_user(
    api: SignedClient, make_user: Callable[..., User]
) -> None:
    alice, bob = make_user(), make_user()
    alice_call, bob_call = await start_call(api, alice), await start_call(api, bob)

    codes = [
        (await create_task(api, alice_call["conversation_id"]))["short_code"],
        (await create_task(api, alice_call["conversation_id"]))["short_code"],
        (await create_task(api, bob_call["conversation_id"]))["short_code"],
    ]
    assert codes == ["T1", "T2", "T1"]


async def test_concurrent_creations_get_distinct_codes(
    api: SignedClient, make_user: Callable[..., User]
) -> None:
    call = await start_call(api, make_user())
    results = await asyncio.gather(*(create_task(api, call["conversation_id"]) for _ in range(8)))
    assert sorted(r["short_code"] for r in results) == sorted(f"T{n}" for n in range(1, 9))


async def test_retried_request_returns_same_task_and_texts_once(
    api: SignedClient, make_user: Callable[..., User], twilio: FakeTwilio
) -> None:
    user = make_user()
    call = await start_call(api, user)
    request_id = uuid4()

    first, second = await asyncio.gather(
        create_task(api, call["conversation_id"], request_id),
        create_task(api, call["conversation_id"], request_id),
    )

    assert first["task_id"] == second["task_id"]
    assert sorted([first["created"], second["created"]]) == [False, True]
    assert len(twilio.sent) == 1
    assert twilio.sent[0]["To"] == user.phone
    assert twilio.sent[0]["Body"] == "T1 started: Compare laptops. I'll text updates here."


async def test_sms_failure_does_not_lose_the_task(
    api: SignedClient, make_user: Callable[..., User], twilio: FakeTwilio, admin: Admin
) -> None:
    call = await start_call(api, make_user())
    twilio.fail_with = (500, {"code": 20500, "message": "Internal Server Error"})

    task = await create_task(api, call["conversation_id"])

    assert task["created"] is True
    message = admin.execute(
        "select delivery_status from public.messages where dedupe_key = %s",
        (f"task:{task['task_id']}:created",),
    ).fetchone()
    assert message == {"delivery_status": "failed"}


async def test_opted_out_recipient_is_remembered(
    api: SignedClient, make_user: Callable[..., User], twilio: FakeTwilio, admin: Admin
) -> None:
    user = make_user()
    call = await start_call(api, user)
    twilio.fail_with = (
        400,
        {"code": 21610, "message": "Attempt to send to unsubscribed recipient"},
    )

    await create_task(api, call["conversation_id"])
    twilio.fail_with = None
    second = await create_task(api, call["conversation_id"])

    phone = admin.execute(
        "select sms_opted_out from public.phone_numbers where e164 = %s", (user.phone,)
    ).fetchone()
    assert phone == {"sms_opted_out": True}
    assert twilio.sent == []  # no further attempts once opted out
    status = admin.execute(
        "select delivery_status from public.messages where dedupe_key = %s",
        (f"task:{second['task_id']}:created",),
    ).fetchone()
    assert status == {"delivery_status": "no_reachable_number"}


async def test_task_for_unknown_conversation_is_404(api: SignedClient) -> None:
    response = await api.post(
        "/v1/tasks",
        {"request_id": str(uuid4()), "conversation_id": str(uuid4()), "title": "x", "spec": {}},
    )
    assert response.status_code == 404


# --- inbound SMS ----------------------------------------------------------------


async def test_inbound_sms_is_stored_once_per_twilio_sid(
    api: SignedClient, make_user: Callable[..., User], admin: Admin
) -> None:
    user = make_user()
    sms = {"message_sid": f"SM{uuid4().hex}", "from_number": user.phone, "body": "how far along?"}

    first, retry = await asyncio.gather(
        api.post("/v1/sms/inbound", sms), api.post("/v1/sms/inbound", sms)
    )

    assert sorted([first.json()["stored"], retry.json()["stored"]]) == [False, True]
    count = admin.execute(
        "select count(*) as n from public.messages where provider_sid = %s", (sms["message_sid"],)
    ).fetchone()
    assert count == {"n": 1}


async def test_concurrent_texts_share_one_sms_conversation(
    api: SignedClient, make_user: Callable[..., User], admin: Admin
) -> None:
    user = make_user()
    await asyncio.gather(
        *(
            api.post(
                "/v1/sms/inbound",
                {"message_sid": f"SM{uuid4().hex}", "from_number": user.phone, "body": f"msg {i}"},
            )
            for i in range(5)
        )
    )
    count = admin.execute(
        "select count(*) as n from public.conversations where user_id = %s and channel = 'sms'",
        (user.id,),
    ).fetchone()
    assert count == {"n": 1}


async def test_sms_from_unknown_number_is_not_stored(api: SignedClient, admin: Admin) -> None:
    sid = f"SM{uuid4().hex}"
    response = await api.post(
        "/v1/sms/inbound", {"message_sid": sid, "from_number": "+19995550199", "body": "hi"}
    )
    assert response.json() == {"stored": False}
    assert (
        admin.execute("select 1 from public.messages where provider_sid = %s", (sid,)).fetchone()
        is None
    )


# --- approvals and the emergency stop ---------------------------------------------------------


def waiting_task_with_approval(admin: Admin, user: User) -> tuple[UUID, UUID]:
    task = admin.execute(
        "insert into public.tasks (user_id, short_code, title, spec, status, waiting_reason, "
        "budget_cents) values (%s, 'T1', 'Buy a TV', '{}', 'waiting_user', 'approval', 300) "
        "returning id",
        (user.id,),
    ).fetchone()
    assert task is not None
    approval = admin.execute(
        "insert into public.approvals (user_id, task_id, action_class, summary, intent_hash, "
        "expires_at) values (%s, %s, 'purchase', 'Place order', 'h', "
        "now() + interval '30 minutes') returning id",
        (user.id, task["id"]),
    ).fetchone()
    assert approval is not None
    return UUID(str(task["id"])), UUID(str(approval["id"]))


async def test_approving_with_a_fresh_code_resumes_the_task(
    api: SignedClient, make_user: Callable[..., User], admin: Admin
) -> None:
    user = make_user()
    task_id, approval_id = waiting_task_with_approval(admin, user)
    token = api.auth.sign_in(user, mfa_age_seconds=30)

    response = await api.post_as(
        token, f"/v1/approvals/{approval_id}/decide", {"decision": "approved"}
    )

    assert response.status_code == 204
    status = admin.execute("select status from public.tasks where id = %s", (task_id,)).fetchone()
    assert status == {"status": "running"}


@pytest.mark.parametrize("age", [None, 600])
async def test_approving_without_a_recent_code_is_refused_and_changes_nothing(
    api: SignedClient, make_user: Callable[..., User], admin: Admin, age: int | None
) -> None:
    user = make_user()
    task_id, approval_id = waiting_task_with_approval(admin, user)
    token = api.auth.sign_in(user, mfa_age_seconds=age)

    response = await api.post_as(
        token, f"/v1/approvals/{approval_id}/decide", {"decision": "approved"}
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "mfa_stale"
    status = admin.execute("select status from public.tasks where id = %s", (task_id,)).fetchone()
    assert status == {"status": "waiting_user"}


async def test_a_password_only_session_cannot_reach_any_owner_endpoint(
    api: SignedClient, make_user: Callable[..., User], admin: Admin
) -> None:
    user = make_user()
    _, approval_id = waiting_task_with_approval(admin, user)
    weak = api.auth.sign_in(user, aal="aal1", mfa_age_seconds=None)
    for path in (f"/v1/approvals/{approval_id}/decide", "/v1/emergency-stop/clear", "/v1/accounts"):
        response = await api.post_as(weak, path, {"decision": "denied"})
        assert response.status_code == 403, path
        assert response.json()["detail"] == "mfa_required"


async def test_owner_endpoints_reject_missing_and_unknown_tokens(
    api: SignedClient, make_user: Callable[..., User], admin: Admin
) -> None:
    _, approval_id = waiting_task_with_approval(admin, make_user())
    path = f"/v1/approvals/{approval_id}/decide"
    assert (await api.post_as(None, path, {"decision": "denied"})).status_code == 401
    assert (await api.post_as("not-a-token", path, {"decision": "denied"})).status_code == 401


async def test_another_user_cannot_decide_and_a_second_decision_conflicts(
    api: SignedClient, make_user: Callable[..., User], admin: Admin
) -> None:
    owner, stranger = make_user(), make_user()
    _, approval_id = waiting_task_with_approval(admin, owner)
    path = f"/v1/approvals/{approval_id}/decide"
    owner_token, stranger_token = api.auth.sign_in(owner), api.auth.sign_in(stranger)

    assert (await api.post_as(stranger_token, path, {"decision": "approved"})).status_code == 404
    assert (await api.post_as(owner_token, path, {"decision": "denied"})).status_code == 204
    assert (await api.post_as(owner_token, path, {"decision": "approved"})).status_code == 409


async def test_clearing_the_emergency_stop_is_audited(
    api: SignedClient, make_user: Callable[..., User], admin: Admin
) -> None:
    user = make_user()
    admin.execute(
        "insert into public.user_settings (user_id, emergency_stop) values (%s, true)", (user.id,)
    )

    stale = api.auth.sign_in(user, mfa_age_seconds=900)
    assert (await api.post_as(stale, "/v1/emergency-stop/clear")).status_code == 403

    response = await api.post_as(api.auth.sign_in(user), "/v1/emergency-stop/clear")

    assert response.status_code == 204
    flag = admin.execute(
        "select emergency_stop from public.user_settings where user_id = %s", (user.id,)
    ).fetchone()
    assert flag == {"emergency_stop": False}
    audit = admin.execute(
        "select 1 from audit.events where user_id = %s and action = 'emergency_stop_cleared'",
        (user.id,),
    )
    assert audit.fetchone() is not None


ACCOUNT = {
    "site_domain": "shop.example",
    "label": "main",
    "username": "alice@mail.example",
    "password": "correct-horse-battery",
}


async def test_connecting_an_account_over_the_api_stores_the_password_only_in_the_store(
    api: SignedClient, make_user: Callable[..., User], admin: Admin
) -> None:
    user = make_user()
    token = api.auth.sign_in(user)

    response = await api.post_as(token, "/v1/accounts", ACCOUNT)

    assert response.status_code == 201
    account_id = response.json()["account_id"]
    row = admin.execute("select * from public.connected_accounts where id = %s", (account_id,))
    assert "correct-horse-battery" not in str(row.fetchone())
    assert any("correct-horse-battery" in v for v in api.secrets.secrets.values())

    gone = await api.post_as(token, f"/v1/accounts/{account_id}/disconnect")
    assert gone.status_code == 204
    assert api.secrets.secrets == {}


async def test_connecting_needs_a_fresh_code_and_refuses_foreign_origins(
    api: SignedClient, make_user: Callable[..., User]
) -> None:
    user = make_user()
    stale = api.auth.sign_in(user, mfa_age_seconds=900)
    assert (await api.post_as(stale, "/v1/accounts", ACCOUNT)).status_code == 403

    fresh = api.auth.sign_in(user)
    phishing = {**ACCOUNT, "allowed_origins": ["https://shop.example.evil.test"]}
    response = await api.post_as(fresh, "/v1/accounts", phishing)
    assert response.status_code == 422
    assert api.secrets.secrets == {}


async def test_the_dashboard_origin_may_call_the_api_from_a_browser_and_no_other(
    api: SignedClient,
) -> None:
    ask = {"origin": "https://dash.example", "access-control-request-method": "POST"}
    allowed = await api.raw.options("/v1/accounts", headers=ask)
    assert allowed.headers["access-control-allow-origin"] == "https://dash.example"

    other = await api.raw.options("/v1/accounts", headers={**ask, "origin": "https://evil.example"})
    assert "access-control-allow-origin" not in other.headers
