"""Web search adapters for Stage 4. Brave by default; any search API with an adapter works.
Docs (checked 2026-11-21): GET https://api.search.brave.com/res/v1/web/search, header
X-Subscription-Token, results in web.results[] with title / url / description."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from .errors import ConfigError
from .http import ApiClient, RateLimiter


@dataclass
class SearchResult:
    title: str
    url: str
    description: str = ""
    age: str | None = None


class SearchProvider:
    name = "search"
    key_env = ""

    def search(self, query: str, count: int = 10) -> list[SearchResult]:
        raise NotImplementedError


class BraveSearch(SearchProvider):
    name = "brave"
    key_env = "BRAVE_API_KEY"
    URL = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, key: str | None = None):
        key = key or os.environ.get(self.key_env)
        if not key:
            raise ConfigError(f"search provider brave: set {self.key_env}")
        self.client = ApiClient(
            service="brave search", headers={"X-Subscription-Token": key, "Accept": "application/json"},
            errors={"401": "auth", "403": "auth", "402": "quota", "422": "data", "429": "transient", "5xx": "transient"},
            limiter=RateLimiter(per_second=1), secrets=[key],
        )

    def search(self, query: str, count: int = 10) -> list[SearchResult]:
        r = self.client.request("GET", self.URL, params={"q": query, "count": min(20, count), "safesearch": "moderate"})
        results: list[dict[str, Any]] = ((r.body or {}).get("web") or {}).get("results") or [] if isinstance(r.body, dict) else []
        return [SearchResult(x.get("title") or "", x.get("url") or "", x.get("description") or "", x.get("page_age") or x.get("age"))
                for x in results if x.get("url")]


PROVIDERS: dict[str, type[SearchProvider]] = {"brave": BraveSearch}


def get_search(name: str) -> SearchProvider:
    if name not in PROVIDERS:
        raise ConfigError(f"research.search_provider '{name}': known: {', '.join(PROVIDERS)}")
    return PROVIDERS[name]()
