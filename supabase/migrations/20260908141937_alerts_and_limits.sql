-- Alerts the worker raises about its own health, and active time per task.

-- At most one open alert per kind: it stays open (and visible in the dashboard) until the
-- condition clears, whether or not the text about it was delivered.
create table public.alerts (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.profiles (id) on delete cascade,
  kind text not null,
  detail text not null,
  raised_at timestamptz not null default now(),
  resolved_at timestamptz
);
create unique index alerts_one_open on public.alerts (user_id, kind) where resolved_at is null;
create index alerts_user_raised on public.alerts (user_id, raised_at desc);

-- Seconds the agent has spent working on a task (not waiting for the owner).
alter table public.tasks add column active_seconds integer not null default 0
  check (active_seconds >= 0);

revoke all on public.alerts from anon, authenticated;
grant select on public.alerts to authenticated;
grant select, insert, update on public.alerts to agent_worker;
alter table public.alerts enable row level security;
create policy worker_all on public.alerts for all to agent_worker using (true) with check (true);
create policy owner_read on public.alerts for select to authenticated
  using (user_id = (select auth.uid()));
create policy require_mfa on public.alerts as restrictive to authenticated
  using (((select auth.jwt()) ->> 'aal') = 'aal2');
