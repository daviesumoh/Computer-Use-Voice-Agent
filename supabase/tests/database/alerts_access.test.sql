-- Access-control tests for alerts.
begin;
create extension if not exists pgtap with schema extensions;
select plan(7);

insert into auth.users (id, email) values
  ('00000000-0000-0000-0000-0000000000a6', 'owner6@example.test'),
  ('00000000-0000-0000-0000-0000000000b6', 'other6@example.test');
insert into public.profiles (id) values
  ('00000000-0000-0000-0000-0000000000a6'), ('00000000-0000-0000-0000-0000000000b6');
insert into public.alerts (user_id, kind, detail) values
  ('00000000-0000-0000-0000-0000000000a6', 'task_failures', '3 tasks failed');

insert into public.push_subscriptions (user_id, endpoint, p256dh, auth) values
  ('00000000-0000-0000-0000-0000000000a6', 'https://push.example/1', 'k', 'a');

set local role authenticated;
set local request.jwt.claims = '{"sub":"00000000-0000-0000-0000-0000000000a6","role":"authenticated","aal":"aal2"}';
select is((select count(*)::int from public.alerts), 1, 'aal2 owner reads own alerts');
select throws_ok($$update public.alerts set resolved_at = now()$$, '42501', null,
  'dashboard cannot resolve alerts; the worker does when the condition clears');
set local request.jwt.claims = '{"sub":"00000000-0000-0000-0000-0000000000a6","role":"authenticated","aal":"aal1"}';
select is_empty($$select 1 from public.alerts$$, 'aal1 session sees no alerts');
set local request.jwt.claims = '{"sub":"00000000-0000-0000-0000-0000000000b6","role":"authenticated","aal":"aal2"}';
select is_empty($$select 1 from public.alerts$$, 'another user sees no alerts');
reset role;
set local role anon;
select throws_ok($$select 1 from public.alerts$$, '42501', null, 'anon has no alert access');

reset role;
set local role authenticated;
set local request.jwt.claims = '{"sub":"00000000-0000-0000-0000-0000000000a6","role":"authenticated","aal":"aal2"}';
select throws_ok($$select 1 from public.push_subscriptions$$, '42501', null,
  'the dashboard cannot read push subscription keys');
select throws_ok($$select 1 from public.push_log$$, '42501', null, 'nor the push log');

select * from finish();
rollback;
