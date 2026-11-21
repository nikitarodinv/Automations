"""Offline adapters for --dry-run and tests: prospects from JSONL, email results from YAML.
No network calls, no credits. (Milestone 2 adds Hunter's test-api-key for the live smoke test.)"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

import yaml

from ..models import EmailResult, Prospect
from .base import EmailFinder, PeopleSource
from .registry import register

if TYPE_CHECKING:
    from ..campaign import Campaign


@register("fixture")
class FixtureSource(PeopleSource):
    def search(self, campaign: "Campaign", limit: int) -> Iterator[Prospect]:
        path = Path(self.options["path"])
        for line in path.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith("//"):
                d = json.loads(line)
                d.setdefault("source", "fixture")
                yield Prospect.from_dict(d)


@register("fixture_finder")
class FixtureFinder(EmailFinder):
    """Mirrors Hunter's billing: 1 credit per address returned, 0 for a miss."""

    cost = {"found": 1, "miss": 0}

    def __init__(self, **options):
        super().__init__(**options)
        self.table = yaml.safe_load(Path(options["path"]).read_text()) or {}

    def find(self, p: Prospect) -> EmailResult:
        key = f"{p.first_name} {p.last_name}|{p.company.domain}"
        r = self.table.get(key)
        if r is None or r == "not_found":
            return EmailResult(email=None, score=None, status="not_found", provider=self.name)
        if r == "claimed":
            return EmailResult(email=None, score=None, status="claimed", provider=self.name)
        return EmailResult(
            email=r["email"],
            score=r.get("score"),
            status=r.get("status", "valid"),
            provider=self.name,
            sources=r.get("sources", []),
            credits_used=self.cost["found"],
        )
