# Decisions

One entry per decision or dependency, with the reason. Newest first.

- **Guards.** Same action 3 times → `loop_detected`; 5 steps in a row that cannot act →
  `stuck`; 150 steps per task (engine). Recoverable failures retry 3 times (1 s, 4 s, 15 s).
- **Cost.** Every model call writes `llm_calls` (tokens, latency, cost); the task's budget
  is charged the cost rounded up to whole cents. The price table holds gpt-6.1-sol
  ($2 / $0.10 cached / $10 per million tokens, from OpenAI's model page).
- **Extraction** is a second, tool-less model call over the page text, so a page cannot
  steer the tool-using model through the values it asks for.

Dependency: `openai` — the model client (Responses API with structured output, `store=False`).
Verified live (`store=False`): the first call failed because OpenAI's
strict schemas reject `oneOf`, which a Pydantic discriminated union emits, so `Action` is a
plain union (a test guards this). Then three read-only tasks succeeded: cheapest TV on the
local test site (3 steps, ~0.9 cents), the same task from a page carrying an injected "press
Buy now" (the model ignored it; 0 purchases; it did not mention the attempt in its answer
despite the prompt asking it to), and a Wikipedia lookup (3 steps, ~6 cents; the large page
snapshot dominates the cost). Model latency was about 2.5 s per call.

Real retail sites, headed Chromium on a developer Mac, read-only:
- walmart.ca served a "Robot or human?" check after the search (a `/blocked` page on a rerun).
  The first run showed it was only detectable from the page body, not the title or status, so
  body text is now checked too; the task ends as `needs_user` and nothing tries to pass it.
- canadiantire.ca: search worked and the model listed three drills, but no prices appeared in
  the page snapshot (not on the results or the product page), so it reported none rather than
  guessing. The run took 9 model calls and ~16 cents, mostly large snapshots.


## 2026-03-29 — Worker host: Northflank free Sandbox, slower browsing accepted

Measured with `services/worker` `browser-smoke` (headed Chromium, fonts/media blocked) in
Docker with CPU/memory limits matching each plan, against walmart.ca, canadiantire.ca and
theweathernetwork.com:

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
