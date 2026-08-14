"""Forgetting, restoring, editing: the owner's control over what is remembered."""

from collections.abc import Callable
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import DictRow

from tests.conftest import User
from tests.memory_support import FakeEmbedder, says
from tests.memory_support_db import add_episode, add_memory
from worker.db import Pool
from worker.memory import manage, recall
from worker.memory.embeddings import vector_literal
from worker.memory.ingest import digest

pytestmark = pytest.mark.db

FACT = "Always buy electronics from Best Buy."


def count(admin: psycopg.Connection[DictRow], sql: str, *args: object) -> int:
    row = admin.execute(sql, args).fetchone()
    assert row is not None
    return int(row["n"])


def leaks(
    admin: psycopg.Connection[DictRow], user: User, memory_id: UUID, words: str
) -> dict[str, int]:
    """Every place a forgotten fact could still be found, with the number of hits in each.
    After erasing, all must be zero. The searches mirror what the app itself can do."""
    vector = vector_literal(FakeEmbedder._one(words))
    like = f"%{words}%"
    mine = "select count(*) n from public.memories where user_id = %s and "
    searches: dict[str, tuple[str, tuple[object, ...]]] = {
        "row": ("select count(*) n from public.memories where id = %s", (memory_id,)),
        "text": (mine + "canonical_text ilike %s", (user.id, like)),
        "keyword": (mine + "tsv @@ websearch_to_tsquery('english', %s)", (user.id, words)),
        "vector": (mine + "embedding <=> %s::extensions.vector < 0.5", (user.id, vector)),
        "sources": (
            "select count(*) n from public.memory_sources where memory_id = %s",
            (memory_id,),
        ),
        "summaries": (
            "select count(*) n from public.episodes where user_id = %s and summary ilike %s",
            (user.id, like),
        ),
        "links": (
            "select count(*) n from public.episode_memory_links where memory_id = %s",
            (memory_id,),
        ),
        "audit_text": (
            "select count(*) n from audit.events where target_id = %s and metadata::text ilike %s",
            (str(memory_id), like),
        ),
    }
    return {name: count(admin, sql, *args) for name, (sql, args) in searches.items()}


async def test_forgetting_hides_the_memory_at_once_and_it_can_be_restored(
    pool: Pool,
    admin: psycopg.Connection[DictRow],
    make_user: Callable[..., User],
    embedder: FakeEmbedder,
) -> None:
    user = make_user()
    memory = add_memory(admin, user.id, FACT)

    async with pool.connection() as conn:
        assert await manage.forget(conn, user.id, [memory], actor="voice") == 1
        assert await recall.recall(conn, embedder, user.id, "electronics") == []
        assert (await recall.profile_card(conn, user.id)).lines == []
        assert await manage.restore(conn, user.id, memory)
        assert [r.text for r in await recall.recall(conn, embedder, user.id, "electronics")] == [
            FACT
        ]


async def test_after_the_undo_window_the_memory_is_erased_everywhere(
    pool: Pool, admin: psycopg.Connection[DictRow], make_user: Callable[..., User]
) -> None:
    user = make_user()
    memory = add_memory(admin, user.id, FACT)
    _, [said] = says(admin, user.id, "always use Best Buy for electronics")
    admin.execute(
        "insert into public.memory_sources (memory_id, source_type, source_id, span) "
        "values (%s, 'message', %s, %s)",
        (memory, said.id, said.text),
    )
    episode = add_episode(admin, user.id, "Told me to always use Best Buy for electronics.")
    admin.execute("insert into public.episode_memory_links values (%s, %s)", (episode, memory))

    async with pool.connection() as conn:
        await manage.forget(conn, user.id, [memory], actor="dashboard")
        assert await manage.purge_expired(conn) == 0  # still inside the window
        admin.execute(
            "update public.memories set deleted_at = now() - interval '25 hours' where id = %s",
            (memory,),
        )
        assert await manage.purge_expired(conn) == 1

    assert set(leaks(admin, user, memory, "Best Buy").values()) == {0}


async def test_forgetting_immediately_skips_the_window(
    pool: Pool, admin: psycopg.Connection[DictRow], make_user: Callable[..., User]
) -> None:
    user = make_user()
    memory = add_memory(admin, user.id, FACT)

    async with pool.connection() as conn:
        await manage.forget(conn, user.id, [memory], actor="dashboard", immediately=True)

    assert set(leaks(admin, user, memory, "Best Buy").values()) == {0}


async def test_a_zero_hour_undo_window_means_immediate_erasure(
    pool: Pool, admin: psycopg.Connection[DictRow], make_user: Callable[..., User]
) -> None:
    user = make_user()
    admin.execute(
        "insert into public.user_settings (user_id, memory_undo_hours) values (%s, 0)", (user.id,)
    )
    memory = add_memory(admin, user.id, FACT)

    async with pool.connection() as conn:
        await manage.forget(conn, user.id, [memory], actor="voice")

    assert leaks(admin, user, memory, "Best Buy")["row"] == 0


async def test_forgetting_also_removes_older_wordings_and_the_summaries_that_held_the_fact(
    pool: Pool, admin: psycopg.Connection[DictRow], make_user: Callable[..., User]
) -> None:
    user = make_user()
    old = add_memory(admin, user.id, "Buy electronics from Future Shop.", status="superseded")
    new = add_memory(admin, user.id, FACT)
    admin.execute("update public.memories set superseded_by = %s where id = %s", (new, old))
    episode = add_episode(admin, user.id, "The owner said to buy electronics from Best Buy.")
    admin.execute("insert into public.episode_memory_links values (%s, %s)", (episode, new))
    unrelated = add_episode(admin, user.id, "Asked about flight prices.")

    async with pool.connection() as conn:
        assert await manage.forget(conn, user.id, [new], actor="voice", immediately=True) == 2

    assert set(leaks(admin, user, old, "Future Shop").values()) == {0}
    assert set(leaks(admin, user, new, "Best Buy").values()) == {0}
    assert count(admin, "select count(*) n from public.episodes where id = %s", unrelated) == 1


async def test_forgetting_can_take_the_source_messages_with_it(
    pool: Pool, admin: psycopg.Connection[DictRow], make_user: Callable[..., User]
) -> None:
    user = make_user()
    _, [said, kept] = says(
        admin, user.id, "always use Best Buy for electronics", "unrelated chatter"
    )
    memory = add_memory(admin, user.id, FACT)
    admin.execute(
        "insert into public.memory_sources (memory_id, source_type, source_id, span) "
        "values (%s, 'message', %s, 'x')",
        (memory, said.id),
    )

    async with pool.connection() as conn:
        await manage.forget(conn, user.id, [memory], actor="dashboard", with_sources=True)

    rows = admin.execute("select id from public.messages where id = any(%s)", ([said.id, kept.id],))
    assert [r["id"] for r in rows.fetchall()] == [kept.id]
    assert set(leaks(admin, user, memory, "Best Buy").values()) == {0}


async def test_what_is_kept_about_a_deletion_is_a_hash_not_the_text(
    pool: Pool, admin: psycopg.Connection[DictRow], make_user: Callable[..., User]
) -> None:
    user = make_user()
    memory = add_memory(admin, user.id, FACT)

    async with pool.connection() as conn:
        await manage.forget(conn, user.id, [memory], actor="voice")

    event = admin.execute(
        "select op, actor, detail_hash from public.memory_events where memory_id = %s", (memory,)
    ).fetchone()
    assert event == {"op": "delete", "actor": "voice", "detail_hash": digest(FACT)}
    audit = admin.execute(
        "select metadata from audit.events where target_id = %s", (str(memory),)
    ).fetchone()
    assert audit is not None and "Best Buy" not in str(audit["metadata"])


async def test_nobody_can_forget_another_users_memory(
    pool: Pool,
    admin: psycopg.Connection[DictRow],
    make_user: Callable[..., User],
    embedder: FakeEmbedder,
) -> None:
    owner, stranger = make_user(), make_user()
    memory = add_memory(admin, owner.id, FACT)

    async with pool.connection() as conn:
        assert (
            await manage.forget(conn, stranger.id, [memory], actor="voice", immediately=True) == 0
        )
        assert await manage.restore(conn, stranger.id, memory) is False
        assert len(await recall.recall(conn, embedder, owner.id, "electronics")) == 1


async def test_clearing_one_type_leaves_the_others(
    pool: Pool, admin: psycopg.Connection[DictRow], make_user: Callable[..., User]
) -> None:
    user = make_user()
    add_memory(admin, user.id, "Prefers window seats.", kind="preference")
    add_memory(admin, user.id, "Lives in Toronto.", kind="fact")
    add_memory(admin, user.id, "Lived in Ottawa.", kind="fact", status="superseded")

    async with pool.connection() as conn:
        assert await manage.clear(conn, user.id, kind="fact") == 2  # includes the superseded one
        states = admin.execute(
            "select type, status from public.memories where user_id = %s", (user.id,)
        ).fetchall()
        assert {(s["type"], s["status"]) for s in states} == {
            ("preference", "active"),
            ("fact", "deleted"),
        }
        assert await manage.clear(conn, user.id) == 1


async def test_confirming_a_habit_makes_it_active_and_refusing_erases_it(
    pool: Pool,
    admin: psycopg.Connection[DictRow],
    make_user: Callable[..., User],
    embedder: FakeEmbedder,
) -> None:
    user = make_user()
    keep = add_memory(admin, user.id, "Prefers Costco for tires.", status="pending_confirm")
    refuse = add_memory(admin, user.id, "Prefers Costco for TVs.", status="pending_confirm")

    async with pool.connection() as conn:
        assert await manage.settle(conn, user.id, keep, keep=True, actor="voice")
        assert await manage.settle(conn, user.id, refuse, keep=False, actor="voice")
        assert not await manage.settle(
            conn, user.id, keep, keep=True, actor="voice"
        )  # no longer pending
        found = await recall.recall(conn, embedder, user.id, "Costco")

    assert [r.text for r in found] == ["Prefers Costco for tires."]
    row = admin.execute("select origin from public.memories where id = %s", (keep,)).fetchone()
    assert row == {"origin": "user_confirmed"}
    assert count(admin, "select count(*) n from public.memories where id = %s", refuse) == 0


async def test_a_refusal_cannot_delete_an_active_memory(
    pool: Pool, admin: psycopg.Connection[DictRow], make_user: Callable[..., User]
) -> None:
    user = make_user()
    memory = add_memory(admin, user.id, FACT)

    async with pool.connection() as conn:
        assert not await manage.settle(conn, user.id, memory, keep=False, actor="voice")

    row = admin.execute("select status from public.memories where id = %s", (memory,)).fetchone()
    assert row == {"status": "active"}


async def test_an_edit_replaces_the_memory_with_the_owners_wording(
    pool: Pool,
    admin: psycopg.Connection[DictRow],
    make_user: Callable[..., User],
    embedder: FakeEmbedder,
) -> None:
    user = make_user()
    old = add_memory(admin, user.id, FACT)

    async with pool.connection() as conn:
        new = await manage.edit(conn, embedder, user.id, old, "Buy electronics from Costco.")
        found = await recall.recall(conn, embedder, user.id, "electronics")

    assert [r.text for r in found] == ["Buy electronics from Costco."]
    old_row = admin.execute(
        "select status, superseded_by from public.memories where id = %s", (old,)
    ).fetchone()
    assert old_row == {"status": "superseded", "superseded_by": new}
    new_row = admin.execute(
        "select origin, announced_at is not null as quiet from public.memories where id = %s",
        (new,),
    ).fetchone()
    assert new_row == {"origin": "user_confirmed", "quiet": True}  # the owner wrote it themselves
    source = admin.execute(
        "select source_type, span from public.memory_sources where memory_id = %s", (new,)
    ).fetchone()
    assert source == {"source_type": "dashboard_edit", "span": "Buy electronics from Costco."}


async def test_an_edit_cannot_smuggle_in_a_secret(
    pool: Pool,
    admin: psycopg.Connection[DictRow],
    make_user: Callable[..., User],
    embedder: FakeEmbedder,
) -> None:
    user = make_user()
    old = add_memory(admin, user.id, FACT)

    async with pool.connection() as conn:
        with pytest.raises(manage.SensitiveMemoryError):
            await manage.edit(conn, embedder, user.id, old, "My bank password is hunter2")
        with pytest.raises(manage.UnknownMemoryError):
            await manage.edit(conn, embedder, make_user().id, old, "Buy from Costco.")


async def test_clearing_everything_also_wipes_summaries_and_remembered_routes(
    pool: Pool, admin: psycopg.Connection[DictRow], make_user: Callable[..., User]
) -> None:
    user, other = make_user(), make_user()
    add_memory(admin, user.id, FACT)
    add_episode(admin, user.id, "Compared TV prices.", kind="task")
    for owner in (user, other):
        admin.execute(
            "insert into public.procedures (user_id, domain, task_type, steps) "
            "values (%s, 'shop.example', 'read', '[]')",
            (owner.id,),
        )
    add_episode(admin, other.id, "Booked a flight.")

    async with pool.connection() as conn:
        await manage.clear(conn, user.id, kind="rule")
        episodes_after_partial = count(
            admin, "select count(*) n from public.episodes where user_id = %s", user.id
        )
        await manage.clear(conn, user.id)

    assert episodes_after_partial == 1  # clearing one type leaves history alone
    for table in ("episodes", "procedures"):
        sql = f"select count(*) n from public.{table} where user_id = %s"
        assert count(admin, sql, user.id) == 0
        assert count(admin, sql, other.id) == 1
