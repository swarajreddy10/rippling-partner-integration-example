"""ASGI entry point: ``uvicorn --factory rippling_partner.web:app``.

Requests borrow connections from a psycopg pool, opened when the app starts and closed when it
stops (https://www.psycopg.org/psycopg3/docs/advanced/pool.html). The pool is created closed and
opened in the lifespan, because its ``open`` default may change in a future release.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from psycopg_pool import ConnectionPool
from starlette.applications import Starlette

from rippling_partner.config import Settings
from rippling_partner.install import create_app
from rippling_partner.oauth import TokenStore


def app() -> Starlette:
    settings = Settings.from_env(os.environ)
    pool = ConnectionPool(settings.database_url, open=False, min_size=1, max_size=10)

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        pool.open(wait=True, timeout=10.0)  # fail at startup, not on the first request
        try:
            yield
        finally:
            pool.close()

    application = create_app(
        oauth_app=settings.oauth_app,
        store=TokenStore(settings.token_key),
        connect=pool.connection,
        api_version=settings.api_version,
        lifespan=lifespan,
    )
    application.state.pool = pool
    return application
