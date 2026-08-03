"""Reading memory back: the short card loaded at the start of a call, and searching on demand."""

import logging
import re
from dataclasses import dataclass
from uuid import UUID

from openai import OpenAIError

from worker.db import Conn
from worker.memory.embeddings import Embedder, vector_literal

log = logging.getLogger(__name__)

# Hybrid score = 0.5 meaning + 0.3 keywords + 0.2 recency (half-life about 60 days).
MIN_COSINE = 0.25  # below this a memory counts as unrelated unless a keyword also matches
RELATIVE_COSINE = 0.75  # and so does anything much further from the query than the best match
KEYWORD_FLOOR = 0.25  # a keyword hit rescues a text only if it is at least this close in meaning
MAX_CHARS = 2400  # about 600 tokens
CARD_LIMIT = 20
CONFIRM_AFTER = 3  # times a habit is seen, in at least two conversations, before asking


@dataclass(frozen=True)
class Recalled:
    id: UUID
    kind: str  # rule | preference | fact | call | task | habit
    text: str
    score: float = 0


@dataclass(frozen=True)
class Card:
    lines: list[str]
    to_confirm: list[Recalled]  # habits seen often enough to ask about, once
    changes: list[str]  # "was X, now Y" for statements that replaced an older one


def any_word(query: str) -> str:
    """A tsquery matching any word of the query. websearch_to_tsquery would require all of
    them, so "compare electronics prices" would miss "buy electronics at Best Buy"."""
    return " | ".join(dict.fromkeys(re.findall(r"[a-z0-9]+", query.lower())[:20]))


async def enabled(conn: Conn, user_id: UUID) -> bool:
    row = await (
        await conn.execute(
            "select memory_enabled from public.user_settings where user_id = %s", (user_id,)
        )
    ).fetchone()
    return row is None or row["memory_enabled"]


async def profile_card(conn: Conn, user_id: UUID) -> Card:
    """Standing rules and preferences, plus anything the owner should hear about once.
    Marks what it reports as announced, so it is said once."""
    rules = await (
        await conn.execute(
            "select canonical_text from public.memories where user_id = %s and status = 'active' "
            "and type in ('rule', 'preference') "
            "order by last_used_at desc nulls last, created_at desc limit %s",
            (user_id, CARD_LIMIT),
        )
    ).fetchall()
    ask = await (
        await conn.execute(
            "select m.id, m.canonical_text from public.memories m "
            "where m.user_id = %s and m.status = 'pending_confirm' and m.announced_at is null "
            "and (select count(*) from public.memory_sources s where s.memory_id = m.id) >= %s "
            "and (select count(distinct g.conversation_id) from public.memory_sources s "
            "     join public.messages g on g.id = s.source_id "
            "     where s.memory_id = m.id and s.source_type = 'message') >= 2 "
            "order by m.created_at limit 1",
            (user_id, CONFIRM_AFTER),
        )
    ).fetchall()
    changed = await (
        await conn.execute(
            "select distinct on (new.id) new.id, new.canonical_text as now, "
            "old.canonical_text as was from public.memories new "
            "join public.memories old on old.superseded_by = new.id "
            "where new.user_id = %s and new.status = 'active' and new.announced_at is null "
            "order by new.id limit 3",
            (user_id,),
        )
    ).fetchall()
    announce = [r["id"] for r in ask] + [r["id"] for r in changed]
    if announce:
        await conn.execute(
            "update public.memories set announced_at = now() where id = any(%s)", (announce,)
        )
    return Card(
        lines=[r["canonical_text"] for r in rules],
        to_confirm=[Recalled(r["id"], "habit", r["canonical_text"]) for r in ask],
        changes=[f"was: {r['was']} / now: {r['now']}" for r in changed],
    )


_SEARCH = """
with q as (select %(vec)s::extensions.vector as v, to_tsquery('english', %(terms)s) as t),
scored as (
  select {columns}, x.tsv @@ q.t as keyword_hit, x.embedding is null as unembedded, x.created_at,
         coalesce(1 - (x.embedding <=> q.v), 0) as cosine,
         ts_rank_cd(x.tsv, q.t, 32) as keyword_rank
  from public.{table} x, q
  where x.user_id = %(user)s and {live}
)
select *, 0.5 * cosine + 0.3 * keyword_rank
       + 0.2 * exp(-extract(epoch from now() - created_at) / 86400 / 60) as score
from scored
where cosine >= greatest(%(min)s, %(relative)s * (select max(cosine) from scored))
   or (keyword_hit and (unembedded or cosine >= %(keyword_floor)s))
order by score desc limit %(limit)s
"""


async def recall(
    conn: Conn,
    embedder: Embedder,
    user_id: UUID,
    query: str,
    *,
    types: tuple[str, ...] = ("fact", "preference", "rule"),
    with_episodes: bool = True,
    limit: int = 8,
) -> list[Recalled]:
    """What the owner has said or done that bears on the query, best first, within a size
    budget. Only active memories: superseded and deleted ones never come back."""
    if not await enabled(conn, user_id):
        return []
    try:
        [vector] = await embedder.embed([query])
    except OpenAIError as unavailable:
        log.warning(
            "embeddings unavailable; keyword search only", extra={"error": str(unavailable)}
        )
        vector = None
    params = {
        "vec": vector_literal(vector),
        "terms": any_word(query),
        "user": user_id,
        "min": MIN_COSINE,
        "relative": RELATIVE_COSINE,
        "keyword_floor": KEYWORD_FLOOR if vector else 0,
        "limit": limit,
    }

    rows = await (
        await conn.execute(
            _SEARCH.format(
                columns="x.id, x.type as kind, x.canonical_text as text",
                table="memories",
                live="x.status = 'active' and x.type = any(%(types)s)",
            ),
            {**params, "types": list(types)},
        )
    ).fetchall()
    found = [Recalled(r["id"], r["kind"], r["text"], r["score"]) for r in rows]
    if with_episodes:
        episodes = await (
            await conn.execute(
                _SEARCH.format(
                    columns="x.id, x.kind, x.summary as text", table="episodes", live="true"
                ),
                params,
            )
        ).fetchall()
        found += [Recalled(r["id"], r["kind"], r["text"], r["score"]) for r in episodes]
        found.sort(key=lambda r: r.score, reverse=True)

    found = found[:limit]
    kept: list[Recalled] = []
    used = 0
    for item in found:
        if used + len(item.text) > MAX_CHARS:
            break
        kept.append(item)
        used += len(item.text)
    ids = [r.id for r in kept if r.kind in {"fact", "preference", "rule"}]
    if ids:
        await conn.execute(
            "update public.memories set last_used_at = now() where id = any(%s)", (ids,)
        )
    return kept
