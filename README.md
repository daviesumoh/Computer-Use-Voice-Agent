# Computer Use Voice Agent

Personal phone agent that takes task instructions over voice or SMS, runs them in a browser, and reports back by SMS.

## Layout

| Path | Role |
|---|---|
| `supabase/` | Postgres schema migrations, row-level security policies, pgTAP tests, and local Supabase config |
| `services/common/` | Shared HMAC-SHA256 request signing and Pydantic wire contracts (`agent-common`) |
| `services/worker/` | Sole database writer and internal API service |
| `services/voice/` | LiveKit telephony voice agent |
| `apps/web/` | Next.js dashboard and Twilio inbound SMS webhook |

## Requirements

- Docker
- Supabase CLI 2.20+
- Node 22+ with pnpm 9+
- Python 3.12+ with uv 0.12+
