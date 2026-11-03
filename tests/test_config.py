"""Configuration comes from the environment and names every missing setting at once."""

from __future__ import annotations

import pytest

from rippling_sync.config import Settings

FULL = {
    "DATABASE_URL": "postgresql://localhost/x",
    "TOKEN_ENCRYPTION_KEY": "k" * 44,
    "RIPPLING_CLIENT_ID": "cid",
    "RIPPLING_CLIENT_SECRET": "secret",
    "RIPPLING_APP_NAME": "acme_sync",
    "RIPPLING_REDIRECT_URI": "https://partner.example/oauth/callback",
    "RIPPLING_API_VERSION": "2025-08-01",
}


def test_settings_load_from_the_environment() -> None:
    s = Settings.from_env(FULL)
    assert s.oauth_app.app_name == "acme_sync" and s.api_version == "2025-08-01"


def test_every_missing_setting_is_named() -> None:
    env = {k: v for k, v in FULL.items() if k not in ("RIPPLING_CLIENT_SECRET", "DATABASE_URL")}
    with pytest.raises(RuntimeError) as err:
        Settings.from_env(env)
    assert "DATABASE_URL" in str(err.value) and "RIPPLING_CLIENT_SECRET" in str(err.value)


def test_redirect_uri_must_be_https() -> None:
    with pytest.raises(RuntimeError, match="https"):
        Settings.from_env({**FULL, "RIPPLING_REDIRECT_URI": "http://partner.example/cb"})
