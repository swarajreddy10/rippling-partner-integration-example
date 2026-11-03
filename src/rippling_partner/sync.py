"""Keep a local copy of a company's Rippling workers correct.

A sync lists every worker (``GET /workers`` with ``limit=100``, following ``next_link`` to null),
upserts them in set-based batches that skip unchanged rows, and only after a listing that reached
the end marks workers absent from it as removed. A listing that stops early never removes anyone,
and an empty complete listing is refused rather than read as "everyone left".
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from rippling_partner.client import RipplingClient

BATCH = 500
LOCK_PREFIX = "rippling-partner:sync:"

_UPSERT = """
INSERT INTO workers AS w (company_id, worker_id, payload, content_hash, first_seen_run,
                          last_changed_run)
SELECT %(company)s, ids.id, ids.payload, ids.hash, %(run)s, %(run)s
FROM unnest(%(ids)s::text[], %(payloads)s::jsonb[], %(hashes)s::text[]) AS ids(id, payload, hash)
ON CONFLICT (company_id, worker_id) DO UPDATE
SET payload = excluded.payload, content_hash = excluded.content_hash,
    last_changed_run = excluded.last_changed_run, removed_at = NULL
WHERE w.content_hash IS DISTINCT FROM excluded.content_hash OR w.removed_at IS NOT NULL
RETURNING (xmax = 0) AS inserted
"""

# An anti-join over the unnested set, so the plan stays a hash anti-join however large the set.
_RECONCILE = """
UPDATE workers AS w SET removed_at = now()
WHERE w.company_id = %(company)s AND w.removed_at IS NULL
  AND NOT EXISTS (SELECT 1 FROM unnest(%(seen)s::text[]) AS s(id) WHERE s.id = w.worker_id)
"""


class EmptyListingError(Exception):
    """A complete listing returned no workers; reconciling it would remove everyone."""


class SyncAlreadyRunningError(Exception):
    """Another sync of the same company holds its lock."""


@dataclass(frozen=True)
class RunResult:
    run_id: int
    outcome: str
    seen: int
    inserted: int
    updated: int
    unchanged: int
    removed: int


def content_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def sync_workers(
    conn: psycopg.Connection[Any], client: RipplingClient, company_id: str
) -> RunResult:
    """One sync of one company. Runs for the same company never overlap: an overlapping run
    could reconcile with a listing that predates a worker the other run just inserted."""
    lock_key = f"{LOCK_PREFIX}{company_id}"
    with conn.transaction():
        acquired = conn.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (lock_key,)).fetchone()
    if not acquired or not acquired[0]:
        raise SyncAlreadyRunningError(f"company {company_id}: another sync holds the lock")
    try:
        return _sync_locked(conn, client, company_id)
    finally:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (lock_key,))


def _sync_locked(
    conn: psycopg.Connection[Any], client: RipplingClient, company_id: str
) -> RunResult:
    with conn.transaction():
        row = conn.execute(
            "INSERT INTO sync_runs (company_id) VALUES (%s) RETURNING run_id", (company_id,)
        ).fetchone()
    if row is None:
        raise RuntimeError("INSERT ... RETURNING run_id returned no row")
    run_id: int = row[0]
    # A listing can return the same worker twice (for example when rows move while the cursor
    # pages). One statement cannot update a row twice, so each batch keeps the latest copy.
    seen: dict[str, None] = {}
    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    batch: dict[str, dict[str, Any]] = {}

    def flush() -> None:
        if not batch:
            return
        workers = list(batch.values())
        with conn.transaction():
            rows = conn.execute(
                _UPSERT,
                {
                    "company": company_id,
                    "run": run_id,
                    "ids": list(batch),
                    "payloads": [Jsonb(w) for w in workers],
                    "hashes": [content_hash(w) for w in workers],
                },
            ).fetchall()
        counts["inserted"] += sum(1 for (inserted,) in rows if inserted)
        counts["updated"] += sum(1 for (inserted,) in rows if not inserted)
        counts["unchanged"] += len(workers) - len(rows)
        batch.clear()

    try:
        for worker in client.paginate("/workers", limit=100):
            worker_id = str(worker["id"])
            seen[worker_id] = None
            batch[worker_id] = worker
            if len(batch) >= BATCH:
                flush()
        flush()
    except Exception as exc:
        _finish(conn, run_id, "failed", len(seen), counts, 0, repr(exc))
        raise

    if not seen:
        _finish(conn, run_id, "refused", 0, counts, 0, "complete listing returned no workers")
        raise EmptyListingError(f"company {company_id}: complete listing returned no workers")

    with conn.transaction():
        removed = conn.execute(_RECONCILE, {"company": company_id, "seen": list(seen)}).rowcount
    _finish(conn, run_id, "succeeded", len(seen), counts, removed, None)
    return RunResult(
        run_id,
        "succeeded",
        len(seen),
        counts["inserted"],
        counts["updated"],
        counts["unchanged"],
        removed,
    )


def _finish(
    conn: psycopg.Connection[Any],
    run_id: int,
    outcome: str,
    seen: int,
    counts: dict[str, int],
    removed: int,
    error: str | None,
) -> None:
    with conn.transaction():
        conn.execute(
            "UPDATE sync_runs SET finished_at = now(), outcome = %s, seen = %s, inserted = %s,"
            " updated = %s, unchanged = %s, removed = %s, error = %s WHERE run_id = %s",
            (
                outcome,
                seen,
                counts["inserted"],
                counts["updated"],
                counts["unchanged"],
                removed,
                error,
                run_id,
            ),
        )
