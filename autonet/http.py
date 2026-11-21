"""Shared HTTP client for every provider: error classes (section 11), retries with backoff and
jitter, Retry-After, client-side rate limits, and keys kept out of URLs and logs.

Tests swap the network for `httpx.MockTransport` by setting `TRANSPORT`, and skip real waiting by
setting `SLEEP`."""

from __future__ import annotations

import random
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from .errors import AuthError, DataError, QuotaError, TransientError

TRANSPORT: httpx.BaseTransport | None = None   # tests: httpx.MockTransport(handler)
SLEEP: Callable[[float], None] = time.sleep
MONOTONIC: Callable[[], float] = time.monotonic

# Classes an adapter can map a response to. ok / not_found / claimed are returned, the rest raise.
CLASSES = {"ok", "not_found", "claimed", "auth", "quota", "transient", "data"}


class RateLimiter:
    def __init__(self, per_second: float | None = None, per_minute: float | None = None):
        self.per_second = per_second
        self.per_minute = per_minute
        self.calls: deque[float] = deque()

    def wait(self) -> None:
        now = MONOTONIC()
        while self.calls and now - self.calls[0] > 60:
            self.calls.popleft()
        delay = 0.0
        if self.per_second:
            recent = [t for t in self.calls if now - t < 1]
            if len(recent) >= self.per_second:
                delay = max(delay, 1 - (now - recent[-int(self.per_second)]))
        if self.per_minute and len(self.calls) >= self.per_minute:
            delay = max(delay, 60 - (now - self.calls[-int(self.per_minute)]))
        if delay > 0:
            SLEEP(delay)
            now += delay
        self.calls.append(now)


def default_classify(status: int, errors: dict[str, str]) -> str:
    """Map an HTTP status to a class using a manifest-style table ({"401": auth, "5xx": transient})."""
    if 200 <= status < 300:
        return errors.get(str(status), "ok")
    if str(status) in errors:
        return errors[str(status)]
    if f"{str(status)[0]}xx" in errors:
        return errors[f"{str(status)[0]}xx"]
    if status in (401,):
        return "auth"
    if status == 429 or status >= 500:
        return "transient"
    if status == 404:
        return "not_found"
    return "data"


@dataclass
class Reply:
    status: int
    cls: str
    body: Any
    headers: httpx.Headers
    error_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.cls == "ok"


@dataclass
class ApiClient:
    service: str
    headers: dict[str, str] = field(default_factory=dict)
    query_auth: dict[str, str] = field(default_factory=dict)   # only for APIs that accept nothing else
    errors: dict[str, str] = field(default_factory=dict)
    limiter: RateLimiter = field(default_factory=RateLimiter)
    timeout: float = 10.0
    backoff: tuple[float, ...] = (1, 2, 4, 8, 16)
    classify: Callable[[int, Any, httpx.Headers], tuple[str, str | None]] | None = None
    secrets: list[str] = field(default_factory=list)

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self.timeout, transport=TRANSPORT, headers={"User-Agent": "autonetworker/0.2"})

    def redact(self, text: str) -> str:
        for s in self.secrets:
            if s:
                text = text.replace(s, "***")
        return text

    def request(self, method: str, url: str, *, params: dict | None = None, json: Any = None,
                retry_transient: bool = True) -> Reply:
        params = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        params.update(self.query_auth)
        attempt = 0
        while True:
            self.limiter.wait()
            try:
                with self._client() as c:
                    r = c.request(method, url, params=params, json=json, headers=self.headers)
                body: Any
                try:
                    body = r.json()
                except ValueError:
                    body = r.text
                if self.classify:
                    cls, err_id = self.classify(r.status_code, body, r.headers)
                else:
                    cls, err_id = default_classify(r.status_code, self.errors), _error_id(body)
                reply = Reply(r.status_code, cls, body, r.headers, err_id)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                reply = Reply(0, "transient", None, httpx.Headers(), self.redact(type(e).__name__))

            if reply.cls == "transient" and retry_transient and attempt < len(self.backoff):
                wait = _retry_after(reply.headers) or self.backoff[attempt] * (1 + random.random() * 0.25)
                SLEEP(wait)
                attempt += 1
                continue
            return self._raise_or_return(reply)

    def _raise_or_return(self, reply: Reply) -> Reply:
        detail = f"HTTP {reply.status}" + (f" {reply.error_id}" if reply.error_id else "")
        if reply.cls == "auth":
            raise AuthError(self.service, f"key rejected ({detail})")
        if reply.cls == "quota":
            raise QuotaError(f"{self.service}: plan limit reached ({detail})")
        if reply.cls == "transient":
            raise TransientError(f"{self.service}: still failing after retries ({detail})")
        if reply.cls == "data":
            raise DataError(f"{self.service}: request rejected ({detail})")
        return reply


def _retry_after(headers: httpx.Headers) -> float | None:
    v = headers.get("Retry-After")
    if v and v.strip().isdigit():
        return float(v)
    return None


def _error_id(body: Any) -> str | None:
    """Pull an error id out of the common error envelopes (Hunter, PDL, Apollo, Prospeo)."""
    if not isinstance(body, dict):
        return None
    if isinstance(body.get("errors"), list) and body["errors"]:
        e = body["errors"][0]
        return e.get("id") or e.get("details")
    if isinstance(body.get("error"), dict):
        return body["error"].get("type") or body["error"].get("message")
    if isinstance(body.get("error_details"), dict):
        return body["error_details"].get("code")
    if body.get("error_code"):
        return str(body["error_code"])
    return None


def dig(obj: Any, path: str | None) -> Any:
    """Read a dotted path like 'data.email' or 'emails.0.value'."""
    if not path:
        return None
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur
