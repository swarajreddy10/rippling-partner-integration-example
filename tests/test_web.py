"""The ASGI factory builds the install app from the environment and fails loudly without it."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from starlette.applications import Starlette

from rippling_sync import web


def test_factory_builds_the_install_app(monkeypatch: pytest.MonkeyPatch) -> None:
    env = {
        "DATABASE_URL": "postgresql://localhost/x",
        "TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "RIPPLING_CLIENT_ID": "cid",
        "RIPPLING_CLIENT_SECRET": "secret",
        "RIPPLING_APP_NAME": "acme_sync",
        "RIPPLING_REDIRECT_URI": "https://partner.example/oauth/callback",
        "RIPPLING_API_VERSION": "2025-08-01",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    app = web.app()
    assert isinstance(app, Starlette)
    assert {r.path for r in app.routes} == {"/install", "/oauth/callback"}  # type: ignore[attr-defined]


def test_factory_fails_without_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        web.app()
