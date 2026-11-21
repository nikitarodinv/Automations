"""The three roles a provider can fill. Adapters only translate requests and responses;
the pool owns allowances, pacing, budgets, the ledger and dedupe."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Iterator

from ..models import EmailResult, Prospect

if TYPE_CHECKING:
    from ..campaign import Campaign
    from ..http import ApiClient
    from .manifest import Manifest


class Provider:
    name: str = "provider"
    # Which allowance this adapter spends from (pool rule 1: one allowance per provider).
    account: str | None = None
    # Credits charged per outcome; the ledger records these.
    cost: dict[str, float] = {}

    def __init__(self, manifest: "Manifest | None" = None, client: "ApiClient | None" = None, **options: Any):
        self.manifest = manifest
        self.client = client
        self.options = options
        if manifest is not None:
            self.account = self.account or manifest.name
            if manifest.finder and manifest.finder.get("cost") and not self.cost:
                self.cost = dict(manifest.finder["cost"])
        # Set by the pool: returns True if `credits` more may be spent right now.
        self.can_spend: Callable[[float], bool] = lambda credits: True
        # Set by the pool: record a paid (or free) call in the ledger.
        self.charge: Callable[..., None] = lambda endpoint, credits=0, status=None: None

    def balance(self) -> tuple[float, str | None] | None:
        """(credits remaining, reset date) if the provider reports it, else None."""
        return None


class PeopleSource(Provider):
    def search(self, campaign: "Campaign", limit: int) -> Iterator[Prospect]:
        raise NotImplementedError

    def supported_filters(self) -> set[str]:
        """Filters the provider applies itself. Everything else runs locally."""
        return set()

    # Sources that page through results keep a cursor between runs (set by the pool).
    get_cursor: Callable[[str], str | None] = staticmethod(lambda key: None)  # type: ignore[assignment]
    set_cursor: Callable[[str, str | None], None] = staticmethod(lambda key, value: None)  # type: ignore[assignment]


class EmailFinder(Provider):
    def find(self, prospect: Prospect) -> EmailResult:
        raise NotImplementedError


class Enricher(Provider):
    def enrich(self, prospect: Prospect) -> Prospect | None:
        raise NotImplementedError
