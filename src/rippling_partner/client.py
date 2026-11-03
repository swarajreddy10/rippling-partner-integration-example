"""Rippling REST API v2 client: version pinning, cursor paging, and retries.

Rules, each from Rippling's documentation (checked 2026-11-03):
- Pin the dated version on every request with ``Rippling-Api-Version``
  (rest-api/essentials/versioning).
- Page list endpoints with ``limit`` (1 to 100) and follow ``next_link`` as-is until it is null
  (rest-api/essentials/pagination).
- Retry 429, 500, and 504 with exponential backoff or ``Retry-After``; never retry 400, 401, 403,
  404, or 422 (rest-api/essentials/api-limits, rest-api/essentials/troubleshooting).
"""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx2

RETRYABLE_STATUS = frozenset({429, 500, 504})
_VERSION = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class RetryPolicy:
    """How long one call may keep trying. ``deadline`` covers the call, retries included."""

    max_attempts: int = 5
    base_delay: float = 0.5
    max_delay: float = 30.0
    retry_after_cap: float = 60.0
    deadline: float = 120.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError(f"RetryPolicy needs at least one attempt, got {self.max_attempts}")
        for name in ("base_delay", "max_delay", "retry_after_cap", "deadline"):
            if getattr(self, name) < 0:
                raise ValueError(f"RetryPolicy.{name} must not be negative")


DEFAULT_RETRY = RetryPolicy()


class RipplingAPIError(Exception):
    """A failed call, with what Rippling support asks for: status, error body, and request id."""

    def __init__(
        self,
        status: int,
        message: str,
        *,
        code: str | None = None,
        field_errors: list[Any] | None = None,
        request_id: str | None = None,
        body: Any = None,
    ) -> None:
        super().__init__(
            f"{status}: {message}" + (f" (request id {request_id})" if request_id else "")
        )
        self.status = status
        self.message = message
        self.code = code
        self.field_errors = field_errors or []
        self.request_id = request_id
        self.body = body


def parse_error(response: httpx2.Response) -> RipplingAPIError:
    """Read any of the four error shapes live on Rippling's API today.

    ``{"error", "message"}``, ``{"code", "message", "field_errors"}``, ``{"detail"}``, and
    ``{"ok": false, "error"}``; anything else is kept as text.
    """
    request_id = response.headers.get("x-rippling-request-id")
    try:
        body: Any = response.json()
    except ValueError:
        return RipplingAPIError(
            response.status_code, response.text[:500], request_id=request_id, body=response.text
        )
    if not isinstance(body, dict):
        return RipplingAPIError(
            response.status_code, str(body)[:500], request_id=request_id, body=body
        )
    if "code" in body and "message" in body:
        return RipplingAPIError(
            response.status_code,
            str(body["message"]),
            code=str(body["code"]),
            field_errors=body.get("field_errors"),
            request_id=request_id,
            body=body,
        )
    if "detail" in body:
        return RipplingAPIError(
            response.status_code, str(body["detail"]), request_id=request_id, body=body
        )
    if body.get("ok") is False and "error" in body:
        return RipplingAPIError(
            response.status_code, str(body["error"]), request_id=request_id, body=body
        )
    if "error" in body:
        return RipplingAPIError(
            response.status_code,
            str(body.get("message") or body["error"]),
            code=str(body["error"]),
            request_id=request_id,
            body=body,
        )
    return RipplingAPIError(response.status_code, str(body)[:500], request_id=request_id, body=body)


def _retry_after_seconds(value: str | None) -> float | None:
    """``Retry-After`` is either delay-seconds or an HTTP-date (RFC 9110, section 10.2.3)."""
    if value is None:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:  # "-0000" parses as naive; an HTTP-date is always GMT
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


class RipplingClient:
    BASE_URL = "https://rest.ripplingapis.com"

    def __init__(
        self,
        token_provider: Callable[[], str],
        api_version: str,
        *,
        transport: httpx2.BaseTransport | None = None,
        retry: RetryPolicy = DEFAULT_RETRY,
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
        rng: Callable[[], float] | None = None,
    ) -> None:
        if not _VERSION.match(api_version):
            raise ValueError(
                f"api_version must be a dated version, YYYY-MM-DD; got {api_version!r}"
            )
        self._token = token_provider
        self._version = api_version
        self._retry = retry
        self._sleep = sleep or time.sleep
        self._clock = clock or time.monotonic
        self._rng = rng or random.random
        self._origin = httpx2.URL(self.BASE_URL)
        self._http = httpx2.Client(base_url=self.BASE_URL, transport=transport, timeout=30.0)

    def get(self, path_or_url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET one resource or page, retrying only what Rippling documents as retryable."""
        started = self._clock()
        last: RipplingAPIError | None = None
        for attempt in range(self._retry.max_attempts):
            try:
                response = self._http.get(path_or_url, params=params, headers=self._headers())
            except httpx2.TransportError as exc:
                last = RipplingAPIError(0, f"transport error: {exc}")
                delay = self._backoff(attempt)
            else:
                if response.is_success:
                    data: dict[str, Any] = response.json()
                    return data
                last = parse_error(response)
                if response.status_code not in RETRYABLE_STATUS:
                    raise last
                hinted = _retry_after_seconds(response.headers.get("Retry-After"))
                delay = (
                    min(hinted, self._retry.retry_after_cap)
                    if hinted is not None
                    else self._backoff(attempt)
                )
            if attempt + 1 == self._retry.max_attempts:
                break
            if self._clock() - started + delay > self._retry.deadline:
                break
            self._sleep(delay)
        if last is None:  # RetryPolicy guarantees at least one attempt
            raise RuntimeError("no attempt was made")
        raise last

    def paginate(
        self, path: str, params: dict[str, Any] | None = None, *, limit: int = 100
    ) -> Iterator[dict[str, Any]]:
        """Yield every result, following ``next_link`` exactly as returned until it is null."""
        if not 1 <= limit <= 100:
            raise ValueError(
                f"limit must be between 1 and 100 (Rippling's documented range); got {limit}"
            )
        page = self.get(path, {**(params or {}), "limit": limit})
        seen: set[str] = set()
        while True:
            yield from page.get("results", [])
            next_link = page.get("next_link")
            if not next_link:
                return
            url = httpx2.URL(next_link)
            if (url.scheme, url.host, url.port) != (
                self._origin.scheme,
                self._origin.host,
                self._origin.port,
            ):
                raise RipplingAPIError(0, f"next_link points outside {self.BASE_URL}: {next_link}")
            if next_link in seen:
                raise RipplingAPIError(0, f"next_link repeated, refusing to loop: {next_link}")
            seen.add(next_link)
            page = self.get(next_link)

    def close(self) -> None:
        self._http.close()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token()}",
            "Rippling-Api-Version": self._version,
            "Accept": "application/json",
        }

    def _backoff(self, attempt: int) -> float:
        """Full jitter: uniform between 0 and the exponential ceiling."""
        return self._rng() * min(self._retry.max_delay, self._retry.base_delay * 2.0**attempt)
