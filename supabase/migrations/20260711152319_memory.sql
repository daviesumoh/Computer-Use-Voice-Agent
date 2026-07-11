-- Long-term memory: facts, preferences and rules the owner stated themselves, call and
-- task summaries, and per-site procedures. Memories are written only by the worker.

create extension if not exists vector with schema extensions;
-- The worker's queries use pgvector's types and operators.
grant usage on schema extensions to agent_worker;
alter role agent_worker set search_path = public, extensions;

-- How long a deleted memory stays recoverable before it is erased for good (0 = at once).
alter table public.user_settings
  add column memory_undo_hours integer not null default 24 check (memory_undo_hours >= 0);

create table public.memories (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.profiles (id) on delete cascade,
  type text not null check (type in ('fact', 'preference', 'rule')),
  scope jsonb not null default '{}'::jsonb,   -- when it applies, e.g. {"category": "electronics"}
  canonical_text text not null,               -- one sentence; what is embedded and shown
  status text not null default 'active'
    check (status in ('active', 'superseded', 'pending_confirm', 'deleted')),
  origin text not null check (origin in ('user_stated', 'user_confirmed', 'inferred')),
  cue text not null check (cue in ('explicit_rule', 'stated_fact', 'repeated_choice')),
  confidence real not null default 1 check (confidence between 0 and 1),
  valid_from timestamptz not null default now(),
  valid_to timestamptz,
  superseded_by uuid references public.memories (id) on delete set null,
  last_confirmed_at timestamptz,
  last_used_at timestamptz,
  announced_at timestamptz,                   -- when the owner was last told: asked to confirm a
                                              -- pending memory, or informed that it replaced one
  deleted_at timestamptz,                     -- start of the undo window
  embedding extensions.vector(1536),          -- null if the embedding service was unavailable
  tsv tsvector generated always as (to_tsvector('english', canonical_text)) stored,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index memories_user_type_status on public.memories (user_id, type, status);
create index memories_embedding on public.memories using hnsw (embedding extensions.vector_cosine_ops);
create index memories_tsv on public.memories using gin (tsv);
create index memories_deleted on public.memories (deleted_at) where status = 'deleted';

-- Where each memory came from: the owner's own words. Web content is never a source.
create table public.memory_sources (
  memory_id uuid not null references public.memories (id) on delete cascade,
  source_type text not null check (source_type in ('message', 'instruction', 'dashboard_edit')),
  source_id uuid not null,
  span text not null,                         -- the owner's words, verbatim
  created_at timestamptz not null default now(),
  primary key (memory_id, source_type, source_id)
);
create index memory_sources_source on public.memory_sources (source_id);

-- What happened in a call or task, in a sentence or two.
create table public.episodes (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.profiles (id) on delete cascade,
  kind text not null check (kind in ('call', 'task')),
  ref_id uuid not null,                       -- the conversation or task
  summary text not null,
  outcome text,
  embedding extensions.vector(1536),
  tsv tsvector generated always as (to_tsvector('english', summary)) stored,
  created_at timestamptz not null default now(),
  unique (kind, ref_id)
);
create index episodes_user_created on public.episodes (user_id, created_at desc, id);
create index episodes_embedding on public.episodes using hnsw (embedding extensions.vector_cosine_ops);
create index episodes_tsv on public.episodes using gin (tsv);

-- Which episodes were summarised with a memory in view, so forgetting the memory reaches them.
create table public.episode_memory_links (
  episode_id uuid not null references public.episodes (id) on delete cascade,
  memory_id uuid not null references public.memories (id) on delete cascade,
  primary key (episode_id, memory_id)
);
create index episode_memory_links_memory on public.episode_memory_links (memory_id);

-- What worked on a site last time, for the browser agent to start from.
create table public.procedures (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.profiles (id) on delete cascade,
  domain text not null check (domain = lower(domain)),
  task_type text not null,
  steps jsonb not null,
  success_count integer not null default 0,
  fail_count integer not null default 0,
  stale boolean not null default false,       -- a later run could not follow the steps
  last_success_at timestamptz,
  unique (user_id, domain, task_type)
);

-- Every change to a memory. No foreign keys and no text: it must outlive the memory it
-- describes, and for a deletion it keeps only a hash.
create table public.memory_events (
  id bigint generated always as identity primary key,
  user_id uuid not null,
  memory_id uuid not null,
  op text not null check (op in ('create', 'supersede', 'edit', 'confirm', 'delete', 'restore',
                                 'purge')),
  actor text not null check (actor in ('voice', 'dashboard', 'worker')),
  detail_hash text,
  ts timestamptz not null default now()
);
create index memory_events_user_ts on public.memory_events (user_id, ts desc);

-- privileges -------------------------------------------------------------------------------
revoke all on public.memories, public.memory_sources, public.episodes,
  public.episode_memory_links, public.procedures, public.memory_events
  from anon, authenticated;
-- The dashboard never receives embeddings or the search vector.
grant select (id, user_id, type, scope, canonical_text, status, origin, cue, confidence,
              valid_from, valid_to, superseded_by, last_confirmed_at, last_used_at, announced_at,
              deleted_at, created_at, updated_at)
  on public.memories to authenticated;
grant select (id, user_id, kind, ref_id, summary, outcome, created_at)
  on public.episodes to authenticated;
grant select on public.memory_sources, public.procedures, public.memory_events to authenticated;
grant select, insert, update, delete on public.memories, public.memory_sources, public.episodes,
  public.episode_memory_links, public.procedures to agent_worker;
grant select, insert on public.memory_events to agent_worker;
-- Forgetting a memory can include the messages it was learned from.
grant delete on public.messages to agent_worker;

do $$
declare
  t text;
begin
  foreach t in array array['memories', 'memory_sources', 'episodes', 'episode_memory_links',
                           'procedures', 'memory_events']
  loop
    execute format('alter table public.%I enable row level security', t);
    execute format(
      'create policy worker_all on public.%I for all to agent_worker using (true) with check (true)', t);
  end loop;
end;
$$;

-- memory_sources and episode_memory_links carry no user_id; the owner reads them through the
-- memory or episode they belong to.
create policy owner_read on public.memory_sources for select to authenticated
  using (exists (select 1 from public.memories m
                 where m.id = memory_id and m.user_id = (select auth.uid())));
create policy require_mfa on public.memory_sources as restrictive to authenticated
  using (((select auth.jwt()) ->> 'aal') = 'aal2');
revoke all on public.episode_memory_links from authenticated;

do $$
declare
  t text;
begin
  foreach t in array array['memories', 'episodes', 'procedures', 'memory_events']
  loop
    execute format(
      'create policy owner_read on public.%I for select to authenticated '
      'using (user_id = (select auth.uid()))', t);
    execute format(
      'create policy require_mfa on public.%I as restrictive to authenticated '
      'using (((select auth.jwt()) ->> ''aal'') = ''aal2'')', t);
  end loop;
end;
$$;
