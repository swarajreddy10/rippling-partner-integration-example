"""The install endpoints a Rippling App Shop partner app exposes.

Flow, from Rippling's installation guide (developer-portal/v2-guides/installation, checked
2026-11-03): exchange the code for tokens, bind the token to its one company with
``GET /companies``, then call ``mark_app_installed`` and return the user to Rippling. A 403 at any
step means the install is not complete, so nothing is marked (developer-portal/v2-guides/errors).

``state`` is issued for installs that start from the partner's side and must come back unchanged
(RFC 6749, section 4.1.1). Installs that start in the Rippling App Shop arrive without one.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import psycopg
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from rippling_sync.client import RipplingAPIError, RipplingClient, parse_error
from rippling_sync.oauth import OAuthApp, TokenStore, authorize_url, exchange_code

MARK_INSTALLED_URL = "https://api.rippling.com/platform/api/mark_app_installed"
RETURN_HOST = "app.rippling.com"


def create_app(
    *,
    oauth_app: OAuthApp,
    store: TokenStore,
    connect: Callable[[], psycopg.Connection[Any]],
    api_version: str,
    transport: httpx.BaseTransport | None = None,
    clock: Callable[[], datetime] | None = None,
    state_ttl: timedelta = timedelta(minutes=10),
) -> Starlette:
    now = clock or (lambda: datetime.now(UTC))
    default_return = f"https://{RETURN_HOST}/apps/PLATFORM/{oauth_app.app_name}/return"

    def install(request: Request) -> Response:
        state = secrets.token_urlsafe(32)
        with connect() as conn:
            # States live in database time; abandoned ones are purged here, so the table stays
            # bounded by the issue rate within one TTL.
            conn.execute("DELETE FROM oauth_states WHERE created_at <= now() - %s", (state_ttl,))
            conn.execute("INSERT INTO oauth_states (state) VALUES (%s)", (state,))
        return RedirectResponse(authorize_url(oauth_app, state), status_code=302)

    def callback(request: Request) -> Response:
        code = request.query_params.get("code")
        if not code:
            return PlainTextResponse("missing code", status_code=400)
        state = request.query_params.get("state")
        with connect() as conn:
            if state is not None:
                used = conn.execute(
                    "DELETE FROM oauth_states WHERE state = %s AND created_at > now() - %s"
                    " RETURNING 1",
                    (state, state_ttl),
                ).fetchone()
                conn.commit()
                if used is None:
                    return PlainTextResponse("unknown or expired state", status_code=400)
            with httpx.Client(transport=transport, timeout=30.0) as http:
                try:
                    tokens = exchange_code(http, oauth_app, code, now())
                    rippling = RipplingClient(
                        lambda: tokens.access_token, api_version, transport=transport
                    )
                    try:
                        companies = list(rippling.paginate("/companies", limit=100))
                    finally:
                        rippling.close()
                    if len(companies) != 1:
                        return PlainTextResponse(
                            f"expected exactly one company for this token, got {len(companies)}",
                            status_code=502,
                        )
                    company_id = str(companies[0]["id"])
                    # Mark first, persist after: tokens are kept only for a completed install.
                    marked = http.post(
                        MARK_INSTALLED_URL,
                        headers={"Authorization": f"Bearer {tokens.access_token}"},
                    )
                    if not marked.is_success:
                        raise parse_error(marked)
                except RipplingAPIError as err:
                    status = 403 if err.status == 403 else 502 if err.status >= 500 else 400
                    return PlainTextResponse(f"install not completed: {err}", status_code=status)
                except httpx.TransportError as err:
                    return PlainTextResponse(
                        f"install not completed: could not reach Rippling ({err})",
                        status_code=502,
                    )
            with conn.transaction():
                store.save(conn, company_id, tokens)
                conn.execute(
                    "INSERT INTO installs (company_id) VALUES (%s) ON CONFLICT"
                    " (company_id) DO UPDATE SET installed_at = now()",
                    (company_id,),
                )
        return RedirectResponse(
            _return_url(request.query_params.get("redirect_uri"), default_return), status_code=302
        )

    return Starlette(routes=[Route("/install", install), Route("/oauth/callback", callback)])


def _return_url(candidate: str | None, default: str) -> str:
    """Follow Rippling's return URL only when it is HTTPS on app.rippling.com (no open redirect)."""
    if candidate:
        try:
            url = httpx.URL(candidate)
        except httpx.InvalidURL:
            return default
        if url.scheme == "https" and url.host == RETURN_HOST and not url.userinfo:
            return candidate
    return default
