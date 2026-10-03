-- Web push: the browsers that asked for alerts, and which alerts were already pushed.

create table public.push_subscriptions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.profiles (id) on delete cascade,
  endpoint text not null unique,      -- the browser vendor's address for this device
  p256dh text not null,               -- the device's public key
  auth text not null,                 -- the device's shared secret for encrypting payloads
  created_at timestamptz not null default now()
);
create index push_subscriptions_user on public.push_subscriptions (user_id);

-- Makes sending idempotent: a step that runs twice pushes once.
create table public.push_log (
  dedupe_key text primary key,
  sent_at timestamptz not null default now()
);

-- Only the worker reads these: a subscription's keys let anyone holding them push to the device.
revoke all on public.push_subscriptions, public.push_log from anon, authenticated;
grant select, insert, update, delete on public.push_subscriptions to agent_worker;
grant select, insert on public.push_log to agent_worker;
alter table public.push_subscriptions enable row level security;
alter table public.push_log enable row level security;
create policy worker_all on public.push_subscriptions for all to agent_worker
  using (true) with check (true);
create policy worker_all on public.push_log for all to agent_worker using (true) with check (true);
