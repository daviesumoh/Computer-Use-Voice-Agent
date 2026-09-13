#!/usr/bin/env bash
# Proves a backup of the local database can be restored: dumps it, restores into a scratch
# database, compares row counts of the tables that matter, and drops the scratch database.
# For the hosted project the same steps apply to a Supabase backup (see docs/runbooks.md).
set -euo pipefail

container=${DB_CONTAINER:-supabase_db_Computer_Use_Voice_Agent}
scratch=restore_drill_$$
tables=(profiles phone_numbers conversations messages tasks task_events memories memory_sources
        episodes procedures approvals browser_actions connected_accounts alerts)

psql() { docker exec -i "$container" psql -U postgres -v ON_ERROR_STOP=1 -tA "$@"; }
trap 'psql -c "drop database if exists $scratch" >/dev/null' EXIT

# Only the application's own schemas: auth and storage belong to Supabase's backups.
docker exec "$container" pg_dump -U postgres --format=custom --schema=public --schema=audit \
  --no-owner --no-privileges postgres >/tmp/$scratch.dump
docker cp /tmp/$scratch.dump "$container:/tmp/$scratch.dump"
rm /tmp/$scratch.dump

psql -c "create database $scratch" >/dev/null
# Expect exactly two pg_restore errors: `schema public already exists`, and the foreign key
# from profiles to auth.users (the scratch database has no users). Anything else is a failure,
# and the row counts below are the check.
# The scratch database gets stand-ins for what Supabase provides (auth tables and functions).
docker exec "$container" sh -c "psql -U postgres -d $scratch -c 'create schema auth; create table auth.users (id uuid primary key); create function auth.jwt() returns jsonb language sql as \$\$select null::jsonb\$\$; create function auth.uid() returns uuid language sql as \$\$select null::uuid\$\$; create schema extensions; create extension vector with schema extensions;' >/dev/null && pg_restore -U postgres -d $scratch --no-owner --disable-triggers /tmp/$scratch.dump" 2>&1 | grep -v "already exists" || true
docker exec "$container" rm -f /tmp/$scratch.dump

status=0
for table in "${tables[@]}"; do
  live=$(psql -d postgres -c "select count(*) from public.$table")
  restored=$(psql -d "$scratch" -c "select count(*) from public.$table")
  if [ "$live" = "$restored" ]; then
    echo "ok       $table: $live rows"
  else
    echo "MISMATCH $table: $live live, $restored restored"
    status=1
  fi
done
exit $status
