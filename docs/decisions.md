# Decisions

Checked November 3, 2026. Each decision names its evidence; inference is labelled.

**1. A hand-written HTTP layer instead of an SDK.** Rippling's documentation says to use its "TypeScript, Python, or Go SDK", but no public Python REST SDK could be found: 21 likely package names on PyPI returned 404, and Rippling's public GitHub organisation has no Python or Go REST client. Rippling's only Python packages on PyPI (`rippling-flux-sdk`, `rippling-flux-dev-tools`) define Flux app interfaces, not a REST client. The layer is small (paging, version header, retries, errors), so writing it keeps every documented rule visible and tested. Revisit when a public Python SDK appears.

**2. httpx2 for HTTP.** httpx gives one sync API with an in-process `MockTransport`, so the fake Rippling runs without a network. httpx itself sees "limited activity", and Pydantic maintains httpx2 as its continuation with timely security updates; its only breaking change is the import name (httpx2 changelog). Starlette 1.x also deprecates plain httpx in its test client in favour of httpx2.

**2a. uv for environments and the lockfile.** `uv.lock` is committed and CI installs with `uv sync --locked`, so every run uses the same versions (uv documentation, GitHub Actions guide). Development tools live in a PEP 735 `[dependency-groups]` table, so they are not published as an install option of the package.

**2b. A connection pool for the web app.** The install endpoints borrow connections from a psycopg pool opened at startup and closed at shutdown, as psycopg's documentation recommends for web applications; the pool is created closed and opened explicitly, because the default of its `open` argument may change.

**3. Retry only what Rippling documents.** 429, 500, and 504 retry; 400, 401, 403, 404, 409, 422, 502, and 503 do not, because the troubleshooting page does not list them as retryable (409 is "maybe" there; this sample treats it as a caller decision). `Retry-After` is honoured as seconds or an HTTP date and capped; without it, full-jitter exponential backoff. Every call has an attempt budget and a deadline, so a retry never sleeps past the caller's patience. Whether Rippling always sends `Retry-After` on a 429 is not documented (inference: honour it when present).

**4. Refresh under a row lock.** A used refresh token is revoked (with a one-hour grace), so two workers refreshing at once could each spend it. The token row is locked with `SELECT ... FOR UPDATE`; the second caller waits and reads the fresh pair. A test runs four concurrent callers and expects one refresh.

**5. Form encoding for the token request.** Rippling's example uses `curl -F` (multipart). This sample sends `application/x-www-form-urlencoded`, the format RFC 6749 section 4.1.3 requires. That Rippling's endpoint accepts it is inference from the standard; confirm with a partner test company before production.

**6. `state` only for installs started by the partner, and why that is safe here.** Installs that start in the App Shop arrive without a `state`; installs started from the partner's `/install` carry one that must come back unchanged, expires after ten minutes, and is single use (RFC 6749 section 4.1.1). Rippling's documentation does not describe `state` (inference). Accepting a callback without `state` is a cross-site request forgery risk only when the callback links the installing company to a signed-in partner user; this sample has no partner users, so a forged callback can only install the attacker's own company, which the attacker could do directly. An app that adds partner accounts must require the signed-in user to start or confirm the install.

**7. Reconcile only after a complete listing, and refuse an empty one.** Marking workers removed after a partial listing would remove real people. An empty complete listing is more likely a permissions or token problem than a company with no workers, so it fails loudly. Removal is one `NOT EXISTS` over the unnested set of seen ids, which stays a hash anti-join whatever its size.

**8. Skip unchanged rows.** Each worker's canonical JSON is hashed; the upsert's `WHERE` clause skips rows whose hash is unchanged and not removed, so an identical sync writes nothing.

**9. Mark installed first, store tokens after.** Tokens and the install record are written in one transaction only after `mark_app_installed` succeeds, so a 403 or an outage there leaves no usable tokens behind for the sync to pick up.

**10. One sync per company at a time.** A per-company advisory lock refuses an overlapping run, because a run whose listing finished before a new hire could otherwise reconcile after another run inserted that hire and mark them removed.

**11. Cache the access token in memory.** The CLI asks the token store only when the cached token is within five minutes of expiry, so a long listing does not take a row lock and decrypt a token on every page.

**12. v2 for data, V1 only where the install guide still requires it.** `GET /companies` uses v2; `mark_app_installed` is still documented on `api.rippling.com/platform/api`, so it is called there.
