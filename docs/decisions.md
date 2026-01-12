# Decisions

One entry per decision or dependency, with the reason. Newest first.

## 2026-01-13 — Worker dependencies

- `playwright` — browser automation; pinned with the matching `mcr.microsoft.com/playwright/python` image.
- `pydantic-settings` — validated configuration; rejects unknown `WORKER_*` variables.
- `logfire` — tracing/log export to Pydantic Logfire (free tier); only active when a token is set.
- dev: `pytest`, `ruff`, `mypy` — tests, lint/format, strict typing.


## 2026-01-05 — Database: Supabase (hosted project "Computer Use Voice Agent", us-east-1)

Postgres + Auth (TOTP MFA is free; phone MFA is a paid add-on and not used) + Storage +
Realtime. Every table requires an `aal2` (MFA-verified) session via a restrictive RLS
policy. Migrations are tested locally (`supabase test db`) before being applied.
