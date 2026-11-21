"""A tiny fake HTTP server for adapter tests: route (METHOD, url prefix) -> list of replies."""

from __future__ import annotations

import json
from typing import Any, Callable

import httpx


class FakeApi:
    def __init__(self) -> None:
        self.routes: list[tuple[str, str, list[Any]]] = []
        self.calls: list[httpx.Request] = []

    def add(self, method: str, url: str, *replies: Any) -> "FakeApi":
        """Each reply: (status, json_body[, headers]) or a callable(request) -> httpx.Response.
        Replies are used in order; the last one repeats."""
        self.routes.append((method, url, list(replies)))
        return self

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        url = str(request.url).split("?")[0]
        for method, prefix, replies in self.routes:
            if request.method == method and url == prefix:
                rep = replies.pop(0) if len(replies) > 1 else replies[0]
                if callable(rep):
                    return rep(request)
                status, body, *rest = rep
                headers = rest[0] if rest else {}
                return httpx.Response(status, json=body, headers=headers)
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    def install(self, monkeypatch) -> "FakeApi":
        from autonet import http

        monkeypatch.setattr(http, "TRANSPORT", httpx.MockTransport(self.handler))
        return self

    def to(self, url: str) -> list[httpx.Request]:
        return [c for c in self.calls if str(c.url).split("?")[0] == url]

    @staticmethod
    def body(req: httpx.Request) -> dict[str, Any]:
        return json.loads(req.content or b"{}")
