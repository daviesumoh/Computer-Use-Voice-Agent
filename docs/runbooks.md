# Runbooks

What to do when something breaks. Each entry says how you notice, what to check, what to do.
Nothing here relies on an automatic fallback: the system reports a problem and stops, and you
decide. Alerts arrive by text and stay on the dashboard's task page until the cause clears.

Commands assume the repository root. Parts marked **(not exercised live)** describe hosted
services that have not been connected yet.

## Alerts and what they mean

| Alert | Means | First look |
|---|---|---|
| `task_failures` | 3 or more tasks failed in 24 hours | Task list, filter failed; each has its error class |
| `internal_errors` | 2 or more tasks hit unexpected errors | Worker log; a provider outage or a bug |
| `security_incident` | A task was stopped for a security reason (a credential on the wrong origin, a secret in a URL) | The task's events and `audit.events` |
| `sms_delivery` | Texts to you failed, were refused (you texted STOP) or had no number | See "Texts are not arriving" |
| `cost_spike` | A quarter of the monthly cap spent in one day | `llm_calls` grouped by route and task |
| `cost_cap_near` | 80% of the monthly cap spent | Decide whether to raise the cap |

Checks run every 10 minutes. An alert opens once, is texted once, and closes by itself.

## A task is stuck, looping or spending

1. Text `STATUS`, then `KILL T7` (or `PAUSE T7`). Pausing keeps the browser's work; killing ends it.
2. If you cannot text, open the task in the dashboard and use the same commands, or press the
   emergency stop (below).
3. Budget (default $3), 2 hours of working time and the monthly cap each pause a task by
   themselves. `RESUME T7` grants one more allowance; it is refused while the monthly cap is hit.

## Emergency stop

Text `HALT ALL` from your verified number, or use the dashboard. Every task pauses and browsers
close. Clearing it needs the dashboard and a fresh authenticator code.

## The worker is down or restarted

Tasks are durable workflows in Postgres. On start the worker resumes each from its last
completed step; a task waiting for approval keeps waiting. After a restart check:
`select id, status from dbos.workflow_status where status not in ('SUCCESS','ERROR','CANCELLED');`
A browser that was open is gone; the task reopens the last page. An action that was already
started but not confirmed is recorded as `unknown` and you are asked; it is never repeated.

## Texts are not arriving

1. Dashboard alert `sms_delivery` shows the reasons. `opted_out` means you texted STOP: text
   `START` to the number. Twilio refuses sends until then (error 21610).
2. `failed`: Twilio console, Monitor, Logs, Messaging. Check the account balance and that the
   Canadian number's registration is still approved. **(not exercised live)**
3. If texts are down the alert itself will not reach you; the dashboard banner still shows it.

## OpenAI errors or a spike in cost

Model errors end the current task as an unexpected error and raise `internal_errors`. Check
the OpenAI status page and the project's usage limits. To stop spending at once: emergency stop,
then lower `user_settings.monthly_cost_cap_cents`. A memory-learning job that fails three times is left undone: that call's memories are
not learned, and nothing is retried later.

## The database is paused or unreachable

The hosted free plan pauses after 7 days without activity; Supabase dashboard, Restore project.
The worker's health endpoint (`/healthz`) fails while the database is down and tasks resume
from their last step afterwards.

## Restoring from a backup

Hosted backups need the Pro plan (daily, 7 days) **(not exercised live)**. Practise the steps
on the local database any time:

    ./scripts/restore-drill.sh

It dumps the `public` and `audit` schemas, restores them into a scratch database and compares
row counts for the tables that matter. Expect exactly two `pg_restore` errors (schema `public`
already exists, and the foreign key to `auth.users`); anything else, or a MISMATCH line, is a
failure. After a real restore, run `supabase test db` against it, sign in, and check that
`select count(*) from memories` and `approvals` look right. Restoring brings back forgotten
memories that were erased after the backup was taken; forget them again.

## Rotating secrets

- **Service signing secret** (`WORKER_SERVICE_HMAC_SECRET`, shared with the voice agent and
  the web app): change it in all three places in one go; requests in flight fail once.
- **Bitwarden machine token:** revoke it in Bitwarden, create a new one, set
  `WORKER_BITWARDEN_ACCESS_TOKEN`. Saved website passwords are unaffected.
- **Saved-login key** (`WORKER_SESSION_KEY`): changing it makes every saved login unreadable;
  tasks will ask you to sign in again by takeover.
- **Twilio and OpenAI keys:** replace in the worker's environment and restart.

## A login needs you (takeover)

A task parked on a login, CAPTCHA or code sends a text with a dashboard link. Open the task,
take over, finish the step, press Done. If the browser was lost meanwhile, the task reopens
the last page and asks again.
