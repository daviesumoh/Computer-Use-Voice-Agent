"""Finding memories again: ranking, relevance, and what must never come back."""

from collections.abc import Callable

import psycopg
import pytest
from psycopg.rows import DictRow

from tests.conftest import User
from tests.memory_support import FakeEmbedder
from tests.memory_support_db import add_episode, add_memory
from worker.db import Pool
from worker.memory import manage, recall

pytestmark = pytest.mark.db

FACT_TEXT = "Always buy electronics from Best Buy."


async def test_the_most_relevant_memory_comes_first(
    pool: Pool,
    admin: psycopg.Connection[DictRow],
    make_user: Callable[..., User],
    embedder: FakeEmbedder,
) -> None:
    user = make_user()
    add_memory(admin, user.id, "Always buy electronics from Best Buy.")
    add_memory(admin, user.id, "Ship parcels to the office address on weekdays.")
    add_memory(admin, user.id, "Prefers window seats on flights.")

    async with pool.connection() as conn:
        found = await recall.recall(conn, embedder, user.id, "where should I buy electronics")

    assert found[0].text == "Always buy electronics from Best Buy."


async def test_unrelated_queries_recall_nothing(
    pool: Pool,
    admin: psycopg.Connection[DictRow],
    make_user: Callable[..., User],
    embedder: FakeEmbedder,
) -> None:
    user = make_user()
    add_memory(admin, user.id, "Always buy electronics from Best Buy.")
    add_memory(admin, user.id, "Prefers window seats on flights.")

    async with pool.connection() as conn:
        assert await recall.recall(conn, embedder, user.id, "quarterly tax filing deadline") == []


async def test_a_fresher_memory_wins_between_equally_relevant_ones(
    pool: Pool,
    admin: psycopg.Connection[DictRow],
    make_user: Callable[..., User],
    embedder: FakeEmbedder,
) -> None:
    user = make_user()
    add_memory(admin, user.id, "Order groceries from Loblaws.", age_days=300)
    add_memory(admin, user.id, "Order groceries from Metro.", age_days=1)

    async with pool.connection() as conn:
        found = await recall.recall(conn, embedder, user.id, "order groceries")

    assert [r.text for r in found][:1] == ["Order groceries from Metro."]


@pytest.mark.parametrize("status", ["superseded", "deleted", "pending_confirm"])
async def test_only_active_memories_come_back(
    pool: Pool, admin: psycopg.Connection[DictRow], make_user: Callable[..., User],
    embedder: FakeEmbedder, status: str,
) -> None:  # fmt: skip
    user = make_user()
    add_memory(admin, user.id, "Always buy electronics from Best Buy.", status=status)

    async with pool.connection() as conn:
        assert await recall.recall(conn, embedder, user.id, "electronics") == []


async def test_one_users_memories_are_invisible_to_another(
    pool: Pool,
    admin: psycopg.Connection[DictRow],
    make_user: Callable[..., User],
    embedder: FakeEmbedder,
) -> None:
    owner, stranger = make_user(), make_user()
    add_memory(admin, owner.id, "Always buy electronics from Best Buy.")
    add_episode(admin, owner.id, "Compared electronics prices.")

    async with pool.connection() as conn:
        assert await recall.recall(conn, embedder, stranger.id, "electronics") == []


async def test_episodes_are_found_alongside_memories_and_can_be_excluded(
    pool: Pool,
    admin: psycopg.Connection[DictRow],
    make_user: Callable[..., User],
    embedder: FakeEmbedder,
) -> None:
    user = make_user()
    add_memory(admin, user.id, "Always buy electronics from Best Buy.")
    add_episode(admin, user.id, "Asked for electronics deals near Toronto.", kind="task")

    async with pool.connection() as conn:
        both = await recall.recall(conn, embedder, user.id, "electronics")
        only_memories = await recall.recall(
            conn, embedder, user.id, "electronics", with_episodes=False
        )

    assert {r.kind for r in both} == {"rule", "task"}
    assert {r.kind for r in only_memories} == {"rule"}


async def test_types_can_be_filtered(
    pool: Pool,
    admin: psycopg.Connection[DictRow],
    make_user: Callable[..., User],
    embedder: FakeEmbedder,
) -> None:
    user = make_user()
    add_memory(admin, user.id, "Always buy electronics from Best Buy.", kind="rule")
    add_memory(admin, user.id, "Has a Best Buy electronics membership.", kind="fact")

    async with pool.connection() as conn:
        found = await recall.recall(
            conn, embedder, user.id, "electronics", types=("fact",), with_episodes=False
        )

    assert [r.kind for r in found] == ["fact"]


async def test_recall_marks_what_it_returned_as_used(
    pool: Pool,
    admin: psycopg.Connection[DictRow],
    make_user: Callable[..., User],
    embedder: FakeEmbedder,
) -> None:
    user = make_user()
    used = add_memory(admin, user.id, "Always buy electronics from Best Buy.")
    unused = add_memory(admin, user.id, "Prefers window seats on flights.")

    async with pool.connection() as conn:
        await recall.recall(conn, embedder, user.id, "electronics")

    rows = {
        r["id"]: r["last_used_at"]
        for r in admin.execute(
            "select id, last_used_at from public.memories where user_id = %s", (user.id,)
        )
    }
    assert rows[used] is not None and rows[unused] is None


async def test_the_result_stays_within_its_size_budget(
    pool: Pool,
    admin: psycopg.Connection[DictRow],
    make_user: Callable[..., User],
    embedder: FakeEmbedder,
) -> None:
    user = make_user()
    for n in range(12):
        add_memory(admin, user.id, f"Rule {n}: " + "electronics " * 40)

    async with pool.connection() as conn:
        found = await recall.recall(conn, embedder, user.id, "electronics", limit=12)

    assert 0 < sum(len(r.text) for r in found) <= recall.MAX_CHARS


async def test_a_paused_memory_recalls_nothing_until_resumed(
    pool: Pool,
    admin: psycopg.Connection[DictRow],
    make_user: Callable[..., User],
    embedder: FakeEmbedder,
) -> None:
    user = make_user()
    add_memory(admin, user.id, "Always buy electronics from Best Buy.")
    async with pool.connection() as conn:
        await manage.set_enabled(conn, user.id, False)
        assert await recall.recall(conn, embedder, user.id, "electronics") == []
        assert not await recall.enabled(conn, user.id)
        await manage.set_enabled(conn, user.id, True)
        assert len(await recall.recall(conn, embedder, user.id, "electronics")) == 1


async def test_the_profile_card_lists_rules_and_preferences_but_not_facts_or_pending_ones(
    pool: Pool, admin: psycopg.Connection[DictRow], make_user: Callable[..., User]
) -> None:
    user = make_user()
    add_memory(admin, user.id, "Always buy electronics from Best Buy.", kind="rule")
    add_memory(admin, user.id, "Prefers window seats.", kind="preference")
    add_memory(admin, user.id, "Lives in Toronto.", kind="fact")
    add_memory(admin, user.id, "Maybe likes Costco.", kind="preference", status="pending_confirm")

    async with pool.connection() as conn:
        card = await recall.profile_card(conn, user.id)

    assert sorted(card.lines) == ["Always buy electronics from Best Buy.", "Prefers window seats."]


async def test_a_replaced_statement_is_announced_once(
    pool: Pool,
    admin: psycopg.Connection[DictRow],
    make_user: Callable[..., User],
    embedder: FakeEmbedder,
) -> None:
    user = make_user()
    old = add_memory(admin, user.id, "Always buy electronics from Best Buy.", status="superseded")
    new = add_memory(admin, user.id, "Always buy electronics from Costco.")
    admin.execute("update public.memories set superseded_by = %s where id = %s", (new, old))

    async with pool.connection() as conn:
        first = await recall.profile_card(conn, user.id)
        second = await recall.profile_card(conn, user.id)

    assert first.changes == [
        "was: Always buy electronics from Best Buy. / now: Always buy electronics from Costco."
    ]
    assert second.changes == []


async def test_a_memory_stored_without_a_vector_is_still_found_by_its_words(
    pool: Pool,
    admin: psycopg.Connection[DictRow],
    make_user: Callable[..., User],
    embedder: FakeEmbedder,
) -> None:
    user = make_user()
    memory = add_memory(admin, user.id, FACT_TEXT)
    admin.execute("update public.memories set embedding = null where id = %s", (memory,))

    async with pool.connection() as conn:
        found = await recall.recall(conn, embedder, user.id, "which electronics shop")

    assert [r.text for r in found] == [FACT_TEXT]
