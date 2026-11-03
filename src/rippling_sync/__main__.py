"""Command line: ``python -m rippling_sync migrate`` and ``... sync --company ID``."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import UTC, datetime

import httpx
import psycopg

from rippling_sync import db
from rippling_sync.client import RipplingClient
from rippling_sync.config import Settings
from rippling_sync.oauth import CachedToken, TokenStore
from rippling_sync.sync import sync_workers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rippling_sync")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="create or update the database schema")
    run = sub.add_parser("sync", help="sync one installed company's workers")
    run.add_argument("--company", required=True, help="Rippling company id bound at install")
    args = parser.parse_args(argv)

    settings = Settings.from_env(os.environ)
    with psycopg.connect(settings.database_url) as conn:
        if args.command == "migrate":
            db.migrate(conn)
            return 0
        store = TokenStore(settings.token_key)
        with httpx.Client(timeout=30.0) as http:

            def now() -> datetime:
                return datetime.now(UTC)

            token = CachedToken(
                lambda: store.current(conn, args.company, http, settings.oauth_app, now()),
                clock=now,
            )
            client = RipplingClient(token, settings.api_version)
            try:
                result = sync_workers(conn, client, args.company)
            finally:
                client.close()
    json.dump(asdict(result), sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
