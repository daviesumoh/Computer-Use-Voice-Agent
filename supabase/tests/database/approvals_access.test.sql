-- Access-control tests for approvals, the action ledger, accounts, sessions and tokens.
begin;
create extension if not exists pgtap with schema extensions;
grant usage on schema extensions to agent_worker;  -- test-only, rolled back
select plan(13);

insert into auth.users (id, email) values
  ('00000000-0000-0000-0000-0000000000a1', 'owner4@example.test'),
  ('00000000-0000-0000-0000-0000000000b1', 'other4@example.test');
insert into public.profiles (id) values
  ('00000000-0000-0000-0000-0000000000a1'), ('00000000-0000-0000-0000-0000000000b1');
insert into public.tasks (id, user_id, short_code, title, spec, budget_cents) values
  ('00000000-0000-0000-0000-0000000000c1', '00000000-0000-0000-0000-0000000000a1', 'T1', 'owner', '{}', 300);
insert into public.approvals (user_id, task_id, action_class, summary, intent_hash, expires_at) values
  ('00000000-0000-0000-0000-0000000000a1', '00000000-0000-0000-0000-0000000000c1', 'purchase',
   'Place order', 'h', now() + interval '30 minutes');
insert into public.connected_accounts (id, user_id, site_domain, allowed_origins, label, secret_ref) values
  ('00000000-0000-0000-0000-0000000000d1', '00000000-0000-0000-0000-0000000000a1', 'shop.example',
   array['https://shop.example'], 'main', 'bw-secret-id');
insert into public.account_sessions (account_id, user_id, state_ciphertext) values
  ('00000000-0000-0000-0000-0000000000d1', '00000000-0000-0000-0000-0000000000a1', '\x00');
insert into public.takeover_tokens (token_hash, user_id, task_id, expires_at) values
  ('abc', '00000000-0000-0000-0000-0000000000a1', '00000000-0000-0000-0000-0000000000c1', now() + interval '1 minute');

set local role authenticated;
set local request.jwt.claims = '{"sub":"00000000-0000-0000-0000-0000000000a1","role":"authenticated","aal":"aal2"}';
select is((select count(*)::int from public.approvals), 1, 'aal2 owner sees own approvals');
select is((select count(*)::int from public.connected_accounts), 1, 'aal2 owner lists own accounts');
select throws_ok($$select secret_ref from public.connected_accounts$$, '42501', null,
  'owner cannot read the secret reference');
select throws_ok($$select 1 from public.account_sessions$$, '42501', null,
  'owner cannot read saved browser sessions');
select throws_ok($$select 1 from public.takeover_tokens$$, '42501', null,
  'owner cannot read takeover tokens');
select throws_ok($$update public.approvals set status = 'approved'$$, '42501', null,
  'dashboard cannot approve directly; the worker checks MFA freshness');

set local request.jwt.claims = '{"sub":"00000000-0000-0000-0000-0000000000a1","role":"authenticated","aal":"aal1"}';
select is_empty($$select 1 from public.approvals$$, 'aal1 session sees no approvals');

set local request.jwt.claims = '{"sub":"00000000-0000-0000-0000-0000000000b1","role":"authenticated","aal":"aal2"}';
select is_empty($$select 1 from public.connected_accounts$$, 'another user sees no accounts');

reset role;
set local role anon;
select throws_ok($$select 1 from public.approvals$$, '42501', null, 'anon has no approval access');
select throws_ok($$select 1 from public.instructions$$, '42501', null, 'anon has no instruction access');

reset role;
set local role agent_worker;
select lives_ok($$update public.approvals set status = 'expired'$$, 'worker can update approvals');

-- Deleting a user must not be blocked by, or rewrite, their audit trail.
reset role;
insert into audit.events (user_id, actor, action) values ('00000000-0000-0000-0000-0000000000a1', 'user', 'probe');
select lives_ok($$delete from auth.users where id = '00000000-0000-0000-0000-0000000000a1'$$,
  'a user with audit rows can be deleted');
select is((select count(*)::int from audit.events where user_id = '00000000-0000-0000-0000-0000000000a1'), 1,
  'their audit rows remain, still carrying the id');

select * from finish();
rollback;
