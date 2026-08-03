"""Storing what the owner said, against real Postgres."""

from collections.abc import Callable, Sequence

import psycopg
import pytest
from psycopg.rows import DictRow

from tests.conftest import User
from tests.memory_support import FakeEmbedder, ScriptedMemoryModel, rule, says
from worker.db import Pool
from worker.memory import recall
from worker.memory.ingest import Outcome, ingest
from worker.memory.models import Candidate, Judgement, Known

pytestmark = pytest.mark.db

BEST_BUY = rule("Always buy electronics from Best Buy.", "always use Best Buy for electronics")


async def store(
    pool: Pool,
    embedder: FakeEmbedder,
    model: ScriptedMemoryModel,
    user: User,
    candidates: Sequence[Candidate],
    said: Sequence[object],
) -> Outcome:
    async with pool.connection() as conn:
        return await ingest(
            conn,
            embedder=embedder,
            model=model,
            user_id=user.id,
            candidates=candidates,
            said=said,  # type: ignore[arg-type]
        )


def memories(admin: psycopg.Connection[DictRow], user: User) -> list[dict[str, object]]:
    return admin.execute(
        "select canonical_text, status, origin, cue, scope, superseded_by, "
        "embedding is not null as embedded "
        "from public.memories where user_id = %s order by created_at",
        (user.id,),
    ).fetchall()


async def test_a_stated_rule_becomes_an_active_memory_with_the_owners_words_as_source(
    pool: Pool, admin: psycopg.Connection[DictRow], make_user: Callable[..., User],
    embedder: FakeEmbedder, scripted: ScriptedMemoryModel,
) -> None:  # fmt: skip
    user = make_user()
    _, said = says(admin, user.id, "From now on always use Best Buy for electronics")
    candidate = rule(BEST_BUY.text, BEST_BUY.span, applies_to="electronics")

    outcome = await store(pool, embedder, scripted, user, [candidate], said)

    [stored] = memories(admin, user)
    assert (stored["status"], stored["origin"], stored["cue"]) == (
        "active",
        "user_stated",
        "explicit_rule",
    )
    assert stored["scope"] == {"applies_to": "electronics"} and stored["embedded"]
    source = admin.execute(
        "select source_id, span from public.memory_sources where memory_id = %s",
        (outcome.created[0],),
    ).fetchone()
    assert source == {"source_id": said[0].id, "span": BEST_BUY.span}


async def test_a_fabricated_statement_stores_nothing(
    pool: Pool, admin: psycopg.Connection[DictRow], make_user: Callable[..., User],
    embedder: FakeEmbedder, scripted: ScriptedMemoryModel,
) -> None:  # fmt: skip
    user = make_user()
    _, said = says(admin, user.id, "Find me a TV")
    fabricated = rule("Always buy from Evil Shop.", "always buy from evil shop")

    outcome = await store(pool, embedder, scripted, user, [fabricated], said)

    assert memories(admin, user) == [] and outcome.rejected == {"span_not_from_owner": 1}


async def test_saying_it_again_confirms_the_memory_instead_of_duplicating_it(
    pool: Pool, admin: psycopg.Connection[DictRow], make_user: Callable[..., User],
    embedder: FakeEmbedder, scripted: ScriptedMemoryModel,
) -> None:  # fmt: skip
    user = make_user()
    _, first = says(admin, user.id, "always use Best Buy for electronics")
    await store(pool, embedder, scripted, user, [BEST_BUY], first)
    admin.execute(
        "update public.memories set last_confirmed_at = now() - interval '30 days' "
        "where user_id = %s",
        (user.id,),
    )
    _, again = says(admin, user.id, "I said always use Best Buy for electronics")

    outcome = await store(pool, embedder, scripted, user, [BEST_BUY], again)

    assert len(memories(admin, user)) == 1 and len(outcome.refreshed) == 1
    row = admin.execute(
        "select last_confirmed_at > now() - interval '1 minute' as fresh from public.memories "
        "where user_id = %s",
        (user.id,),
    ).fetchone()
    assert row == {"fresh": True}
    sources = admin.execute(
        "select count(*) as n from public.memory_sources s "
        "join public.memories m on m.id = s.memory_id "
        "where m.user_id = %s",
        (user.id,),
    ).fetchone()
    assert sources == {"n": 2}
    assert scripted.related == []  # an exact repeat needs no model call


async def test_a_contradiction_replaces_the_old_memory_and_only_the_new_one_is_recalled(
    pool: Pool, admin: psycopg.Connection[DictRow], make_user: Callable[..., User],
    embedder: FakeEmbedder, scripted: ScriptedMemoryModel,
) -> None:  # fmt: skip
    user = make_user()
    _, first = says(admin, user.id, "always use Best Buy for electronics")
    await store(pool, embedder, scripted, user, [BEST_BUY], first)
    scripted.judge = lambda new, known: [Judgement(id=k.id, relation="contradicts") for k in known]
    newer = rule("Always buy electronics from Costco.", "actually use Costco for electronics")
    _, second = says(admin, user.id, "actually use Costco for electronics")

    outcome = await store(pool, embedder, scripted, user, [newer], second)

    old, new = memories(admin, user)
    assert (old["status"], new["status"]) == ("superseded", "active") and len(
        outcome.superseded
    ) == 1
    assert old["superseded_by"] is not None
    async with pool.connection() as conn:
        found = await recall.recall(conn, embedder, user.id, "electronics store")
    assert [r.text for r in found] == [newer.text]


async def test_a_narrower_case_refines_and_keeps_the_old_memory(
    pool: Pool, admin: psycopg.Connection[DictRow], make_user: Callable[..., User],
    embedder: FakeEmbedder, scripted: ScriptedMemoryModel,
) -> None:  # fmt: skip
    user = make_user()
    _, first = says(admin, user.id, "always use Best Buy for electronics")
    await store(pool, embedder, scripted, user, [BEST_BUY], first)
    scripted.judge = lambda new, known: [Judgement(id=k.id, relation="refines") for k in known]
    narrower = rule("Buy TVs from Costco.", "but get TVs from Costco")
    _, second = says(admin, user.id, "but get TVs from Costco")

    await store(pool, embedder, scripted, user, [narrower], second)

    assert [m["status"] for m in memories(admin, user)] == ["active", "active"]


async def test_the_model_is_not_shown_other_users_memories(
    pool: Pool, admin: psycopg.Connection[DictRow], make_user: Callable[..., User],
    embedder: FakeEmbedder, scripted: ScriptedMemoryModel,
) -> None:  # fmt: skip
    owner, other = make_user(), make_user()
    _, theirs = says(admin, other.id, "always use Best Buy for electronics")
    await store(pool, embedder, scripted, other, [BEST_BUY], theirs)
    _, mine = says(admin, owner.id, "always shop at Costco for electronics")

    await store(
        pool, embedder, scripted, owner, [rule("Shop at Costco.", "always shop at Costco")], mine
    )

    assert scripted.related == []  # nothing to compare with: the only other memory is not theirs


async def test_a_habit_waits_for_confirmation_and_never_overrides_a_stated_rule(
    pool: Pool, admin: psycopg.Connection[DictRow], make_user: Callable[..., User],
    embedder: FakeEmbedder, scripted: ScriptedMemoryModel,
) -> None:  # fmt: skip
    user = make_user()
    _, first = says(admin, user.id, "always use Best Buy for electronics")
    await store(pool, embedder, scripted, user, [BEST_BUY], first)
    scripted.judge = lambda new, known: [Judgement(id=k.id, relation="contradicts") for k in known]
    habit = rule("Prefers Costco.", "get it from Costco", cue="repeated_choice")
    _, second = says(admin, user.id, "get it from Costco")

    outcome = await store(pool, embedder, scripted, user, [habit], second)

    assert [m["status"] for m in memories(admin, user)] == ["active"]
    assert outcome.rejected == {"contradicts_stated": 1}


async def test_a_new_habit_is_stored_as_pending_and_inferred(
    pool: Pool, admin: psycopg.Connection[DictRow], make_user: Callable[..., User],
    embedder: FakeEmbedder, scripted: ScriptedMemoryModel,
) -> None:  # fmt: skip
    user = make_user()
    _, said = says(admin, user.id, "get it from Costco")
    habit = rule("Prefers Costco.", "get it from Costco", cue="repeated_choice")

    await store(pool, embedder, scripted, user, [habit], said)

    [stored] = memories(admin, user)
    assert (stored["status"], stored["origin"]) == ("pending_confirm", "inferred")
    async with pool.connection() as conn:
        assert await recall.recall(conn, embedder, user.id, "Costco") == []


async def test_a_habit_is_asked_about_once_after_three_sightings_in_two_calls(
    pool: Pool, admin: psycopg.Connection[DictRow], make_user: Callable[..., User],
    embedder: FakeEmbedder, scripted: ScriptedMemoryModel,
) -> None:  # fmt: skip
    user = make_user()
    habit = rule("Prefers Costco.", "get it from Costco", cue="repeated_choice")
    _, call_one = says(admin, user.id, "get it from Costco", "yes get it from Costco")
    await store(pool, embedder, scripted, user, [habit], call_one[:1])
    await store(pool, embedder, scripted, user, [habit], call_one[1:])
    async with pool.connection() as conn:
        assert (await recall.profile_card(conn, user.id)).to_confirm == []  # three, but one call

    _, call_two = says(admin, user.id, "get it from Costco again")
    await store(pool, embedder, scripted, user, [habit], call_two)

    async with pool.connection() as conn:
        card = await recall.profile_card(conn, user.id)
        again = await recall.profile_card(conn, user.id)
    assert [q.text for q in card.to_confirm] == ["Prefers Costco."]
    assert again.to_confirm == []


async def test_without_embeddings_a_memory_is_still_stored_and_found_by_keyword(
    pool: Pool, admin: psycopg.Connection[DictRow], make_user: Callable[..., User],
    embedder: FakeEmbedder, scripted: ScriptedMemoryModel,
) -> None:  # fmt: skip
    user = make_user()
    embedder.down = True
    _, said = says(admin, user.id, "always use Best Buy for electronics")

    await store(pool, embedder, scripted, user, [BEST_BUY], said)

    [stored] = memories(admin, user)
    assert stored["embedded"] is False
    async with pool.connection() as conn:
        found = await recall.recall(conn, embedder, user.id, "electronics")
    assert [r.text for r in found] == [BEST_BUY.text]


async def test_judgements_about_unknown_ids_are_ignored(
    pool: Pool, admin: psycopg.Connection[DictRow], make_user: Callable[..., User],
    embedder: FakeEmbedder, scripted: ScriptedMemoryModel,
) -> None:  # fmt: skip
    """A model that invents an id must not be able to supersede anything."""
    user, other = make_user(), make_user()
    _, theirs = says(admin, other.id, "always use Best Buy for electronics")
    await store(pool, embedder, scripted, other, [BEST_BUY], theirs)
    foreign = admin.execute(
        "select id from public.memories where user_id = %s", (other.id,)
    ).fetchone()
    assert foreign is not None
    _, said = says(admin, user.id, "I like Costco for tires", "always use Costco for tires")
    await store(
        pool, embedder, scripted, user, [rule("Likes Costco.", "I like Costco for tires")], said[:1]
    )
    scripted.judge = lambda new, known: [Judgement(id=foreign["id"], relation="contradicts")]

    await store(
        pool,
        embedder,
        scripted,
        user,
        [rule("Costco for tires.", "always use Costco for tires")],
        said[1:],
    )

    assert admin.execute(
        "select status from public.memories where id = %s", (foreign["id"],)
    ).fetchone() == {"status": "active"}


def test_known_is_what_the_model_sees() -> None:
    assert set(Known.model_fields) == {"id", "text"}  # no user ids, no origins: nothing to leak
