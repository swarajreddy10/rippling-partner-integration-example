"""ASGI entry point: ``uvicorn --factory rippling_partner.web:app``."""

from __future__ import annotations

import os
from typing import Any

import psycopg
from starlette.applications import Starlette

from rippling_partner.config import Settings
from rippling_partner.install import create_app
from rippling_partner.oauth import TokenStore


def app() -> Starlette:
    settings = Settings.from_env(os.environ)

    def connect() -> psycopg.Connection[Any]:
        return psycopg.connect(settings.database_url)

    return create_app(
        oauth_app=settings.oauth_app,
        store=TokenStore(settings.token_key),
        connect=connect,
        api_version=settings.api_version,
    )
