"""Settings from the environment. Every missing setting is named at once, so setup fails loudly."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from rippling_partner.oauth import OAuthApp

REQUIRED = (
    "DATABASE_URL",
    "TOKEN_ENCRYPTION_KEY",
    "RIPPLING_CLIENT_ID",
    "RIPPLING_CLIENT_SECRET",
    "RIPPLING_APP_NAME",
    "RIPPLING_REDIRECT_URI",
    "RIPPLING_API_VERSION",
)


@dataclass(frozen=True)
class Settings:
    database_url: str
    token_key: bytes
    oauth_app: OAuthApp
    api_version: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        missing = [name for name in REQUIRED if not env.get(name)]
        if missing:
            raise RuntimeError(f"missing settings: {', '.join(missing)}")
        redirect = env["RIPPLING_REDIRECT_URI"]
        if not redirect.startswith("https://"):
            raise RuntimeError("RIPPLING_REDIRECT_URI must be https (Rippling accepts only https)")
        return cls(
            database_url=env["DATABASE_URL"],
            token_key=env["TOKEN_ENCRYPTION_KEY"].encode(),
            oauth_app=OAuthApp(
                env["RIPPLING_CLIENT_ID"],
                env["RIPPLING_CLIENT_SECRET"],
                env["RIPPLING_APP_NAME"],
                redirect,
            ),
            api_version=env["RIPPLING_API_VERSION"],
        )
