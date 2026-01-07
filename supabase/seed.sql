-- Local development only: `supabase start` / `supabase db reset` run this; hosted projects never do.
-- Production sets the worker's password out of band.
alter role agent_worker with login password 'local-dev-only';
