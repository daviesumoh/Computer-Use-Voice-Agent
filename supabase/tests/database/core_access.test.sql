-- Access-control tests for the core schema. Run with: supabase test db
begin;
create extension if not exists pgtap with schema extensions;
grant usage on schema extensions to agent_worker;  -- test-only, rolled back
select plan(13);

-- Two users, each owning one task.
insert into auth.users (id, email) values
  ('00000000-0000-0000-0000-00000000000a', 'owner@example.test'),
  ('00000000-0000-0000-0000-00000000000b', 'other@example.test');
insert into public.profiles (id) values
  ('00000000-0000-0000-0000-00000000000a'),
  ('00000000-0000-0000-0000-00000000000b');
insert into public.tasks (user_id, short_code, title, spec, budget_cents) values
  ('00000000-0000-0000-0000-00000000000a', 'T1', 'owner task', '{}', 300),
  ('00000000-0000-0000-0000-00000000000b', 'T1', 'other task', '{}', 300);
insert into audit.events (user_id, actor, action) values
  ('00000000-0000-0000-0000-00000000000a', 'system', 'seed');

-- Signed-in owner with MFA: sees exactly their own rows.
set local role authenticated;
set local request.jwt.claims = '{"sub":"00000000-0000-0000-0000-00000000000a","role":"authenticated","aal":"aal2"}';
select results_eq($$select title from public.tasks$$, array['owner task'], 'aal2 owner sees only own task');
select is((select count(*)::int from audit.events), 1, 'aal2 owner sees own audit events');

-- Same user without MFA: sees nothing at all.
set local request.jwt.claims = '{"sub":"00000000-0000-0000-0000-00000000000a","role":"authenticated","aal":"aal1"}';
select is_empty($$select 1 from public.tasks$$, 'aal1 session sees no tasks');
select is_empty($$select 1 from public.profiles$$, 'aal1 session sees no profile');
select is_empty($$select 1 from audit.events$$, 'aal1 session sees no audit events');

-- A token with no aal claim is treated as unverified.
set local request.jwt.claims = '{"sub":"00000000-0000-0000-0000-00000000000a","role":"authenticated"}';
select is_empty($$select 1 from public.tasks$$, 'token without aal claim sees no tasks');

-- The dashboard role cannot write; commands go through the worker.
set local request.jwt.claims = '{"sub":"00000000-0000-0000-0000-00000000000a","role":"authenticated","aal":"aal2"}';
select throws_ok(
  $$update public.tasks set status = 'cancelled'$$, '42501', null, 'dashboard cannot update tasks');
select throws_ok(
  $$insert into audit.events (actor, action) values ('user', 'forged')$$, '42501', null,
  'dashboard cannot write audit events');

-- Anonymous callers see nothing.
reset role;
set local role anon;
select throws_ok($$select 1 from public.tasks$$, '42501', null, 'anon has no table access');

-- The worker can read and write across rows, but cannot rewrite the audit log.
reset role;
set local role agent_worker;
-- Scoped to this test's users: other suites (e.g. dashboard e2e) may leave rows behind.
select * from finish();
rollback;
