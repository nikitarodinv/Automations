"""The three roles a provider can fill. Adapters only translate requests and responses;
the pipeline owns budgets, pacing, retries, the ledger and dedupe."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterator

from ..models import EmailResult, Prospect

if TYPE_CHECKING:
    from ..campaign import Campaign


class Provider:
    name: str = "provider"
    # Credits charged per outcome; the ledger records these.
    cost: dict[str, float] = {}

    def __init__(self, **options: Any):
        self.options = options


class PeopleSource(Provider):
    def search(self, campaign: "Campaign", limit: int) -> Iterator[Prospect]:
        raise NotImplementedError

    def supported_filters(self) -> set[str]:
        """Filters the provider applies itself. Everything else runs locally."""
        return set()


class EmailFinder(Provider):
    def find(self, prospect: Prospect) -> EmailResult:
        raise NotImplementedError


class Enricher(Provider):
    def enrich(self, prospect: Prospect) -> Prospect:
        raise NotImplementedError
