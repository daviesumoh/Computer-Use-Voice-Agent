-- Durable task engine: DBOS state schema and the instruction inbox.

-- DBOS keeps workflow state in its own schema. It is created here, owned by the worker role,
-- because the worker has no CREATE privilege on the database itself.
create schema dbos authorization agent_worker;

-- Commands that change a task's run state are recorded before the task's workflow applies
-- them at its next safe point. `seq` fixes the order when channels race. Read-only commands
-- (status) are answered directly and never stored.
create table public.instructions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.profiles (id) on delete cascade,
  task_id uuid not null references public.tasks (id) on delete cascade,
  message_id uuid unique references public.messages (id) on delete set null,  -- one per inbound text
  seq bigint generated always as identity,
  command text not null check (command in ('pause', 'resume', 'cancel')),
  received_at timestamptz not null default now(),
  applied_at timestamptz,
  outcome text
);
create index instructions_task_seq on public.instructions (task_id, seq);
create index instructions_pending on public.instructions (task_id, seq) where applied_at is null;
create index instructions_user_seq on public.instructions (user_id, seq desc);

grant select on public.instructions to authenticated;
grant select, insert, update on public.instructions to agent_worker;

alter table public.instructions enable row level security;
create policy owner_read on public.instructions for select to authenticated
  using (user_id = (select auth.uid()));
create policy require_mfa on public.instructions as restrictive to authenticated
  using (((select auth.jwt()) ->> 'aal') = 'aal2');
create policy worker_all on public.instructions for all to agent_worker
  using (true) with check (true);
