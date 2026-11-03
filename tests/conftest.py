"""Real-Postgres fixtures. Database tests fail, not skip, when DATABASE_URL is missing."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from cryptography.fernet import Fernet

from rippling_partner import db


@pytest.fixture
def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set; database tests need a real Postgres")
    return url


@pytest.fixture
def schema_url(database_url: str) -> Iterator[str]:
    """A fresh schema per test, dropped afterwards; connections opened from the URL use it."""
    schema = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(database_url, autocommit=True) as admin:
        admin.execute(f"CREATE SCHEMA {schema}")
    url = f"{database_url}{'&' if '?' in database_url else '?'}options=-csearch_path%3D{schema}"
    with psycopg.connect(url) as conn:
        db.migrate(conn)
    try:
        yield url
    finally:
        with psycopg.connect(database_url, autocommit=True) as admin:
            admin.execute(f"DROP SCHEMA {schema} CASCADE")


@pytest.fixture
def conn(schema_url: str) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    with psycopg.connect(schema_url) as c:
        yield c


@pytest.fixture
def fernet_key() -> bytes:
    return Fernet.generate_key()
