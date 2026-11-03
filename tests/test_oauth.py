"""OAuth exchange and rotating refresh tokens, per Rippling's installation guide."""

from __future__ import annotations

import base64
import threading
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import psycopg
import pytest
from fake_rippling import FakeRippling

from rippling_sync.oauth import OAuthApp, TokenSet, TokenStore, authorize_url, exchange_code

APP = OAuthApp("cid", "secret", "acme_sync", "https://partner.example/oauth/callback")
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


def test_authorize_url_carries_the_documented_parameters() -> None:
    url = urlsplit(authorize_url(APP, "st-1"))
    assert (url.scheme, url.netloc, url.path) == (
        "https",
        "app.rippling.com",
        "/apps/PLATFORM/acme_sync/authorize",
    )
    assert parse_qs(url.query) == {
        "response_type": ["code"],
        "client_id": ["cid"],
        "redirect_uri": ["https://partner.example/oauth/callback"],
        "state": ["st-1"],
    }


def test_code_exchange_uses_basic_auth_and_the_exact_form_fields() -> None:
    fake = FakeRippling()
    with httpx.Client(transport=fake.transport()) as http:
        tokens = exchange_code(http, APP, "code-1", NOW)
    sent = fake.token_requests[0]
    assert sent["auth"] == "Basic " + base64.b64encode(b"cid:secret").decode()
    assert {k: sent[k] for k in ("grant_type", "code", "redirect_uri")} == {
        "grant_type": "authorization_code",
        "code": "code-1",
        "redirect_uri": APP.redirect_uri,
    }
    assert tokens.access_token == "tok-2" and tokens.refresh_token == "ref-2"
    assert tokens.expires_at == NOW + timedelta(seconds=129600)


@pytest.mark.db
def test_tokens_are_encrypted_at_rest(conn: psycopg.Connection[Any], fernet_key: bytes) -> None:
    TokenStore(fernet_key).save(conn, "co_1", TokenSet("acc-1", "ref-1", NOW, "workers.read"))
    row = conn.execute("SELECT access_token_enc, refresh_token_enc FROM oauth_tokens").fetchone()
    assert row is not None and b"acc-1" not in bytes(row[0]) and b"ref-1" not in bytes(row[1])


@pytest.mark.db
def test_a_fresh_access_token_is_returned_without_refreshing(
    conn: psycopg.Connection[Any], fernet_key: bytes
) -> None:
    fake, store = FakeRippling(), TokenStore(fernet_key)
    store.save(conn, "co_1", TokenSet("acc-1", "ref-1", NOW + timedelta(hours=1), "s"))
    with httpx.Client(transport=fake.transport()) as http:
        assert store.access_token(conn, "co_1", http, APP, NOW) == "acc-1"
    assert fake.token_requests == []


@pytest.mark.db
def test_an_expiring_token_is_refreshed_and_the_new_pair_replaces_the_old(
    conn: psycopg.Connection[Any], fernet_key: bytes
) -> None:
    fake, store = FakeRippling(), TokenStore(fernet_key)
    store.save(conn, "co_1", TokenSet("acc-1", "ref-1", NOW + timedelta(seconds=30), "s"))
    with httpx.Client(transport=fake.transport()) as http:
        assert store.access_token(conn, "co_1", http, APP, NOW) == "tok-2"
        assert store.access_token(conn, "co_1", http, APP, NOW) == "tok-2"
    assert len(fake.token_requests) == 1
    assert fake.token_requests[0]["grant_type"] == "refresh_token"
    assert fake.token_requests[0]["refresh_token"] == "ref-1"


@pytest.mark.db
def test_concurrent_callers_refresh_once(schema_url: str, fernet_key: bytes) -> None:
    fake, store = FakeRippling(), TokenStore(fernet_key)
    with psycopg.connect(schema_url) as c:
        store.save(c, "co_1", TokenSet("acc-1", "ref-1", NOW - timedelta(seconds=1), "s"))
    got: list[str] = []

    def call() -> None:
        with psycopg.connect(schema_url) as c, httpx.Client(transport=fake.transport()) as http:
            got.append(store.access_token(c, "co_1", http, APP, NOW))

    threads = [threading.Thread(target=call) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert got == ["tok-2"] * 4
    assert len(fake.token_requests) == 1


@pytest.mark.db
def test_unknown_company_fails_loudly(conn: psycopg.Connection[Any], fernet_key: bytes) -> None:
    with httpx.Client(transport=FakeRippling().transport()) as http, pytest.raises(LookupError):
        TokenStore(fernet_key).access_token(conn, "co_missing", http, APP, NOW)
