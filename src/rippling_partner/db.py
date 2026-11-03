"""Schema for tokens, install state, workers, and sync runs. Safe to run repeatedly."""

from __future__ import annotations

from typing import Any

import psycopg

SCHEMA = """
CREATE TABLE IF NOT EXISTS oauth_tokens (
    company_id        text PRIMARY KEY,
    access_token_enc  bytea NOT NULL,
    refresh_token_enc bytea NOT NULL,
    expires_at        timestamptz NOT NULL,
    scope             text NOT NULL,
    updated_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS oauth_states (
    state      text PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS installs (
    company_id   text PRIMARY KEY,
    installed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sync_runs (
    run_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_id  text NOT NULL,
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    outcome     text NOT NULL DEFAULT 'running'
                CHECK (outcome IN ('running', 'succeeded', 'failed', 'refused')),
    seen        integer NOT NULL DEFAULT 0,
    inserted    integer NOT NULL DEFAULT 0,
    updated     integer NOT NULL DEFAULT 0,
    unchanged   integer NOT NULL DEFAULT 0,
    removed     integer NOT NULL DEFAULT 0,
    error       text
);

CREATE TABLE IF NOT EXISTS workers (
    company_id       text NOT NULL,
    worker_id        text NOT NULL,
    payload          jsonb NOT NULL,
    content_hash     text NOT NULL,
    first_seen_run   bigint NOT NULL REFERENCES sync_runs (run_id),
    last_changed_run bigint NOT NULL REFERENCES sync_runs (run_id),
    removed_at       timestamptz,
    PRIMARY KEY (company_id, worker_id)
);

-- Reconciliation scans only a company's live workers.
CREATE INDEX IF NOT EXISTS workers_live ON workers (company_id) WHERE removed_at IS NULL;
"""


def migrate(conn: psycopg.Connection[Any]) -> None:
    with conn.transaction():
        conn.execute(SCHEMA)
