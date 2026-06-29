embedded, compared and shown.

**Only the owner's words become memories.** After a call, `gpt-4.1-mini` proposes candidates
with the exact span it relied on. Code then decides (`memory/gates.py`): the span must occur
in something the owner said in that call, one-off requests are dropped, and anything that
looks like a card or ID number, a password or PIN, a long token or a health detail is dropped
(a deliberately blunt keyword list). Website text can never reach this path: task episodes are
written from the title, the owner's summary and the outcome only. A habit seen once is stored
as `pending_confirm`; after three sightings in two calls the agent asks once. A habit never
overrides something the owner stated.

**Changes.** An exact repeat refreshes the memory without a model call. Otherwise the new
statement is compared with the five nearest memories (the model answers duplicate / refines /
contradicts / unrelated per id; ids it invents are ignored). Contradicted memories are marked
`superseded` and the owner is told once at the next call. "Refines" keeps both.

**Search** (`memory/recall.py`): 0.5 meaning + 0.3 keywords + 0.2 recency, active memories
only, 600-token budget. Admission needs cosine >= 0.25 and >= 0.75 of the best match; a
keyword hit rescues a text only if it is also >= 0.25. On a labelled set of 20 memories and 15
questions with the real embeddings (`pytest -m live -s`): precision 0.75, recall 0.90, and two
unrelated questions returned nothing. The thresholds were tuned on that same set, so treat the
numbers as optimistic. Keywords match any word, not all (`websearch_to_tsquery` ANDs them and
missed most real questions). Live extraction on 10 utterances: 10/10 as expected (the gates
caught a peanut-allergy statement the model proposed); a replaced rule was superseded.

**Forgetting.** Forgetting hides a memory at once and erases it after the owner's undo window
(24 hours, `user_settings.memory_undo_hours`; 0 or "erase now" is immediate). It also removes
older wordings it replaced, and the summaries written while it was known. "Erase with its
sources" deletes the messages it came from. Kept afterwards: a SHA-256 of the text, nothing
else. A test searches the table, the keyword and vector indexes, sources, summaries, links and
the audit log for the forgotten text. "Clear everything" also wipes all summaries and routes.
An hourly loop in the worker erases expired ones; it is idempotent.

**Where it runs.** Learning is two DBOS workflows on their own queue (concurrency 1), started
when a call ends and when a task ends, with ids derived from the call / task so repeats attach
to the first. Steps are at-least-once, so the job is written to be repeated safely.
The voice agent gets a short card at call start (rules and preferences, 20 max) and three
tools: `recall_memory`, `forget_memory`, `settle_memory`. The browser agent gets the rules
and preferences that match its task, and a remembered route once it is on a site, shown
inside `<earlier_route>` tags as data, not instructions. A route that was offered and then
failed is marked stale. Pausing memory (dashboard) stops learning, recall, cards and routes.

**Not built.** Retention periods per memory type; reading memories from SMS (only voice
calls are learned from); episode summaries cannot be listed or deleted one by one (they go
with a forgotten memory or "clear everything"); procedures are keyed by site and
read/write only, not by task type, so a route is a hint about the site; no eval of a
GPT-4.1-mini replacement yet (the model is configured via `WORKER_MEMORY_MODEL`).

Dependencies: none new (the OpenAI client already present; pgvector is a Postgres extension).


## 2026-06-29 — Approvals, accounts, takeover

**Owner endpoints trust the owner's token, not the web server.** Approving, clearing the
emergency stop and connecting or removing an account are called by the dashboard's browser
directly, with the owner's Supabase access token. The worker has Supabase validate the token
(`/auth/v1/user`: signature, expiry, revocation) and then reads `aal` and the latest `amr`
TOTP timestamp from it (checked against the real local Auth service, ES256 tokens). Approving
and the other sensitive actions need a TOTP code entered in the last 5 minutes; a password-only
session gets 403 on everything. The checks run as dependencies, before the request body is
parsed. Passwords therefore never pass through Vercel. Service-to-service calls (voice agent,
SMS webhook) keep the HMAC signing.

**Approval = one action on one page.** `intent_hash` covers the target's role and name, the
Url path and the money amounts shown on the page, so an approval for a $214.37 total is not
spent on a $219.99 one. It is spent exactly once (`consume` is a conditional UPDATE; a
5-way race test shows one winner), expires after 30 minutes, and the task waits with its
browser kept open. The model is told when the owner declined or the request lapsed.

**Action ledger.** Before an irreversible action (submit, purchase, send, delete, settings),
a `started` row is claimed under a unique index. A second attempt finds it and does nothing.
If the page cannot confirm the outcome (the site took the order but answered too late), the
row becomes `unknown`: the owner is asked and nothing retries it. Only a failure before the
page was touched, which proves nothing happened, frees the slot. Reconciliation by reading a
site's order history is not built; the owner is the reconciler.

**Passwords.** Stored in Bitwarden Secrets Manager (`bitwarden-sdk` 2.1.0, synchronous, so
called in threads); the database keeps an opaque reference. The model names an account and a
field (`fill_secret`), never a value. Before typing, code checks the page origin, the origin
of the frame holding the field, and that it is a real input of the right kind. A mismatch
(a look-alike login page, or a form framed from another site) ends the task as
`security_incident` and is audited. Passwords are masked in everything the model or the logs
could see, and a URL containing one is refused. Verified with the Bitwarden SDK installed;
**not yet run against a real Bitwarden account** (needs an access token).
`UnknownSiteError`: a task naming a site with no saved login does not start.

**Saved logins** are the browser's cookies and storage, AES-256-GCM encrypted with the account
id as associated data (a blob cannot be moved to another account), only for that account's
origins. Key is an environment secret (`WORKER_SESSION_KEY`), and the ciphertext sits in
Postgres; both are simpler and equally private. An undecryptable blob means logging in again,
not a failed task.

**Takeover.** A task parked on a login, CAPTCHA or MFA keeps its Chromium. The dashboard
asks for a single-use 60-second token (stored only as a hash), opens a WebSocket (checked
against the dashboard's Origin, one viewer per task), receives CDP screencast frames and sends
mouse and keyboard input. Nothing typed is logged: the websocket library logs frame contents
at DEBUG, so those loggers are silenced in `configure_logging` (a test caught this).
Pressing Done saves the login and returns the task to the agent.

**Bugs the tests found in earlier work:** the default Supabase table grants silently
overrode column-level privileges (a new revoke-then-grant block fixes it, and `instructions`
too); `audit.events`' `on delete set null` made deleting any user with audit rows fail
(the foreign key is gone; the log keeps the id).

**Red team.** 63 tests: 13 injection phrasings x 4 purchase controls (a named button, a
"Yes!" and a "Complete" button in POST forms, a link) in rotating hiding styles, against a
stand-in model that obeys the page; dangerous URLs (file:, javascript:, metadata IP,
localhost); password leaks through a URL or a look-alike origin. Result: no unauthorised
action, no leaked secret. Removing the POST-form check makes all 26 disguised-label cases
fail. Known limit: a button whose label is harmless and that acts through JavaScript
(`fetch`) rather than a form is not detectable in code; a real model is a second line there.

Dependencies: `cryptography` (AES-GCM), `bitwarden-sdk` (secret storage).


## 2026-04-20 — Browser tasks (read-only): Playwright AI snapshots, typed actions, code-side policy

The model never writes selectors or scripts. Each turn it sees an accessibility snapshot
from Playwright's AI mode (`page.aria_snapshot(mode="ai")`): every node has a `ref`,
including nodes inside shadow roots and iframes (verified in `tests/browser`), and
`aria-ref=<ref>` resolves it. That replaces a custom element walker.

- **Pre-flight.** Before an action, the ref is re-checked against a fresh snapshot. If the
  page re-rendered, the same role and name is accepted only when exactly one node matches;
  two matches or none is reported to the model, never guessed. Playwright's own click checks
  (visible, enabled, not covered) do the rest; an overlay-blocked click fails and is retried.
- **Policy** (`browser/policy.py`) classifies each action from the target's role and name and
  compares it with the task's scope. By default the scope is read, navigate, fill_form: no
  purchase, send, delete, sign-in or settings change without explicit approval.
  A test has the scripted model obey a page that says "press Buy now"; nothing is bought.
  Classification rounds up (unknown target, Enter outside a search box).
- **Destinations.** `browser/guard.py` refuses non-http(s) URLs and hosts that resolve to
  loopback, private or link-local addresses (e.g. the cloud metadata endpoint), for
  navigation and for every sub-request and redirect. Challenge pages and HTTP 403/429/456
  end the task as `needs_user`; nothing tries to get around them.
- **State.** The browser lives in memory; everything the model needs is rebuilt from
  `task_events` (`step`, `step_failed`, `denied`), so a restarted worker reopens the last URL.
  A paused, cancelled or finished task closes its Chromium, which matters on a 1.5 GB host.
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

| Limits | p95 page load | Peak memory | Result |
|---|---|---|---|
| 0.5 vCPU / 1 GB (nf-compute-50, $12) | 15.6 s | 1,026 MB | 1 timeout, memory at ceiling |
| 0.5 vCPU / 1.5 GB (custom, ~$15) | 17.3 s | 1,250 MB | all pages load, slowly |
| 1 vCPU / 2 GB (nf-compute-100-2, $24) | 6.0 s | 998 MB | passes |

Chose the free Sandbox ($20/mo allowance) over paid options, accepting 10–18 s page loads.
Target 0.5 vCPU / 1.5 GB if custom plans are available on the Sandbox (unverified), else
nf-compute-50. Consequences: one browser task at a time; navigation timeouts of 60 s.
Revisit if task failure rates from timeouts are high: 1 vCPU / 2 GB is the measured fix.


## 2026-03-22 — Headed Chromium on Xvfb, not headless

Headless Chromium identifies itself as `HeadlessChrome`; from a residential IP,
walmart.ca (HTTP 456), canadiantire.ca (aborted) and bestbuy.ca (403) refused it, and
amazon.ca served a challenge page. Headed Chromium loaded walmart.ca and canadiantire.ca
fully. amazon.ca and bestbuy.ca still challenge it; those need user takeover. Faking the
user agent or other fingerprints is out of scope (no evasion).

`with-display.sh` starts Xvfb itself because `xvfb-run` hangs as a container's PID 1.


## 2026-03-08 — Durable tasks: DBOS Transact 3.4.0, in the existing Postgres

One workflow per task (`worker/engine.py`), queued with a global concurrency of 1 because
the free worker runs one browser at a time. Chosen over Temporal (cluster to run or paid
cloud) and Inngest (steps over HTTP, awkward when one process holds a browser). Pinned to
3.4.0, not the 3.5.0 published the day before. Checked against the installed package:

- DBOS creates its `dbos` schema itself, which the restricted `agent_worker` role cannot do,
  so the `task_engine` migration creates it, owned by that role.
- `register_queue` must be awaited from async code (`register_queue_async`).
- `send` to a finished or unknown workflow raises; the engine treats that as "nothing to wake".

Design points:
- The `instructions` table is the inbox and the source of truth; `DBOS.send` is only a wake-up
  so a paused task reacts at once instead of at its next 60 s poll. A text that arrives while
  the worker is down is applied on restart.
- Steps are at-least-once: after a kill, the step that was in flight may run again, completed
  steps do not (`tests/test_durability.py` kills a real worker process to show it).
- A paused task keeps the single queue slot, so later tasks wait behind it.
- RESUME after a budget pause grants one more default allowance ($3). A text can therefore
  authorise at most that much extra spend per resume.
- HALT ALL sets `emergency_stop`; only the dashboard may clear it, so a spoofed text can stop
  work but never restart it.
- Task verbs avoid carrier opt-out words: `kill` (not cancel/stop), `halt` = pause.


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

New dependencies:
- worker: `fastapi` + `uvicorn` (internal API), `psycopg[binary,pool]` (Postgres), `httpx`
  (Twilio REST); dev `pytest-asyncio`.
- `services/common` (`agent-common`): signing and the API's Pydantic contracts, shared so the
  voice agent and worker cannot drift. Depends only on `pydantic`.
- voice: `livekit-agents[deepgram,openai,silero]` 1.8 (Flux STT, gpt-4.1, Aura-2 TTS, VAD for
  barge-in), `livekit-plugins-noise-cancellation` (BVCTelephony), `httpx`, `pydantic-settings`.
- web: `server-only` (keeps server env access out of client bundles); dev `@playwright/test`.
  Node 22+ is required (`@supabase/supabase-js` needs native WebSocket).


## 2026-01-13 — Worker dependencies

- `playwright` — browser automation; pinned with the matching `mcr.microsoft.com/playwright/python` image.
- `pydantic-settings` — validated configuration; rejects unknown `WORKER_*` variables.
- `logfire` — tracing/log export to Pydantic Logfire (free tier); only active when a token is set.
- dev: `pytest`, `ruff`, `mypy` — tests, lint/format, strict typing.


## 2026-01-05 — Database: Supabase (hosted project "Computer Use Voice Agent", us-east-1)

Postgres + Auth (TOTP MFA is free; phone MFA is a paid add-on and not used) + Storage +
Realtime. Every table requires an `aal2` (MFA-verified) session via a restrictive RLS
policy. Migrations are tested locally (`supabase test db`) before being applied.
