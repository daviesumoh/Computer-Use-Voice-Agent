-- Approvals, the action ledger, connected accounts, saved browser sessions and
-- takeover tokens.

-- audit.events is append-only, but its user_id foreign key was `on delete set null`, which is
-- an UPDATE: deleting a user who had any audit rows failed. The log keeps the id instead.
alter table audit.events drop constraint events_user_id_fkey;

-- Approvals: a pending request from a task to do something its scope does not simply allow.
create table public.approvals (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.profiles (id) on delete cascade,
  task_id uuid not null references public.tasks (id) on delete cascade,
  action_class text not null,
  summary text not null,           -- what will happen, in words the owner can judge
  intent_hash text not null,       -- binds the approval to this exact action on this exact page
  status text not null default 'pending'
    check (status in ('pending', 'approved', 'denied', 'expired', 'used')),
  requested_at timestamptz not null default now(),
  expires_at timestamptz not null,
  decided_at timestamptz,
  mfa_verified_at timestamptz      -- when the owner last proved a TOTP code for this decision
);
create unique index approvals_one_pending on public.approvals (task_id, intent_hash)
  where status in ('pending', 'approved');
create index approvals_user_status on public.approvals (user_id, status, requested_at desc);
create index approvals_task on public.approvals (task_id);

-- The action ledger: written before an irreversible action runs, so a retry or a replayed
-- step can never perform it twice.
create table public.browser_actions (
  id bigint generated always as identity primary key,
  task_id uuid not null references public.tasks (id) on delete cascade,
  user_id uuid not null references public.profiles (id) on delete cascade,
  action_class text not null,
  description text not null,
  intent_hash text not null,
  phase text not null check (phase in ('started', 'confirmed', 'unknown', 'not_happened')),
  started_at timestamptz not null default now(),
  finished_at timestamptz
);
-- At most one live attempt per intent; 'not_happened' (proven not done) frees it for a retry.
create unique index browser_actions_one_attempt on public.browser_actions (task_id, intent_hash)
  where phase in ('started', 'confirmed', 'unknown');
create index browser_actions_user on public.browser_actions (user_id);

-- Websites the owner has connected. The credential itself lives in Bitwarden; only its id is here.
create table public.connected_accounts (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.profiles (id) on delete cascade,
  site_domain text not null check (site_domain = lower(site_domain)),
  allowed_origins text[] not null check (cardinality(allowed_origins) > 0),
  label text not null,
  username_hint text not null default '',
  secret_ref text not null,
  status text not null default 'active' check (status in ('active', 'needs_update')),
  last_login_at timestamptz,
  created_at timestamptz not null default now(),
  unique (user_id, site_domain, label)
);
create index connected_accounts_user on public.connected_accounts (user_id);

-- Saved logged-in browser state, encrypted by the worker before it reaches the database.
create table public.account_sessions (
  account_id uuid primary key references public.connected_accounts (id) on delete cascade,
  user_id uuid not null references public.profiles (id) on delete cascade,
  state_ciphertext bytea not null,
  updated_at timestamptz not null default now()
);
create index account_sessions_user on public.account_sessions (user_id);

-- Single-use tokens that let the dashboard open a live view of a task's browser.
create table public.takeover_tokens (
  token_hash text primary key,     -- sha256 of the token; the token itself is never stored
  user_id uuid not null references public.profiles (id) on delete cascade,
  task_id uuid not null references public.tasks (id) on delete cascade,
  expires_at timestamptz not null,
  used_at timestamptz
);
create index takeover_tokens_task on public.takeover_tokens (task_id);
create index takeover_tokens_user on public.takeover_tokens (user_id);

-- Which accounts a task may use is fixed when the owner confirms it.
alter table public.tasks add column account_ids uuid[] not null default '{}';

-- privileges -------------------------------------------------------------------------------
-- Supabase grants broad default privileges on every new public table, and a table-level grant
-- would override the column-level one below. Replace them, and tighten `instructions` from
-- the previous migration, which was created under the same defaults.

revoke all on public.instructions, public.approvals, public.browser_actions,
  public.connected_accounts, public.account_sessions, public.takeover_tokens
  from anon, authenticated;
grant select on public.instructions, public.approvals, public.browser_actions to authenticated;
-- The dashboard lists accounts but never sees the Bitwarden reference.
grant select (id, user_id, site_domain, allowed_origins, label, username_hint, status,
              last_login_at, created_at) on public.connected_accounts to authenticated;
grant select, insert, update on public.approvals, public.browser_actions,
  public.connected_accounts, public.account_sessions, public.takeover_tokens to agent_worker;
grant delete on public.connected_accounts, public.account_sessions to agent_worker;

do $$
declare
  t text;
begin
  foreach t in array array['approvals', 'browser_actions', 'connected_accounts',
                           'account_sessions', 'takeover_tokens']
  loop
    execute format('alter table public.%I enable row level security', t);
    execute format(
      'create policy worker_all on public.%I for all to agent_worker using (true) with check (true)', t);
    if t not in ('account_sessions', 'takeover_tokens') then
      execute format(
        'create policy owner_read on public.%I for select to authenticated '
        'using (user_id = (select auth.uid()))', t);
      execute format(
        'create policy require_mfa on public.%I as restrictive to authenticated '
        'using (((select auth.jwt()) ->> ''aal'') = ''aal2'')', t);
    end if;
  end loop;
end;
$$;
