# Spec: rippling-worker-sync

Status: agreed November 3, 2026. Scope, test target, publishing, and SDK choice were chosen by the author from options with recommendations; everything below them is derived from Rippling's published documentation, fetched November 3, 2026.

## Purpose

A small, public Python sample that does what a Rippling App Shop partner integration must do first: install through OAuth, then keep a local copy of a company's workers correct. Every behaviour cites the Rippling page that defines it. It is not affiliated with Rippling and has not run against a live Rippling company.

## Scope

In:

1. **Install (OAuth 2.0 authorization code).** Start from the app or from Rippling; verify `state`; exchange the code within its 300-second, single-use window at `https://api.rippling.com/api/o/token/` with HTTP Basic client credentials and form fields `grant_type`, `code`, `redirect_uri`; bind the token to its company with `GET https://rest.ripplingapis.com/companies`; then `POST https://api.rippling.com/platform/api/mark_app_installed`. A 403 during install means the install is not marked successful.
2. **Tokens.** One token set per company. Refresh with `grant_type=refresh_token` before expiry. Refresh tokens are revoked once used (one-hour grace), so the new pair is stored in the same transaction that reads the old one; a concurrent refresh for the same company waits on a row lock.
3. **API client.** Bearer token; `Rippling-Api-Version` pinned on every request; list endpoints followed by `next_link` until it is null; `limit` validated to 1..100 before sending; retries only on 429, 500, and 504, honouring `Retry-After` (seconds or HTTP date, capped) and otherwise full-jitter exponential backoff, within an attempt budget and an overall deadline; never retries 400, 401, 403, 404, or 422; every raised error carries the status, the parsed error body (any of the four live shapes), and `x-rippling-request-id`.
4. **Worker sync.** List every worker for a company (`GET /workers`, `limit=100`), upsert each into Postgres in batches with `INSERT ... ON CONFLICT DO UPDATE ... WHERE` the content changed, so an unchanged worker is not rewritten. After a listing that reached `next_link: null`, reconcile: mark workers absent from that complete listing as removed, in one set-based statement. A listing that stopped early never reconciles, and an empty complete listing is refused rather than treated as "remove everyone".
5. **Run record.** Each sync writes a run row: started, finished, outcome, pages, workers seen, upserted, unchanged, removed.

Out (stated in the README as not done): SSO (SAML or OpenID), user provisioning, webhooks, the 24-hour scheduler and manual-sync button, a UI, multi-region deployment, and any live-Rippling run.

## Interfaces

- `rippling_sync.client.RipplingClient(token_provider, api_version, *, transport=None)`: `get(path, params)`, `paginate(path, params) -> Iterator[dict]`.
- `rippling_sync.oauth`: `authorize_url(app_name, client_id, redirect_uri, state)`, `exchange_code(...)`, `refresh(...)`; `TokenStore` (Postgres; refresh tokens encrypted at rest with Fernet, key from the environment).
- `rippling_sync.install.app`: a Starlette app with `GET /install` and `GET /oauth/callback`.
- `rippling_sync.sync.sync_workers(conn, client, company_id) -> RunResult`.
- CLI: `python -m rippling_sync sync --company <id>`.

## Data model (Postgres)

- `oauth_tokens(company_id pk, access_token_enc, refresh_token_enc, expires_at, scope, updated_at)`
- `workers(company_id, worker_id, payload jsonb, content_hash, first_seen_run, last_seen_run, removed_at, pk(company_id, worker_id))`
- `sync_runs(run_id pk, company_id, started_at, finished_at, outcome, pages, seen, upserted, unchanged, removed)`

## Tests (written first, each watched failing for the right reason)

Against a local fake of the documented contract (httpx `MockTransport`) and a real Postgres:

- pagination follows `next_link` to null and stops; a `limit` outside 1..100 raises before any request
- the version header is on every request
- 429 with `Retry-After` waits that long (capped); 429 without it backs off; 500 and 504 retry; 400, 401, 403, 404, 422 do not; the budget and the deadline end retries with the last error
- each of the four error shapes parses, and the request id is kept
- code exchange sends Basic auth and the exact form fields; a mismatched `state` is rejected before any exchange
- refresh stores the new pair atomically; two concurrent refreshes for one company produce one exchange
- install marks installed only after the company is bound; a 403 at any step leaves it unmarked
- sync is idempotent (a second identical run upserts nothing); a changed worker updates; a worker missing from a complete listing is marked removed; a run that fails mid-listing removes nobody; an empty complete listing is refused

## Acceptance

- `ruff check`, `ruff format --check`, `mypy --strict src`, and `pytest` pass locally and in GitHub Actions (Postgres service container; actions pinned to commit SHAs).
- The README maps each behaviour to its Rippling documentation URL and the date it was checked, and states what is not done.
- `docs/decisions.md`, one page: why a hand-written HTTP layer (no public Python REST SDK found on November 3, 2026), the retry policy source, token rotation, and reconciliation guards.
- An `AGENTS.md` that points coding agents at Rippling's `llms.txt` and this repo's checks.
- Nothing from the author's employer: no code, data, names, or identifiers.
