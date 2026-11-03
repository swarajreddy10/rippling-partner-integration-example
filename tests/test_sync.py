"""Worker sync: idempotent upserts and guarded reconciliation, on a real Postgres."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from fake_rippling import FakeRippling

from rippling_sync.client import RetryPolicy, RipplingAPIError, RipplingClient
from rippling_sync.sync import EmptyListingRefused, sync_workers

pytestmark = pytest.mark.db


def client(fake: FakeRippling) -> RipplingClient:
    return RipplingClient(
        lambda: "tok-1",
        "2025-08-01",
        transport=fake.transport(),
        retry=RetryPolicy(max_attempts=2),
        sleep=lambda s: None,
    )


def people(n: int) -> list[dict[str, Any]]:
    return [
        {"id": f"w{i}", "status": "ACTIVE", "work_email": f"w{i}@acme.example"} for i in range(n)
    ]


def stored(conn: psycopg.Connection[Any]) -> dict[str, tuple[str, bool]]:
    rows = conn.execute(
        "SELECT worker_id, payload->>'status', removed_at IS NOT NULL FROM workers"
    ).fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


def test_first_sync_inserts_every_worker_across_pages(conn: psycopg.Connection[Any]) -> None:
    fake = FakeRippling(workers=people(250))
    r = sync_workers(conn, client(fake), "co_1")
    assert (r.outcome, r.seen, r.inserted, r.updated, r.unchanged, r.removed) == (
        "succeeded",
        250,
        250,
        0,
        0,
        0,
    )
    assert len(stored(conn)) == 250
    assert len(fake.requests) == 3  # limit=100: 100 + 100 + 50


def test_an_identical_second_sync_writes_nothing(conn: psycopg.Connection[Any]) -> None:
    fake = FakeRippling(workers=people(3))
    sync_workers(conn, client(fake), "co_1")
    r = sync_workers(conn, client(fake), "co_1")
    assert (r.inserted, r.updated, r.unchanged, r.removed) == (0, 0, 3, 0)


def test_a_changed_worker_is_updated(conn: psycopg.Connection[Any]) -> None:
    fake = FakeRippling(workers=people(3))
    sync_workers(conn, client(fake), "co_1")
    fake.workers[1]["status"] = "TERMINATED"
    r = sync_workers(conn, client(fake), "co_1")
    assert (r.updated, r.unchanged) == (1, 2)
    assert stored(conn)["w1"] == ("TERMINATED", False)


def test_a_worker_missing_from_a_complete_listing_is_marked_removed_then_restored(
    conn: psycopg.Connection[Any],
) -> None:
    fake = FakeRippling(workers=people(3))
    sync_workers(conn, client(fake), "co_1")
    gone = fake.workers.pop(2)
    r = sync_workers(conn, client(fake), "co_1")
    assert r.removed == 1 and stored(conn)["w2"] == ("ACTIVE", True)
    fake.workers.append(gone)
    r = sync_workers(conn, client(fake), "co_1")
    assert (r.updated, r.removed) == (1, 0) and stored(conn)["w2"] == ("ACTIVE", False)


def test_a_listing_that_fails_partway_removes_nobody(conn: psycopg.Connection[Any]) -> None:
    fake = FakeRippling(workers=people(150))
    sync_workers(conn, client(fake), "co_1")
    fake.workers = people(120)
    fake.next_link_override = None
    # first page succeeds, then the second page fails past the retry budget
    first = fake.handle
    calls = {"n": 0}

    def fail_second(request: Any) -> Any:
        calls["n"] += 1
        if calls["n"] >= 2:
            fake.script(500, {"error": "server_error", "message": "down"})
        return first(request)

    fake.handle = fail_second  # type: ignore[method-assign]
    with pytest.raises(RipplingAPIError):
        sync_workers(conn, client(fake), "co_1")
    assert sum(1 for _, removed in stored(conn).values() if removed) == 0
    outcome = conn.execute("SELECT outcome FROM sync_runs ORDER BY run_id DESC LIMIT 1").fetchone()
    assert outcome == ("failed",)


def test_an_empty_complete_listing_is_refused(conn: psycopg.Connection[Any]) -> None:
    fake = FakeRippling(workers=people(3))
    sync_workers(conn, client(fake), "co_1")
    fake.workers = []
    with pytest.raises(EmptyListingRefused):
        sync_workers(conn, client(fake), "co_1")
    assert all(not removed for _, removed in stored(conn).values())
    outcome = conn.execute("SELECT outcome FROM sync_runs ORDER BY run_id DESC LIMIT 1").fetchone()
    assert outcome == ("refused",)


def test_companies_are_isolated(conn: psycopg.Connection[Any]) -> None:
    sync_workers(conn, client(FakeRippling(workers=people(2))), "co_1")
    r = sync_workers(conn, client(FakeRippling(workers=people(1))), "co_2")
    assert r.removed == 0
    n = conn.execute(
        "SELECT count(*) FROM workers WHERE company_id = 'co_1' AND removed_at IS NULL"
    ).fetchone()
    assert n == (2,)
