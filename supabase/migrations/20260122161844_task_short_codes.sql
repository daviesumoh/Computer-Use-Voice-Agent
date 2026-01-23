-- Per-user counter behind task short codes (T1, T2, ...), the names the user says on calls
-- and texts in SMS. The worker allocates with a single UPDATE ... RETURNING, which row-locks
-- the profile, so a voice and an SMS request racing for a code can never get the same one.
alter table public.profiles
  add column next_task_number integer not null default 1 check (next_task_number > 0);

-- Outbound SMS dedupe: the worker records a send under a key such as 'task:<id>:created'
-- before calling Twilio (whose message API has no idempotency keys), so a retried
-- notification cannot text the user twice.
alter table public.messages add column dedupe_key text unique;

-- Idempotent task creation: the caller (voice agent, SMS router) sends a request id, so a
-- retried "start the task" tool call returns the existing task instead of creating another.
alter table public.tasks add column request_id uuid unique;

-- SMS is one continuous thread per user. Concurrent inbound texts must not open two.
create unique index conversations_one_open_sms on public.conversations (user_id)
  where channel = 'sms' and ended_at is null;
