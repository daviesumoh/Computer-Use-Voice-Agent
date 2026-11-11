# Computer Use Voice Agent

Personal phone agent that takes task instructions over voice or SMS, runs them in a headed Chromium browser through a durable workflow queue, and reports progress by SMS and a web dashboard.

Tasks are queued, paused, resumed, killed, and budget-limited by a DBOS workflow engine backed by Postgres. With `WORKER_OPENAI_API_KEY` set, tasks execute in Playwright against real pages; without it, started tasks fail fast with `executor_unavailable`. Read-only actions (browsing, searching, extracting text) run automatically within the task's budget. State-changing actions (purchases, form submissions, messages, deletions, account changes) pause the workflow and require explicit approval in the dashboard with a fresh TOTP code. Site credentials are stored in Bitwarden Secrets Manager and typed only into verified origins; when a site presents a login challenge, CAPTCHA, or MFA prompt, the dashboard streams the live browser over a WebSocket for manual takeover.

The voice and worker services also maintain long-term memory of facts, preferences, and rules stated on calls (cited back to the exact transcript span), enforce per-task and monthly spending and active-time caps, emit self-health alerts, and send Web Push notifications alongside SMS.

## Architecture

| Path | Role |
|---|---|
| `supabase/` | Postgres schema migrations, row-level security policies, pgTAP access-control tests, and local Supabase CLI configuration |
| `apps/web/` | Next.js dashboard (password + Cloudflare Turnstile + TOTP MFA), live browser takeover viewer, approvals, memory management, and the Twilio inbound SMS webhook |
| `services/worker/` | Sole database writer. Exposes an HMAC-signed internal API for call turns, task creation, and inbound SMS; runs DBOS workflows, the Playwright browser agent (`worker/browser/`), memory ingestion/recall (`worker/memory/`), health checks, and outbound Twilio SMS / Web Push |
| `services/voice/` | LiveKit telephony worker: Deepgram Flux STT (`flux-general-en`) -> `gpt-4.1` -> Deepgram Aura-2 TTS (`aura-2-thalia-en`). Tracks low-confidence transcript spans, requires read-back confirmation before starting a task, and calls the worker over the signed API |
| `services/common/` | Shared HMAC-SHA256 request signing and Pydantic wire contracts (`agent-common`) used by `services/worker` and `services/voice` |
| `docs/decisions.md` | Engineering decisions, trade-offs, and benchmark notes |
| `docs/runbooks.md`, `scripts/` | Operational runbooks, VAPID key generator, voice staging script, and Postgres backup restore drill |

## Requirements

- Docker
- [Supabase CLI](https://supabase.com/docs/guides/cli) 2.120+
- Node 22+ with pnpm 9+
- Python 3.12+ with [uv](https://docs.astral.sh/uv/) 0.12+

## Quick Start

```sh
# 1. Local Postgres & Auth (uses ports 553xx)
echo 'SUPABASE_AUTH_CAPTCHA_SECRET=1x0000000000000000000000000000000AA' > supabase/.env
supabase start -x studio,inbucket,imgproxy,edge-runtime,logflare,vector,supavisor
supabase test db

# 2. Web dashboard
cd apps/web
cp .env.example .env.local   # fill in NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY from `supabase status`
pnpm install
pnpm dev                     # http://localhost:3000 (use localhost, not 127.0.0.1)
eval "$(supabase status -o env)" && export SUPABASE_SECRET_KEY=$SECRET_KEY NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY=$PUBLISHABLE_KEY
pnpm test:e2e

# 3. Worker service
cd ../../services/worker
cp .env.example .env
uv sync
uv run playwright install chromium
uv run pytest                # add `-m live -s` to run live OpenAI evaluations

# 4. Voice agent service
cd ../voice
cp .env.example .env
uv sync
uv run pytest
```

On macOS, if `uv run <script>` cannot import the editable package, macOS set the `UF_HIDDEN` flag on the `.pth` file in `.venv` (which Python's `site` module skips); `pytest` sets `pythonpath` explicitly in `pyproject.toml` and is unaffected.

Public signups are disabled in `supabase/config.toml`. Create your owner user via the Supabase admin API or Studio; the first sign-in in the dashboard enrolls a TOTP authenticator app.

## Configuration

Each component reads environment variables from its own `.env` file (ignored by git) with documented templates:

- `apps/web/.env.example` — Supabase URL/publishable key, Turnstile site key, worker URL, shared `SERVICE_SIGNING_SECRET`, and `TWILIO_AUTH_TOKEN`
- `services/worker/.env.example` — Postgres connection (`WORKER_DATABASE_URL`), `WORKER_SERVICE_SIGNING_SECRET`, OpenAI API key and model names, Twilio credentials, Bitwarden Secrets Manager IDs, `WORKER_SESSION_KEY` (32-byte base64 key for AES-256-GCM session state), and Web Push VAPID keys
- `services/voice/.env.example` — LiveKit Cloud credentials, Deepgram/OpenAI keys, worker URL, `VOICE_SERVICE_SIGNING_SECRET`, and `VOICE_USER_ID`

## Access Control

Every application table enforces a restrictive row-level security policy requiring an MFA-verified JWT (`aal2`), so a password-only (`aal1`) session cannot read rows. The `authenticated` role has `SELECT` grants only (with sensitive columns such as `connected_accounts.secret_ref` and `memories.embedding` omitted from column grants) and no direct table write access. Owner mutations from the dashboard (approvals, emergency-stop reset, connected accounts, takeover tokens, memory edits) call the worker with the user's Supabase access token, where the worker verifies token validity with Supabase Auth and requires a recent TOTP verification timestamp for sensitive actions. `audit.events` rejects `UPDATE`, `DELETE`, and `TRUNCATE` from every role via triggers.

See `supabase/tests/database/core_access.test.sql`, `supabase/tests/database/approvals_access.test.sql`, and `apps/web/e2e/auth.spec.ts`.
