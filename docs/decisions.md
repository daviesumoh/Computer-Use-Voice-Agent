# Decisions

One entry per decision or dependency, with the reason. Newest first.

## 2026-01-22 — The worker is the only database writer

The voice agent (LiveKit Cloud) and the SMS webhook (Vercel) call the worker's internal API
with HMAC-signed requests (`services/common/src/agent_common/signing.py`, mirrored in
`apps/web/src/lib/service-signing.ts`, both checked against `services/common/signing_vectors.json`).
They hold one shared secret instead of database credentials, and task/message rules live in
one place. Every signed endpoint is idempotent, so replay within the 5-minute window is harmless:
calls by Twilio call SID, turns by (conversation, index, speaker), tasks by `request_id`,
inbound SMS by MessageSid, outbound SMS by `messages.dedupe_key`.

- Twilio REST is called with `httpx` (one endpoint) rather than the Twilio SDK.
- Twilio webhook signatures are verified in `apps/web/src/lib/twilio-signature.ts`, tested
  against vectors generated with Twilio's own `RequestValidator` (twilio 9.13.0), including
  Twilio's documented example.
- `start_task` refuses in code until every detail heard below 0.85 transcription confidence
  has been read back and confirmed, and a summary has been agreed.


## 2026-01-13 — Worker dependencies

- `playwright` — browser automation; pinned with the matching `mcr.microsoft.com/playwright/python` image.
- `pydantic-settings` — validated configuration; rejects unknown `WORKER_*` variables.
- `logfire` — tracing/log export to Pydantic Logfire (free tier); only active when a token is set.
- dev: `pytest`, `ruff`, `mypy` — tests, lint/format, strict typing.


## 2026-01-05 — Database: Supabase (hosted project "Computer Use Voice Agent", us-east-1)

Postgres + Auth (TOTP MFA is free; phone MFA is a paid add-on and not used) + Storage +
Realtime. Every table requires an `aal2` (MFA-verified) session via a restrictive RLS
policy. Migrations are tested locally (`supabase test db`) before being applied.
