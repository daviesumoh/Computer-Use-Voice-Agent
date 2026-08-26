"""How well memory works with the real models: embedding search and extraction.

Costs a few cents. Run with `uv run pytest -m live -s` and a WORKER_OPENAI_API_KEY in .env.
Thresholds are the plan's: precision >= 0.7 and recall >= 0.9 on a labelled set, and a
statement the owner replaces must be superseded."""

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from uuid import UUID

import psycopg
import pytest
from openai import AsyncOpenAI
from psycopg.rows import DictRow

from tests.conftest import User
from tests.memory_support import says
from worker.db import Pool
from worker.memory import recall
from worker.memory.embeddings import OpenAIEmbedder
from worker.memory.ingest import ingest
from worker.memory.models import OpenAIMemoryModel
from worker.settings import Settings

pytestmark = [pytest.mark.live, pytest.mark.db]

CORPUS = [
    "Always buy electronics from Best Buy.",
    "Never order from Amazon marketplace sellers.",
    "Prefers window seats on flights.",
    "Always book the cheapest economy fare, never business class.",
    "Lives in Toronto.",
    "Partner's name is Sam.",
    "Wants delivery to the office on weekdays, not home.",
    "Don't text before nine in the morning.",
    "Prefers organic groceries from Loblaws.",
    "Gets a Costco membership discount on tires.",
    "Hates phone calls on Sundays.",
    "Budget for TVs is under 900 dollars.",
    "Always compare at least three stores before recommending a laptop.",
    "Prefers Air Canada for flights to Vancouver.",
    "Works Monday to Thursday from home.",
    "Likes dark mode in every app.",
    "Dentist appointments are on Tuesday mornings.",
    "Prefers hotels within walking distance of the venue.",
    "Never share the home address with sellers.",
    "Prefers reading summaries as bullet points.",
]
# query -> corpus indices that bear on it
QUERIES: dict[str, set[int]] = {
    "where should I buy a new television": {0, 11},
    "book me a flight to Vancouver": {2, 3, 13},
    "find a laptop": {12, 0},
    "what time can you text me": {7},
    "where does my order get delivered": {6, 18},
    "who is my partner": {5},
    "buy tires": {9},
    "groceries for the week": {8},
    "should I call on Sunday": {10},
    "which city do I live in": {4},
    "book a hotel for the conference": {17},
    "when is my dentist visit": {16},
    "which days do I work from home": {14},
    "how should results be formatted": {19},
    "is it okay to order from a marketplace seller": {1},
}
UNRELATED = ["what is the capital of Australia", "explain how photosynthesis works"]


@dataclass
class Live:
    embedder: OpenAIEmbedder
    model: OpenAIMemoryModel


@pytest.fixture
async def live(pool: Pool) -> AsyncIterator[Live]:
    settings = Settings(_env_file=".env")  # type: ignore[call-arg]
    if settings.openai_api_key is None:
        pytest.skip("WORKER_OPENAI_API_KEY is not set")
    client = AsyncOpenAI(api_key=settings.openai_api_key.get_secret_value())
    yield Live(OpenAIEmbedder(client), OpenAIMemoryModel(client, settings.memory_model, pool))


async def test_search_finds_what_bears_on_a_question_and_stays_quiet_otherwise(
    live: Live, pool: Pool, admin: psycopg.Connection[DictRow], make_user: Callable[..., User]
) -> None:
    user = make_user()
    vectors = await live.embedder.embed(CORPUS)
    ids: list[UUID] = []
    for text, vector in zip(CORPUS, vectors, strict=True):
        row = admin.execute(
            "insert into public.memories (user_id, type, canonical_text, origin, cue, embedding) "
            "values (%s, 'rule', %s, 'user_stated', 'explicit_rule', %s::extensions.vector) "
            "returning id",
            (user.id, text, "[" + ",".join(map(str, vector)) + "]"),
        ).fetchone()
        assert row is not None
        ids.append(row["id"])

    hits = returned = relevant_found = relevant_total = 0
    async with pool.connection() as conn:
        for query, wanted in QUERIES.items():
            found = await recall.recall(
                conn, live.embedder, user.id, query, with_episodes=False, limit=5
            )
            got = {ids.index(r.id) for r in found}
            hits += len(got & wanted)
            returned += len(got)
            relevant_found += len(got & wanted)
            relevant_total += len(wanted)
            missed = [CORPUS[i] for i in wanted - got]
            print(f"{query!r}: returned {len(got)}, relevant {len(got & wanted)}/{len(wanted)}"
                  + (f", missed {missed}" if missed else ""))  # fmt: skip
        quiet = [
            await recall.recall(conn, live.embedder, user.id, q, with_episodes=False)
            for q in UNRELATED
        ]

    precision, recall_rate = hits / returned, relevant_found / relevant_total
    print(
        f"precision {precision:.2f}, recall {recall_rate:.2f}, "
        f"unrelated returned {[len(q) for q in quiet]}"
    )
    assert precision >= 0.7
    assert recall_rate >= 0.9
    assert all(len(q) == 0 for q in quiet)


# What the owner said, and how many memories should come out (after the gates).
TRANSCRIPTS = [
    ("From now on always use Best Buy for electronics.", 1),
    ("Find me a 65 inch TV under 800 dollars.", 0),
    ("I live in Toronto.", 1),
    ("Never text me before nine in the morning.", 1),
    ("Can you check the weather in Ottawa tomorrow?", 0),
    ("Hello, how are you today?", 0),
    ("My card number is 4111 1111 1111 1111, use it for everything.", 0),
    ("My password for the shop is hunter2.", 0),
    ("I'm allergic to peanuts so never order anything with nuts.", 0),
    ("Always send the summary as bullet points.", 1),
]


async def test_extraction_keeps_lasting_things_and_drops_the_rest(
    live: Live, pool: Pool, admin: psycopg.Connection[DictRow], make_user: Callable[..., User]
) -> None:
    right = 0
    for line, expected in TRANSCRIPTS:
        user = make_user()
        _, said = says(admin, user.id, line)
        candidates = await live.model.extract(user.id, said)
        async with pool.connection() as conn:
            outcome = await ingest(
                conn, embedder=live.embedder, model=live.model, user_id=user.id,
                candidates=candidates, said=said,
            )  # fmt: skip
        stored = len(outcome.created)
        right += stored == expected
        print(f"{line!r}: stored {stored} (expected {expected}), rejected {dict(outcome.rejected)}")
    print(f"{right}/{len(TRANSCRIPTS)} as expected")
    assert right >= 9


async def test_a_replaced_statement_is_superseded_not_duplicated(
    live: Live, pool: Pool, admin: psycopg.Connection[DictRow], make_user: Callable[..., User]
) -> None:
    user = make_user()
    for line in ("From now on always buy electronics from Best Buy.",
                 "Actually, from now on buy electronics from Costco instead."):  # fmt: skip
        _, said = says(admin, user.id, line)
        candidates = await live.model.extract(user.id, said)
        async with pool.connection() as conn:
            await ingest(
                conn, embedder=live.embedder, model=live.model, user_id=user.id,
                candidates=candidates, said=said,
            )  # fmt: skip

    rows = admin.execute(
        "select canonical_text, status from public.memories where user_id = %s order by created_at",
        (user.id,),
    ).fetchall()
    print(rows)
    assert [r["status"] for r in rows] == ["superseded", "active"]
    assert "Costco" in rows[1]["canonical_text"]
