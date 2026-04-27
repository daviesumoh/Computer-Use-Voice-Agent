# Computer Use Voice Agent

Personal phone agent that takes task instructions over voice or SMS, runs them in a headed Chromium browser through a durable workflow queue, and reports progress by SMS and a web dashboard.

Tasks are queued, paused, resumed, killed, and budget-limited by a DBOS workflow engine backed by Postgres. With `WORKER_OPENAI_API_KEY` set, tasks execute in Playwright against real pages; without it, started tasks fail fast with `executor_unavailable`.

## Architecture

| Path | Role |
|---|---|
| `supabase/` | Postgres schema migrations, row-level security policies, pgTAP access-control tests, and local Supabase CLI configuration |
| `apps/web/` | Next.js dashboard (password + Cloudflare Turnstile + TOTP MFA), task list, and the Twilio inbound SMS webhook |
| `services/worker/` | Sole database writer. Exposes an HMAC-signed internal API for call turns, task creation, and inbound SMS; runs DBOS workflows and the Playwright browser agent (`worker/browser/`) |
| `services/voice/` | LiveKit telephony worker: Deepgram Flux STT (`flux-general-en`) -> `gpt-4.1` -> Deepgram Aura-2 TTS (`aura-2-thalia-en`) |
| `services/common/` | Shared HMAC-SHA256 request signing and Pydantic wire contracts (`agent-common`) |
| `docs/decisions.md` | Engineering decisions, trade-offs, and benchmark notes |

## Quick Start

```sh
# 1. Local Postgres & Auth (uses ports 553xx)
echo 'SUPABASE_AUTH_CAPTCHA_SECRET=1x0000000000000000000000000000000AA' > supabase/.env
supabase start -x studio,inbucket,imgproxy,edge-runtime,logflare,vector,supavisor
supabase test db

# 2. Web dashboard
cd apps/web
cp .env.example .env.local
pnpm install
pnpm dev

# 3. Worker service
cd ../../services/worker
cp .env.example .env
uv sync
uv run playwright install chromium
uv run pytest

# 4. Voice agent service
cd ../voice
cp .env.example .env
uv sync
uv run pytest
```
