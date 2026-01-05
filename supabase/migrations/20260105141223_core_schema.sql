-- Core schema: identity, conversations, tasks, task events, usage and audit.
--
-- Access model
--   * The dashboard reads as the `authenticated` role, scoped to its own rows by RLS,
--     and only with an MFA-verified session (aal2). It never writes these tables
--     directly; commands go through the worker API.
--   * The worker connects as `agent_worker` (a login password is set outside migrations).
--   * `audit.events` is append-only for every role, including the table owner.

create schema if not exists audit;

create role agent_worker nologin;
grant agent_worker to postgres;  -- lets the admin role set its login and assume it in tests
grant usage on schema public, audit to agent_worker;

-- identity -------------------------------------------------------------------

create table public.profiles (
  id uuid primary key references auth.users (id) on delete cascade,
  display_name text not null default '',
  timezone text not null default 'America/Toronto',
  created_at timestamptz not null default now()
);

create table public.phone_numbers (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.profiles (id) on delete cascade,
  e164 text not null unique check (e164 ~ '^\+[1-9][0-9]{6,14}$'),
  verified_at timestamptz,
  is_primary boolean not null default false,
  sms_opted_out boolean not null default false,
  created_at timestamptz not null default now()
);
create unique index phone_numbers_one_primary on public.phone_numbers (user_id) where is_primary;

create table public.user_settings (
  user_id uuid primary key references public.profiles (id) on delete cascade,
  memory_enabled boolean not null default true,
  monthly_cost_cap_cents integer not null default 15000 check (monthly_cost_cap_cents >= 0),
  default_task_budget_cents integer not null default 300 check (default_task_budget_cents >= 0),
  emergency_stop boolean not null default false,
  updated_at timestamptz not null default now()
);

-- conversations & messages ---------------------------------------------------

create table public.conversations (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.profiles (id) on delete cascade,
  channel text not null check (channel in ('voice', 'sms', 'dashboard')),
  provider_call_sid text unique,
  started_at timestamptz not null default now(),
  ended_at timestamptz,
  end_reason text,
  state jsonb not null default '{}'::jsonb,
  summary text
);
create index conversations_user_started on public.conversations (user_id, started_at desc, id);

create table public.messages (
  id uuid primary key default gen_random_uuid(),
  conversation_id uuid not null references public.conversations (id) on delete cascade,
  user_id uuid not null references public.profiles (id) on delete cascade,
  direction text not null check (direction in ('inbound', 'outbound')),
  channel text not null check (channel in ('voice', 'sms', 'dashboard')),
  provider_sid text unique,           -- Twilio MessageSid; makes webhook retries idempotent
  body_redacted text not null,
  asr_confidence real check (asr_confidence between 0 and 1),
  turn_index integer,
  provider_ts timestamptz,
  received_at timestamptz not null default now(),
  delivery_status text
);
create index messages_conversation_received on public.messages (conversation_id, received_at, id);

-- tasks ----------------------------------------------------------------------

create table public.tasks (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.profiles (id) on delete cascade,
  short_code text not null,
  title text not null,
  spec jsonb not null,
  spec_version integer not null default 1,
  status text not null default 'draft' check (status in (
    'draft', 'queued', 'running', 'paused', 'waiting_user', 'completed', 'failed', 'cancelled')),
  waiting_reason text check (waiting_reason in ('clarify', 'login', 'approval')),
  error_class text,
  priority smallint not null default 0,
  capability_scope jsonb not null default '{}'::jsonb,
  budget_cents integer not null check (budget_cents >= 0),
  spent_cents integer not null default 0 check (spent_cents >= 0),
  deadline timestamptz,
  workflow_id text unique,
  version integer not null default 0,  -- optimistic lock for state transitions
  created_from_conversation uuid references public.conversations (id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, short_code),
  check ((status = 'waiting_user') = (waiting_reason is not null))
);
create index tasks_user_created on public.tasks (user_id, created_at desc, id);
create index tasks_user_active on public.tasks (user_id, priority desc)
  where status in ('queued', 'running', 'paused', 'waiting_user');

create table public.task_events (
  id bigint generated always as identity primary key,
  task_id uuid not null references public.tasks (id) on delete cascade,
  user_id uuid not null references public.profiles (id) on delete cascade,
  seq integer not null,
  type text not null,
  payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique (task_id, seq)
);

-- cost tracking --------------------------------------------------------------

create table public.llm_calls (
  id bigint generated always as identity primary key,
  user_id uuid not null references public.profiles (id) on delete cascade,
  task_id uuid references public.tasks (id) on delete set null,
  conversation_id uuid references public.conversations (id) on delete set null,
  route text not null,                -- e.g. 'voice_turn', 'browser_decide', 'sms_classify'
  model text not null,
  input_tokens integer not null check (input_tokens >= 0),
  cached_tokens integer not null default 0 check (cached_tokens >= 0),
  output_tokens integer not null check (output_tokens >= 0),
  latency_ms integer check (latency_ms >= 0),
  cost_cents numeric(12, 4) not null check (cost_cents >= 0),
  error text,
  ts timestamptz not null default now()
);
create index llm_calls_user_ts on public.llm_calls (user_id, ts desc);

create table public.usage_records (
  id bigint generated always as identity primary key,
  user_id uuid not null references public.profiles (id) on delete cascade,
  provider text not null,
  sku text not null,
  quantity numeric(14, 4) not null check (quantity >= 0),
  cost_cents numeric(12, 4) not null check (cost_cents >= 0),
  task_id uuid references public.tasks (id) on delete set null,
  ts timestamptz not null default now()
);
create index usage_records_user_ts on public.usage_records (user_id, ts desc);

-- audit ----------------------------------------------------------------------

create table audit.events (
  id bigint generated always as identity primary key,
  user_id uuid references public.profiles (id) on delete set null,
  actor text not null,                -- 'user', 'worker', 'voice', 'system'
  action text not null,
  target_type text,
  target_id text,
  metadata jsonb not null default '{}'::jsonb,
  ts timestamptz not null default now()
);
create index audit_events_user_ts on audit.events (user_id, ts desc);

create function audit.reject_mutation() returns trigger
language plpgsql as $$
begin
  raise exception 'audit.events is append-only (% rejected)', tg_op
    using errcode = 'insufficient_privilege';
end;
$$;

create trigger audit_events_append_only
  before update or delete on audit.events
  for each row execute function audit.reject_mutation();

create trigger audit_events_no_truncate
  before truncate on audit.events
  for each statement execute function audit.reject_mutation();

