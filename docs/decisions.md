# Decisions

One entry per decision or dependency, with the reason. Newest first.

## 2026-11-11 — Prices, replies by text, push, live updates, perception evals

**Why canadiantire.ca showed no prices.** Its own price service (`.../PriceAvailability`) answers
this browser with HTTP 403 (bot protection), so the page never receives prices; the search and
product pages otherwise load. One earlier run did load prices, so the refusal is intermittent.
There is no fix that is not evasion, and none was attempted. What changed: the session reports
"the site refused a background request (403)" to the model, which now says prices were
withheld after 2 steps (1.8 cents) instead of searching for 13 steps (27 cents). A real person
taking over the browser may let the site relax; that is untested.

**Cost of a page.** A shop search page is ~190,000 characters of accessibility tree. The model
sees `snapshot.compact` of it, capped at 24,000 characters: text already in a link's name is not
repeated in nested elements, empty wrappers, cursor hints and tracking query strings are dropped,
long boilerplate lines are cut at 200 characters. That removed 40% of the size with no
interactive element lost (checked on the real page and in tests).

**Late content.** Shops draw prices after the network is quiet. Before each look the session waits
until the page text has not changed for 1.5 seconds (at most 8). The cost is up to 1.5 seconds per
step; a page that draws later than that shows as "Loading" and the model can wait.

**Reading pages (30 labelled pages, live model).** First run 58% correct: the tool-less reader was
given `innerText`, which skips shadow DOM and iframes and does not show a ticked checkbox or a
selected option, and the quiet wait was 1 second. Reading from the accessibility tree and the
longer wait gave 96% of the 26 answerable questions and 4 of 4 absent answers reported as absent
(cost 1.4 cents). The remaining miss is content that draws after 2.5 seconds. PDFs are not
read at all (the browser's PDF viewer is not handled); the pages are synthetic, not saved
real ones.

**Multi-store comparison, live.** "1TB NVMe SSD on canadacomputers.com, newegg.ca, staples.ca":
real prices and stock status from all three, 19 model calls, 34 cents; Staples' refused
background request was reported. A drill comparison on bestbuy.ca (Access Denied), homedepot.ca
(no usable prices) and staples.ca returned the Staples prices and said what was missing; a run
including memoryexpress.com stopped at its Cloudflare check and handed over to the owner.

**Replies by text.** Any text that is not a command is, for the one active task (or `T7 ...`),
either the answer to the task's question (if it is waiting to ask one) or a note, stored as an
instruction and applied at the next step; notes are added to the task's spec as `owner_edits`,
which the model reads as later instructions from the owner. `yes`/`approve` while a task awaits
approval is answered with a pointer to the dashboard: texts can be spoofed and never approve.
Carrier opt-out words are never notes. During a call the voice agent has `edit_task`.
**Voice versus text.** Free text has no slots to compare, so nothing here can tell whether two
notes contradict. Two notes from different channels within 60 seconds make the task stop and
ask ("1) by voice: ... 2) by text: ... reply 1, 2 or 3"); until the owner answers, neither
applies. That asks more often than strictly needed, by design. Purchases and sends still need
the dashboard approval either way.

**After a takeover.** Pressing Done is refused once if the page still shows a verification page
or a password field; "Continue anyway" overrides it.

**Browsers.** Every 5 minutes the worker closes browsers whose task is no longer running or
waiting for the owner. It does not scan for Chromium processes of a crashed worker; Playwright
ties a browser's life to its parent.

**Live dashboard.** The task list, task page and approvals subscribe to Supabase Realtime
(`tasks`, `task_events`, `approvals`, `alerts`) and re-render on change; row-level security and
the aal2 rule apply to what a session receives.

**Web push.** Alongside every text (approval needed, login needed, task done, alerts) the worker
pushes the same message to the browsers the owner enabled; the dashboard's "Turn on
notifications" registers one. Texts stay the base channel (no timer-based fallback from push to
text, by decision). Dependency: `pywebpush` (VAPID signing and payload encryption; pulls in
`aiohttp`, `http-ece`, `py-vapid`). Generate keys with `scripts/generate-vapid-keys.py`. Not
verified: delivery to a real browser (needs a deployed worker and a VAPID key pair); on iPhone
push works only for a dashboard added to the home screen, and the manifest has no icons yet.

**Pictures.** The agent works from text; a picture costs several ordinary steps, so it is the
model's choice, helped by two cheap signals from the page: the browser notes say when the page
holds a drawn canvas, a large graphic, or a large image with no description, and when the text
view is under 250 characters. A hint alone is not a reason to look (the prompt says so, and a
live test checks a page with a chart and a price in text). `look` photographs the viewport and a
separate tool-less model answers one question about it, as `extract` does for text, so only its
answer reaches the main model and an instruction written inside an image cannot steer the
browser. Limits in code: 6 pictures per task, and none of a page that has not changed since
the last one. Never taken where a sign-in form or a value typed from the secret store is
visible; password fields are masked. Measured with the real model: it asked for a picture of a
canvas-painted figure and read it correctly; it did not ask on a text page, nor on a text page
that also had a chart. Pictures are not saved or shown in the dashboard yet.

**Deploy images.** Both Dockerfiles build from a folder that holds `common/` and the service side
by side, because each service's pyproject resolves the shared package as `../common`. Worker:
`docker build -f services/worker/Dockerfile services` (its default command is now the API
server; the host-sizing check is `browser-smoke` as the command). Voice:
`scripts/stage-voice.sh <folder>` copies both into one folder for `lk agent create`, which
builds it on LiveKit Cloud. Both images were built and started locally (the worker answered
`/healthz`; the voice image imports). Not done: a run of either image against real accounts.
The worker reaches the hosted database through Supabase's session pooler (IPv4; the direct host
is IPv6-only on the free plan) as the `agent_worker` role, whose password is set out of band.


## 2026-09-25 — Self-health alerts and task limits

**No fallbacks, by decision.** A failure is reported and the work stops; nothing switches to
an alternate provider, host or route on its own. This replaces speech-provider fallback,
host switching and similar paths. The GPT-4.1 / 4.1-mini replacement is also
dropped by the owner's choice; both models shut down on 2027-01-14, after which the voice
agent and memory extraction stop working unless the model settings are changed.

**Alerts** (`health.py`): every 10 minutes the worker checks, per user, for 3+ failed tasks in
a day, 2+ unexpected errors, any security stop, undelivered texts (failed, opted out, no
number), a quarter of the monthly cap spent in a day, and 80% of the cap spent in the month.
An alert opens once per kind, is texted once, shows on the dashboard task list and closes
when the condition clears. The alert text can itself fail to send; the dashboard shows it
regardless. Provider errors are counted through the failed tasks they cause, not separately.

**Limits.** The monthly cap (`user_settings.monthly_cost_cap_cents`, default $150) now pauses
a task before its next step and blocks `RESUME` while it is reached. Each task also pauses
after 2 hours of working time (time spent in the executor, not waiting for approval or you);
`RESUME` grants another 2 hours.

**Proved locally.** Five simultaneous calls of 20 turns (110 requests): median 4 ms, p95 20
ms, all turns stored. Three tasks queue and run one at a time in creation order. A browser that
dies mid-task is replaced and the task continues from the last page. A backup restore drill
(`scripts/restore-drill.sh`) restores the application schemas and matches row counts.
The load numbers are the worker alone with no network and no speech or model latency.

**Not done.** Backups on the hosted project (needs the Pro plan), and a restore drill against
a hosted backup. A load test over real phone calls (needs Twilio and LiveKit). The
interrupt-and-correct check is covered only as far as the conversation-state
tests go (slot corrections); that only the played part of an interrupted sentence is stored
is not tested, because it lives in the LiveKit session. A watchdog for
orphaned Chromium processes.
Runbooks are in `docs/runbooks.md`.


## 2026-08-26 — Long-term memory

**What is stored.** `memories` (fact / preference / rule, one sentence each, with an embedding
and a keyword vector), `memory_sources` (the owner's own words each came from), `episodes`
(a sentence per call or task), `procedures` (the last route that worked on a site) and
`memory_events` (hashes only). pgvector 0.8 in the same Postgres; no separate vector store.
No `subject / predicate / value` columns: nothing used them, and the sentence is what is
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
