-- Fixes from the Supabase security/performance advisors after core_schema.

-- Trigger functions must not resolve names through a caller-controlled search_path.
alter function audit.reject_mutation() set search_path = '';

-- Evaluate the JWT once per statement instead of once per row.
do $$
declare
  t record;
begin
  for t in
    select schemaname, tablename from pg_policies where policyname = 'require_mfa'
  loop
    execute format(
      'alter policy require_mfa on %I.%I using (((select auth.jwt()) ->> ''aal'') = ''aal2'')',
      t.schemaname, t.tablename);
  end loop;
end;
$$;

-- Cover foreign keys used by cascading deletes and per-task/per-user lookups.
create index llm_calls_task on public.llm_calls (task_id);
create index llm_calls_conversation on public.llm_calls (conversation_id);
create index messages_user on public.messages (user_id);
create index task_events_user on public.task_events (user_id);
create index tasks_created_from_conversation on public.tasks (created_from_conversation);
create index usage_records_task on public.usage_records (task_id);

-- Supabase's RLS auto-enable event trigger function (hosted projects only) is exposed
-- through the RPC API by default. Event triggers do not need EXECUTE, so revoke it.
do $$
begin
  if to_regprocedure('public.rls_auto_enable()') is not null then
    revoke execute on function public.rls_auto_enable() from public, anon, authenticated;
  end if;
end;
$$;
