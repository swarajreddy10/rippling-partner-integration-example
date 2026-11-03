"""A local fake of Rippling's documented behaviour, used instead of a live company.

Every rule here is taken from Rippling's published documentation (checked 2026-11-03):
- pagination: default 50, maximum 100; ``next_link`` until null; out-of-range ``limit`` is a 400
  with code VALIDATION_ERROR (rest-api/essentials/pagination, api-limits)
- tokens: a revoked token answers 403 ``{"detail": "The token has been revoked"}``
  (developer-portal/v2-guides/installation)
- unauthenticated requests answer 401 ``{"ok": false, "error": ...}`` with an
  ``x-rippling-request-id`` header (observed on rest.ripplingapis.com, 2026-11-03)
"""

from __future__ import annotations

import itertools
import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlencode

import httpx

REST = "rest.ripplingapis.com"
V1 = "api.rippling.com"


@dataclass
class FakeRippling:
    workers: list[dict[str, Any]] = field(default_factory=list)
    companies: list[dict[str, Any]] = field(
        default_factory=lambda: [{"id": "co_1", "name": "Acme Inc"}]
    )
    valid_tokens: set[str] = field(default_factory=lambda: {"tok-1"})
    revoked_tokens: set[str] = field(default_factory=set)
    scripted: deque[httpx.Response] = field(default_factory=deque)
    next_link_override: str | None = None
    requests: list[httpx.Request] = field(default_factory=list)
    token_requests: list[dict[str, Any]] = field(default_factory=list)
    installed: list[str] = field(default_factory=list)
    mark_installed_status: int = 200
    token_body_override: dict[str, Any] | None = None
    _ids: itertools.count[int] = field(default_factory=lambda: itertools.count(1))

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def script(self, status: int, body: Any = None, headers: dict[str, str] | None = None) -> None:
        """Queue a response returned before normal handling (for failure paths)."""
        content = (
            b""
            if body is None
            else (body if isinstance(body, bytes) else json.dumps(body).encode())
        )
        self.scripted.append(httpx.Response(status, content=content, headers=headers or {}))

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        rid = {"x-rippling-request-id": f"req-{next(self._ids)}"}
        if self.scripted:
            r = self.scripted.popleft()
            return httpx.Response(r.status_code, content=r.content, headers={**rid, **r.headers})
        if request.url.host == V1 and request.url.path == "/api/o/token/":
            return self._token(request, rid)
        auth = request.headers.get("authorization", "")
        token = auth.removeprefix("Bearer ").strip()
        if token in self.revoked_tokens:
            return httpx.Response(403, json={"detail": "The token has been revoked"}, headers=rid)
        if token not in self.valid_tokens:
            return httpx.Response(
                401,
                json={"ok": False, "error": "Incorrect authentication credentials."},
                headers=rid,
            )
        if request.url.host == V1 and request.url.path == "/platform/api/mark_app_installed":
            if self.mark_installed_status != 200:
                return httpx.Response(
                    self.mark_installed_status, json={"detail": "Forbidden."}, headers=rid
                )
            self.installed.append(token)
            return httpx.Response(200, json={"ok": True}, headers=rid)
        if request.url.host == REST and request.url.path in ("/workers", "/workers/"):
            return self._page(request, self.workers, "/workers", rid)
        if request.url.host == REST and request.url.path in ("/companies", "/companies/"):
            return self._page(request, self.companies, "/companies", rid)
        return httpx.Response(404, json={"detail": "Not found."}, headers=rid)

    def _page(
        self, request: httpx.Request, rows: list[dict[str, Any]], path: str, rid: dict[str, str]
    ) -> httpx.Response:
        q = parse_qs(request.url.query.decode())
        limit = int(q.get("limit", ["50"])[0])
        if not 1 <= limit <= 100:
            return httpx.Response(
                400,
                json={
                    "code": "VALIDATION_ERROR",
                    "message": "Parameter 'limit' should be in range [1, 100]",
                },
                headers=rid,
            )
        start = int(q.get("cursor", ["0"])[0])
        page = rows[start : start + limit]
        nxt = start + limit
        next_link = None
        if nxt < len(rows):
            next_link = self.next_link_override or (
                f"https://{REST}{path}?" + urlencode({"cursor": nxt, "limit": limit})
            )
        return httpx.Response(200, json={"results": page, "next_link": next_link}, headers=rid)

    def _token(self, request: httpx.Request, rid: dict[str, str]) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.token_requests.append({"auth": request.headers.get("authorization"), **form})
        n = len(self.token_requests)
        access, refresh = f"tok-{n + 1}", f"ref-{n + 1}"
        self.valid_tokens.add(access)
        if self.token_body_override is not None:
            return httpx.Response(200, json=self.token_body_override, headers=rid)
        return httpx.Response(
            200,
            json={
                "access_token": access,
                "token_type": "Bearer",
                "expires_in": 129600,
                "refresh_token": refresh,
                "scope": "workers.read companies.read",
            },
            headers=rid,
        )
