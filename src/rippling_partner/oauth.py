"""OAuth 2.0 authorization-code install and rotating refresh tokens for a Rippling partner app.

From Rippling's installation guide (developer-portal/v2-guides/installation, checked 2026-11-03):
- the authorization code is valid for 300 seconds and can be used a single time;
- the token endpoint is ``https://api.rippling.com/api/o/token/``, called with HTTP Basic client
  credentials and ``grant_type``, ``code``, and ``redirect_uri``;
- "Refresh tokens are revoked once they are used", with a one-hour grace period, so the new pair
  must replace the old one atomically;
- one access token gives access to one Rippling company.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
import psycopg
from cryptography.fernet import Fernet

from rippling_partner.client import RipplingAPIError, parse_error

TOKEN_URL = "https://api.rippling.com/api/o/token/"
AUTHORIZE_URL = "https://app.rippling.com/apps/PLATFORM/{app_name}/authorize"
REFRESH_MARGIN = timedelta(minutes=5)


@dataclass(frozen=True)
class OAuthApp:
    client_id: str
    client_secret: str
    app_name: str
    redirect_uri: str


@dataclass(frozen=True)
class TokenSet:
    access_token: str
    refresh_token: str
    expires_at: datetime
    scope: str


def authorize_url(app: OAuthApp, state: str) -> str:
    """The URL that starts an install from the partner's side."""
    query = urlencode(
        {
            "response_type": "code",
            "client_id": app.client_id,
            "redirect_uri": app.redirect_uri,
            "state": state,
        }
    )
    return f"{AUTHORIZE_URL.format(app_name=app.app_name)}?{query}"


def _token_request(
    http: httpx.Client, app: OAuthApp, form: dict[str, str], now: datetime
) -> TokenSet:
    response = http.post(TOKEN_URL, data=form, auth=(app.client_id, app.client_secret))
    if not response.is_success:
        raise parse_error(response)
    try:
        body = response.json()
    except ValueError as exc:
        raise RipplingAPIError(502, "token response is not JSON") from exc
    missing = [k for k in ("access_token", "refresh_token", "expires_in") if k not in body]
    if missing:
        raise RipplingAPIError(502, f"token response is missing {', '.join(missing)}")
    return TokenSet(
        access_token=body["access_token"],
        refresh_token=body["refresh_token"],
        expires_at=now + timedelta(seconds=int(body["expires_in"])),
        scope=body.get("scope", ""),
    )


def exchange_code(http: httpx.Client, app: OAuthApp, code: str, now: datetime) -> TokenSet:
    return _token_request(
        http,
        app,
        {"grant_type": "authorization_code", "code": code, "redirect_uri": app.redirect_uri},
        now,
    )


def refresh(http: httpx.Client, app: OAuthApp, refresh_token: str, now: datetime) -> TokenSet:
    return _token_request(
        http, app, {"grant_type": "refresh_token", "refresh_token": refresh_token}, now
    )


class CachedToken:
    """An access-token provider that asks the store only when the cached token nears expiry.

    Without it, every page request would take a row lock and decrypt a token that is valid
    for hours.
    """

    def __init__(self, fetch: Callable[[], TokenSet], clock: Callable[[], datetime]) -> None:
        self._fetch = fetch
        self._clock = clock
        self._tokens: TokenSet | None = None

    def __call__(self) -> str:
        if self._tokens is None or self._tokens.expires_at - REFRESH_MARGIN <= self._clock():
            self._tokens = self._fetch()
        return self._tokens.access_token


class TokenStore:
    """One token pair per company, encrypted at rest; refreshes serialise on the company's row."""

    def __init__(self, key: bytes) -> None:
        self._fernet = Fernet(key)

    def save(self, conn: psycopg.Connection[Any], company_id: str, tokens: TokenSet) -> None:
        with conn.transaction():
            self._upsert(conn, company_id, tokens)

    def access_token(
        self,
        conn: psycopg.Connection[Any],
        company_id: str,
        http: httpx.Client,
        app: OAuthApp,
        now: datetime,
    ) -> str:
        """A usable access token, refreshing first if it expires within five minutes."""
        return self.current(conn, company_id, http, app, now).access_token

    def current(
        self,
        conn: psycopg.Connection[Any],
        company_id: str,
        http: httpx.Client,
        app: OAuthApp,
        now: datetime,
    ) -> TokenSet:
        """The company's usable tokens, refreshed first if they expire within five minutes.

        The row lock makes concurrent callers wait for one refresh instead of each spending the
        single-use refresh token.
        """
        with conn.transaction():
            row = conn.execute(
                "SELECT access_token_enc, refresh_token_enc, expires_at, scope FROM oauth_tokens "
                "WHERE company_id = %s FOR UPDATE",
                (company_id,),
            ).fetchone()
            if row is None:
                raise LookupError(f"no tokens stored for company {company_id}; install first")
            access_enc, refresh_enc, expires_at, scope = row
            refresh_token = self._fernet.decrypt(bytes(refresh_enc)).decode()
            if expires_at - REFRESH_MARGIN > now:
                access = self._fernet.decrypt(bytes(access_enc)).decode()
                return TokenSet(access, refresh_token, expires_at, scope)
            fresh = refresh(http, app, refresh_token, now)
            self._upsert(conn, company_id, fresh)
            return fresh

    def _upsert(self, conn: psycopg.Connection[Any], company_id: str, tokens: TokenSet) -> None:
        conn.execute(
            "INSERT INTO oauth_tokens (company_id, access_token_enc, refresh_token_enc, expires_at,"
            " scope, updated_at) VALUES (%s, %s, %s, %s, %s, now()) ON CONFLICT (company_id) DO"
            " UPDATE SET access_token_enc = excluded.access_token_enc, refresh_token_enc ="
            " excluded.refresh_token_enc, expires_at = excluded.expires_at, scope ="
            " excluded.scope, updated_at = now()",
            (
                company_id,
                self._fernet.encrypt(tokens.access_token.encode()),
                self._fernet.encrypt(tokens.refresh_token.encode()),
                tokens.expires_at,
                tokens.scope,
            ),
        )
