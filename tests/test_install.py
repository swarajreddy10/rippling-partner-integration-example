"""Install flow: state, code exchange, company binding, mark installed, and the return redirect."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import psycopg
import pytest
from fake_rippling import FakeRippling
from starlette.testclient import TestClient

from rippling_partner.install import create_app
from rippling_partner.oauth import OAuthApp, TokenStore

pytestmark = pytest.mark.db

APP = OAuthApp("cid", "secret", "acme_sync", "https://partner.example/oauth/callback")
RETURN = "https://app.rippling.com/apps/PLATFORM/acme_sync/return"


@pytest.fixture
def fake() -> FakeRippling:
    return FakeRippling()


@pytest.fixture
def web(schema_url: str, fake: FakeRippling, fernet_key: bytes) -> Iterator[TestClient]:
    app = create_app(
        oauth_app=APP,
        store=TokenStore(fernet_key),
        connect=lambda: psycopg.connect(schema_url),
        api_version="2025-08-01",
        transport=fake.transport(),
        clock=lambda: datetime(2026, 11, 3, 12, 0, tzinfo=UTC),
    )
    with TestClient(app, follow_redirects=False) as client:
        yield client


def installs(schema_url: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(schema_url) as c:
        return c.execute("SELECT company_id FROM installs").fetchall()


def test_install_redirects_to_rippling_with_a_stored_state(
    web: TestClient, schema_url: str
) -> None:
    r = web.get("/install")
    assert r.status_code == 302
    url = urlsplit(r.headers["location"])
    assert url.netloc == "app.rippling.com"
    state = parse_qs(url.query)["state"][0]
    with psycopg.connect(schema_url) as c:
        assert c.execute(
            "SELECT count(*) FROM oauth_states WHERE state = %s", (state,)
        ).fetchone() == (1,)


def test_an_unknown_state_is_rejected_before_any_code_exchange(
    web: TestClient, fake: FakeRippling
) -> None:
    r = web.get("/oauth/callback", params={"code": "c1", "state": "forged"})
    assert r.status_code == 400
    assert fake.token_requests == []


def test_a_state_is_single_use(web: TestClient, fake: FakeRippling) -> None:
    state = parse_qs(urlsplit(web.get("/install").headers["location"]).query)["state"][0]
    assert web.get("/oauth/callback", params={"code": "c1", "state": state}).status_code == 302
    assert web.get("/oauth/callback", params={"code": "c2", "state": state}).status_code == 400
    assert len(fake.token_requests) == 1


def test_install_binds_the_company_saves_tokens_marks_installed_and_returns(
    web: TestClient, fake: FakeRippling, schema_url: str
) -> None:
    r = web.get("/oauth/callback", params={"code": "c1", "redirect_uri": RETURN})
    assert r.status_code == 302
    assert r.headers["location"] == RETURN
    assert fake.installed == ["tok-2"]
    assert installs(schema_url) == [("co_1",)]
    with psycopg.connect(schema_url) as c:
        assert c.execute("SELECT company_id FROM oauth_tokens").fetchall() == [("co_1",)]


def test_a_403_while_binding_the_company_leaves_the_install_unmarked(
    web: TestClient, fake: FakeRippling, schema_url: str
) -> None:
    fake.revoked_tokens.add("tok-2")
    r = web.get("/oauth/callback", params={"code": "c1"})
    assert r.status_code == 403
    assert fake.installed == []
    assert installs(schema_url) == []


def test_a_return_url_outside_app_rippling_com_is_not_followed(web: TestClient) -> None:
    r = web.get(
        "/oauth/callback", params={"code": "c1", "redirect_uri": "https://evil.example/phish"}
    )
    assert r.status_code == 302
    assert r.headers["location"] == RETURN


def test_a_token_for_more_than_one_company_fails_loudly(
    web: TestClient, fake: FakeRippling, schema_url: str
) -> None:
    fake.companies.append({"id": "co_2", "name": "Other"})
    r = web.get("/oauth/callback", params={"code": "c1"})
    assert r.status_code == 502
    assert fake.installed == []
    assert installs(schema_url) == []


def test_a_missing_code_is_a_bad_request(web: TestClient, fake: FakeRippling) -> None:
    assert web.get("/oauth/callback").status_code == 400
    assert fake.token_requests == []


def test_a_403_from_mark_installed_stores_no_tokens(
    web: TestClient, fake: FakeRippling, schema_url: str
) -> None:
    fake.mark_installed_status = 403
    r = web.get("/oauth/callback", params={"code": "c1"})
    assert r.status_code == 403
    assert installs(schema_url) == []
    with psycopg.connect(schema_url) as c:
        assert c.execute("SELECT count(*) FROM oauth_tokens").fetchone() == (0,)


def test_a_malformed_token_response_fails_cleanly_and_stores_nothing(
    web: TestClient, fake: FakeRippling, schema_url: str
) -> None:
    fake.token_body_override = {"access_token": "tok-x", "token_type": "Bearer"}
    r = web.get("/oauth/callback", params={"code": "c1"})
    assert r.status_code == 502
    assert "token response" in r.text
    assert installs(schema_url) == []
    assert fake.installed == []


def test_a_malformed_return_url_falls_back_to_the_default(web: TestClient) -> None:
    r = web.get("/oauth/callback", params={"code": "c1", "redirect_uri": "https://[::1"})
    assert r.status_code == 302
    assert r.headers["location"] == RETURN


def test_expired_states_are_purged(schema_url: str, web: TestClient) -> None:
    with psycopg.connect(schema_url) as c:
        c.execute(
            "INSERT INTO oauth_states (state, created_at) VALUES ('old', now() - interval '1 day')"
        )
        c.commit()
    web.get("/install")
    with psycopg.connect(schema_url) as c:
        assert c.execute("SELECT count(*) FROM oauth_states WHERE state = 'old'").fetchone() == (0,)
