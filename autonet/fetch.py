"""Polite page fetching for Stage 4: robots.txt honored, at most 1 request a second per domain,
10-second timeout, HTML and plain text only, text cut to 20,000 characters, login pages skipped.
Fetched text is untrusted: it only ever reaches the extraction call as quoted data."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from . import http

USER_AGENT = "autonetworker/0.3 (personal networking research; respects robots.txt)"
MAX_CHARS = 20_000
MAX_BYTES = 2_000_000
SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "footer", "form", "iframe", "template"}
BLOCK_TAGS = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article", "header", "blockquote"}
LOGIN_PATH = re.compile(r"/(login|log-in|signin|sign-in|sign_in|auth|sso|account/login)(/|$|\?)", re.I)
DATE_META = ("article:published_time", "datePublished", "date", "pubdate", "publish-date", "og:updated_time")


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0
        self.title = ""
        self._in_title = False
        self.password = False
        self.date: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag in SKIP_TAGS:
            self.skip += 1
        if tag == "title":
            self._in_title = True
        if tag == "input" and a.get("type", "").lower() == "password":
            self.password = True
        if tag == "meta" and not self.date:
            key = a.get("property") or a.get("name") or a.get("itemprop") or ""
            if key in DATE_META and a.get("content"):
                self.date = a["content"][:10]
        if tag == "time" and not self.date and a.get("datetime"):
            self.date = a["datetime"][:10]
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS and self.skip:
            self.skip -= 1
        if tag == "title":
            self._in_title = False
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self.skip:
            self.parts.append(data)


def html_to_text(html: str) -> tuple[str, str, bool, str | None]:
    p = _Text()
    try:
        p.feed(html)
    except Exception:  # malformed HTML: keep what parsed
        pass
    text = "".join(p.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return text, p.title.strip(), p.password, p.date


@dataclass
class Page:
    url: str
    text: str
    title: str = ""
    date: str | None = None
    found_via: str = "search"


@dataclass
class Fetcher:
    blocked_domains: list[str] = field(default_factory=lambda: ["linkedin.com"])
    timeout: float = 10.0
    min_interval: float = 1.0
    user_agent: str = USER_AGENT
    skipped: dict[str, str] = field(default_factory=dict)   # url -> why
    _robots: dict[str, RobotFileParser | None] = field(default_factory=dict)
    _last: dict[str, float] = field(default_factory=dict)

    def blocked(self, url: str) -> str | None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return "not http"
        host = (parts.hostname or "").lower()
        for d in self.blocked_domains:
            d = d.lower().lstrip(".")
            if host == d or host.endswith("." + d):
                return f"blocked domain {d}"
        if LOGIN_PATH.search(parts.path):
            return "login page"
        if re.search(r"\.(pdf|docx?|pptx?|xlsx?|zip|jpe?g|png|gif|mp[34])$", parts.path, re.I):
            return "not a text page"
        return None

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self.timeout, transport=http.TRANSPORT, follow_redirects=True,
                            max_redirects=3, headers={"User-Agent": self.user_agent})

    def _throttle(self, host: str) -> None:
        now = http.MONOTONIC()
        last = self._last.get(host)
        if last is not None and now - last < self.min_interval:
            http.SLEEP(self.min_interval - (now - last))
            now = last + self.min_interval
        self._last[host] = now

    def allowed_by_robots(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            rp: RobotFileParser | None = RobotFileParser()
            try:
                self._throttle(parts.netloc)
                with self._client() as c:
                    r = c.get(origin + "/robots.txt")
                if r.status_code >= 500:
                    rp = None                      # unreachable: treat as disallowed (RFC 9309)
                elif r.status_code >= 400:
                    rp.parse([])                   # no robots.txt: everything allowed
                else:
                    rp.parse(r.text.splitlines())
            except httpx.HTTPError:
                rp = None
            self._robots[origin] = rp
        rp = self._robots[origin]
        return bool(rp and rp.can_fetch(self.user_agent, url))

    def fetch(self, url: str, found_via: str = "search") -> Page | None:
        why = self.blocked(url)
        if why:
            self.skipped[url] = why
            return None
        if not self.allowed_by_robots(url):
            self.skipped[url] = "robots.txt"
            return None
        host = urlsplit(url).netloc
        self._throttle(host)
        try:
            with self._client() as c:
                r = c.get(url)
        except httpx.HTTPError as e:
            self.skipped[url] = type(e).__name__
            return None
        final = str(r.url)
        if r.status_code in (401, 403) or self.blocked(final):
            self.skipped[url] = "login or blocked after redirect"
            return None
        if r.status_code != 200:
            self.skipped[url] = f"HTTP {r.status_code}"
            return None
        ctype = r.headers.get("content-type", "").lower()
        if len(r.content) > MAX_BYTES:
            self.skipped[url] = "too large"
            return None
        if "text/html" in ctype or "xhtml" in ctype:
            text, title, password, date = html_to_text(r.text)
            if password:
                self.skipped[url] = "login page"
                return None
        elif "text/plain" in ctype:
            text, title, date = r.text, "", None
        else:
            self.skipped[url] = f"content type {ctype or 'unknown'}"
            return None
        if len(text) < 80:
            self.skipped[url] = "no text"
            return None
        return Page(url=final, text=text[:MAX_CHARS], title=title, date=date, found_via=found_via)
