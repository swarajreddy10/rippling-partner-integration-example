"""Client behaviour against Rippling's documented contract (see tests/fake_rippling.py)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
from fake_rippling import FakeRippling

from rippling_partner.client import RetryPolicy, RipplingAPIError, RipplingClient, parse_error

VERSION = "2025-08-01"


class Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


def make(fake: FakeRippling, clock: Clock | None = None, **retry: float) -> RipplingClient:
    clock = clock or Clock()
    return RipplingClient(
        lambda: "tok-1",
        VERSION,
        transport=fake.transport(),
        retry=RetryPolicy(**retry),
        sleep=clock.sleep,
        clock=clock.now,
        rng=lambda: 0.5,
    )


def workers(n: int) -> list[dict[str, str]]:
    return [{"id": f"w{i}", "name": f"Worker {i}"} for i in range(n)]


def test_paginate_follows_next_link_until_null() -> None:
    fake = FakeRippling(workers=workers(5))
    got = list(make(fake).paginate("/workers", limit=2))
    assert [w["id"] for w in got] == ["w0", "w1", "w2", "w3", "w4"]
    assert len(fake.requests) == 3
    assert str(fake.requests[1].url) == "https://rest.ripplingapis.com/workers?cursor=2&limit=2"


@pytest.mark.parametrize("limit", [0, 101])
def test_limit_outside_documented_range_raises_before_any_request(limit: int) -> None:
    fake = FakeRippling(workers=workers(3))
    with pytest.raises(ValueError, match="1 and 100"):
        list(make(fake).paginate("/workers", limit=limit))
    assert fake.requests == []


def test_every_request_pins_the_version_and_sends_the_bearer_token() -> None:
    fake = FakeRippling(workers=workers(5))
    list(make(fake).paginate("/workers", limit=2))
    assert {r.headers["Rippling-Api-Version"] for r in fake.requests} == {VERSION}
    assert {r.headers["Authorization"] for r in fake.requests} == {"Bearer tok-1"}


def test_version_must_be_a_date() -> None:
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        RipplingClient(lambda: "t", "2025-8-1", transport=FakeRippling().transport())


def test_429_with_retry_after_seconds_waits_that_long() -> None:
    fake, clock = FakeRippling(workers=workers(1)), Clock()
    fake.script(429, {"error": "rate_limited"}, {"Retry-After": "3"})
    assert make(fake, clock).get("/workers")["results"][0]["id"] == "w0"
    assert clock.slept == [3.0]


def test_retry_after_is_capped() -> None:
    fake, clock = FakeRippling(workers=workers(1)), Clock()
    fake.script(429, None, {"Retry-After": "600"})
    make(fake, clock, retry_after_cap=5.0).get("/workers")
    assert clock.slept == [5.0]


def test_retry_after_as_http_date() -> None:
    fake, clock = FakeRippling(workers=workers(1)), Clock()
    when = format_datetime(datetime.now(UTC) + timedelta(seconds=5), usegmt=True)
    fake.script(429, None, {"Retry-After": when})
    make(fake, clock).get("/workers")
    assert len(clock.slept) == 1 and 3.0 <= clock.slept[0] <= 5.0


def test_429_without_retry_after_uses_full_jitter_backoff() -> None:
    fake, clock = FakeRippling(workers=workers(1)), Clock()
    fake.script(429)
    fake.script(429)
    make(fake, clock, base_delay=1.0).get("/workers")
    assert clock.slept == [0.5, 1.0]  # rng 0.5 x (1, 2)


@pytest.mark.parametrize("status", [500, 504])
def test_documented_server_errors_are_retried(status: int) -> None:
    fake = FakeRippling(workers=workers(1))
    fake.script(status, {"error": "server_error", "message": "boom"})
    make(fake).get("/workers")
    assert len(fake.requests) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422, 502, 503])
def test_other_statuses_are_not_retried(status: int) -> None:
    fake = FakeRippling(workers=workers(1))
    fake.script(status, {"code": "X", "message": "no"})
    with pytest.raises(RipplingAPIError) as err:
        make(fake).get("/workers")
    assert err.value.status == status
    assert len(fake.requests) == 1


def test_attempt_budget_ends_retries_with_the_last_error() -> None:
    fake = FakeRippling(workers=workers(1))
    for _ in range(5):
        fake.script(500, {"error": "server_error", "message": "still down"})
    with pytest.raises(RipplingAPIError) as err:
        make(fake, max_attempts=3).get("/workers")
    assert err.value.status == 500 and len(fake.requests) == 3


def test_deadline_ends_retries_instead_of_sleeping_past_it() -> None:
    fake, clock = FakeRippling(workers=workers(1)), Clock()
    fake.script(429, None, {"Retry-After": "50"})
    with pytest.raises(RipplingAPIError) as err:
        make(fake, clock, deadline=10.0).get("/workers")
    assert err.value.status == 429 and clock.slept == []


def test_connection_errors_are_retried() -> None:
    calls = {"n": 0}
    fake = FakeRippling(workers=workers(1))

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("reset", request=request)
        return fake.handle(request)

    client = RipplingClient(
        lambda: "tok-1",
        VERSION,
        transport=httpx.MockTransport(flaky),
        sleep=lambda s: None,
        clock=lambda: 0.0,
        rng=lambda: 0.0,
    )
    assert client.get("/workers")["results"][0]["id"] == "w0"
    assert calls["n"] == 2


@pytest.mark.parametrize(
    ("body", "code", "message"),
    [
        ({"error": "bad_request", "message": "Invalid filter"}, "bad_request", "Invalid filter"),
        (
            {"code": "VALIDATION_ERROR", "message": "limit", "field_errors": [{"field": "limit"}]},
            "VALIDATION_ERROR",
            "limit",
        ),
        ({"detail": "The token has been revoked"}, None, "The token has been revoked"),
        (
            {"ok": False, "error": "Incorrect authentication credentials."},
            None,
            "Incorrect authentication credentials.",
        ),
    ],
)
def test_each_live_error_shape_parses(
    body: dict[str, object], code: str | None, message: str
) -> None:
    r = httpx.Response(400, json=body, headers={"x-rippling-request-id": "req-9"})
    err = parse_error(r)
    assert (err.status, err.code, err.message, err.request_id) == (400, code, message, "req-9")


def test_non_json_error_body_is_kept_as_text() -> None:
    err = parse_error(httpx.Response(502, content=b"<html>Bad gateway</html>"))
    assert err.status == 502 and "Bad gateway" in err.message


def test_next_link_to_another_host_is_refused_and_not_followed() -> None:
    fake = FakeRippling(
        workers=workers(4), next_link_override="https://evil.example/workers?cursor=2"
    )
    with pytest.raises(RipplingAPIError, match="next_link"):
        list(make(fake).paginate("/workers", limit=2))
    assert all(r.url.host == "rest.ripplingapis.com" for r in fake.requests)


def test_a_repeated_next_link_is_refused() -> None:
    fake = FakeRippling(
        workers=workers(6),
        next_link_override="https://rest.ripplingapis.com/workers?cursor=2&limit=2",
    )
    with pytest.raises(RipplingAPIError, match="repeated"):
        list(make(fake).paginate("/workers", limit=2))


def test_retry_after_http_date_with_minus_zero_zone_is_read_as_utc() -> None:
    # parsedate_to_datetime returns a naive datetime for "-0000"; HTTP dates are always GMT.
    fake, clock = FakeRippling(workers=workers(1)), Clock()
    when = (datetime.now(UTC) + timedelta(seconds=5)).strftime("%a, %d %b %Y %H:%M:%S -0000")
    fake.script(429, None, {"Retry-After": when})
    make(fake, clock).get("/workers")
    assert len(clock.slept) == 1 and 3.0 <= clock.slept[0] <= 5.0
