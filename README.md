# rippling-worker-sync

A small Python sample of the first two things a Rippling App Shop partner integration has to get right: **installing through OAuth** and **keeping a local copy of a company's workers correct**. Every behaviour below follows Rippling's published v2 documentation, and each one has a test.

Not affiliated with Rippling. It has **not** been run against a live Rippling company: Rippling issues partner test companies only to approved App Shop partners, so the tests run against a local fake built from the documented contract (`tests/fake_rippling.py`) and a real Postgres.

## What it does

| Behaviour | Rippling documentation (checked 2026-11-03) | Test |
|---|---|---|
| Pin `Rippling-Api-Version` (a date) on every request | [Versioning](https://developer.rippling.com/documentation/rest-api/essentials/versioning) | `test_every_request_pins_the_version_and_sends_the_bearer_token` |
| Page with `limit` 1 to 100 and follow `next_link` as-is until null | [Pagination](https://developer.rippling.com/documentation/rest-api/essentials/pagination), [Usage and limits](https://developer.rippling.com/documentation/rest-api/essentials/api-limits) | `test_paginate_follows_next_link_until_null`, `test_limit_outside_documented_range_raises_before_any_request` |
| Retry 429, 500, 504 with `Retry-After` or backoff; never retry 400, 401, 403, 404, 422 | [Usage and limits](https://developer.rippling.com/documentation/rest-api/essentials/api-limits), [Troubleshooting](https://developer.rippling.com/documentation/rest-api/essentials/troubleshooting) | `test_429_*`, `test_documented_server_errors_are_retried`, `test_other_statuses_are_not_retried` |
| Keep `x-rippling-request-id` and read all four live error shapes | [Troubleshooting](https://developer.rippling.com/documentation/rest-api/essentials/troubleshooting) | `test_each_live_error_shape_parses` |
| Exchange the 300-second, single-use code with Basic client credentials | [Installation](https://developer.rippling.com/documentation/developer-portal/v2-guides/installation) | `test_code_exchange_uses_basic_auth_and_the_exact_form_fields` |
| Replace the refresh token atomically, since a used one is revoked | [Installation](https://developer.rippling.com/documentation/developer-portal/v2-guides/installation) | `test_concurrent_callers_refresh_once` |
| Bind the token to its one company, then mark the app installed; a 403 leaves it unmarked | [Installation](https://developer.rippling.com/documentation/developer-portal/v2-guides/installation), [Errors](https://developer.rippling.com/documentation/developer-portal/v2-guides/errors) | `test_install_binds_the_company_saves_tokens_marks_installed_and_returns`, `test_a_403_while_binding_the_company_leaves_the_install_unmarked` |
| Redirect URLs are https only | [Redirect URLs](https://developer.rippling.com/documentation/developer-portal/v2-guides/redirect) | `test_redirect_uri_must_be_https` |

Beyond the documentation, from the author's own practice:

- **Sync without needless writes.** Workers are upserted in set-based batches that skip rows whose content has not changed; a second identical sync writes nothing.
- **Reconcile only on a complete listing.** After a listing that reached `next_link: null`, workers absent from it are marked removed in one anti-join statement. A listing that fails part way removes nobody, and an empty complete listing is refused rather than read as "everyone left".
- **No token leaks.** A `next_link` that points off `rest.ripplingapis.com` is refused, so the bearer token never goes to another host; a return URL off `app.rippling.com` is not followed.
- **Tokens encrypted at rest** (Fernet), one pair per company, refreshed under a row lock.

## Not done

SSO (SAML or OpenID), user provisioning, webhooks, the 24-hour scheduled sync and manual-sync button that the [partner requirements](https://developer.rippling.com/documentation/developer-portal/getting-started/requirements) ask for, a UI, and any run against a live Rippling company.

## Run it

```sh
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
docker run -d --name rws-pg -e POSTGRES_PASSWORD=test -e POSTGRES_DB=rws -p 5432:5432 postgres:17.10
export DATABASE_URL=postgresql://postgres:test@localhost:5432/rws
ruff check . && ruff format --check . && mypy && pytest
```

To use it for real you need App Shop partner credentials: set `TOKEN_ENCRYPTION_KEY` (a Fernet key), `RIPPLING_CLIENT_ID`, `RIPPLING_CLIENT_SECRET`, `RIPPLING_APP_NAME`, `RIPPLING_REDIRECT_URI` (https), and `RIPPLING_API_VERSION`, then:

```sh
pip install -e ".[serve]"
python -m rippling_sync migrate
uvicorn --factory rippling_sync.web:app        # install endpoints: /install, /oauth/callback
python -m rippling_sync sync --company <company id>
```

## Layout

- `src/rippling_sync/client.py`: version pinning, cursor paging, retries, error parsing
- `src/rippling_sync/oauth.py`: authorize URL, code exchange, rotating refresh, encrypted token store
- `src/rippling_sync/install.py`: the install endpoints
- `src/rippling_sync/sync.py`: worker sync and reconciliation
- `docs/spec.md`: what was agreed before code; `docs/decisions.md`: why it is built this way

## Licence

MIT. Built by Swaraj Chandra Reddy M.
