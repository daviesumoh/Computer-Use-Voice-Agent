-- The dashboard refreshes itself when these change. Row-level security still applies: a
-- session only receives changes to rows it may read, and only at MFA level aal2.
alter publication supabase_realtime add table
  public.tasks, public.task_events, public.approvals, public.alerts;
