-- Access-control tests for memories, their sources, episodes and procedures.
begin;
create extension if not exists pgtap with schema extensions;
grant usage on schema extensions to agent_worker;  -- test-only, rolled back
select plan(12);

insert into auth.users (id, email) values
  ('00000000-0000-0000-0000-0000000000a5', 'owner5@example.test'),
  ('00000000-0000-0000-0000-0000000000b5', 'other5@example.test');
insert into public.profiles (id) values
  ('00000000-0000-0000-0000-0000000000a5'), ('00000000-0000-0000-0000-0000000000b5');
insert into public.memories (id, user_id, type, canonical_text, origin, cue) values
  ('00000000-0000-0000-0000-0000000000e1', '00000000-0000-0000-0000-0000000000a5', 'rule',
   'Always buy electronics from Best Buy.', 'user_stated', 'explicit_rule');
insert into public.memory_sources (memory_id, source_type, source_id, span) values
  ('00000000-0000-0000-0000-0000000000e1', 'message', gen_random_uuid(), 'always use Best Buy for electronics');
insert into public.episodes (user_id, kind, ref_id, summary) values
  ('00000000-0000-0000-0000-0000000000a5', 'call', gen_random_uuid(), 'Asked for TV prices.');

set local role authenticated;
set local request.jwt.claims = '{"sub":"00000000-0000-0000-0000-0000000000a5","role":"authenticated","aal":"aal2"}';
select is((select count(*)::int from public.memories), 1, 'aal2 owner reads own memories');
select is((select count(*)::int from public.memory_sources), 1, 'owner reads the source of a memory');
select is((select count(*)::int from public.episodes), 1, 'owner reads own episodes');
select throws_ok($$select embedding from public.memories$$, '42501', null,
  'embeddings are not readable from the dashboard');
select throws_ok($$select tsv from public.memories$$, '42501', null,
  'the search vector is not readable from the dashboard');
select throws_ok($$update public.memories set canonical_text = 'x'$$, '42501', null,
  'dashboard cannot edit memories directly; edits go through the worker');
select throws_ok($$select 1 from public.episode_memory_links$$, '42501', null,
  'deletion links are internal');

set local request.jwt.claims = '{"sub":"00000000-0000-0000-0000-0000000000a5","role":"authenticated","aal":"aal1"}';
select is_empty($$select 1 from public.memories$$, 'aal1 session sees no memories');
select is_empty($$select 1 from public.memory_sources$$, 'aal1 session sees no sources');

set local request.jwt.claims = '{"sub":"00000000-0000-0000-0000-0000000000b5","role":"authenticated","aal":"aal2"}';
select is_empty($$select 1 from public.memories union all select 1 from public.memory_sources$$,
  'another user sees neither memories nor their sources');

reset role;
set local role anon;
select throws_ok($$select 1 from public.memories$$, '42501', null, 'anon has no memory access');

reset role;
set local role agent_worker;
select lives_ok($$update public.memories set status = 'superseded'$$, 'worker can update memories');

select * from finish();
rollback;
