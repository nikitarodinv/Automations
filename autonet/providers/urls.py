"""`source: urls`: LinkedIn profile URLs you paste ("draft emails to these five people I found").
Nothing fetches the LinkedIn page. An enricher (PDL, then Apollo) fills in name and company by URL;
failing that, Hunter's linkedin_handle parameter does it in Stage 3."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from ..errors import ConfigError
from ..models import Company, Prospect
from ..util import iso
from .base import Enricher, PeopleSource
from .registry import register

if TYPE_CHECKING:
    from ..campaign import Campaign


def read_urls(options: dict) -> list[str]:
    urls = list(options.get("urls") or [])
    if options.get("path"):
        path = Path(options["path"])
        if not path.is_file():
            raise ConfigError(f"urls source: file not found: {path}")
        urls += [ln.strip() for ln in path.read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    bad = [u for u in urls if "linkedin.com/in/" not in u.lower()]
    if bad:
        raise ConfigError(f"urls source: not LinkedIn profile URLs: {', '.join(bad[:3])}")
    return list(dict.fromkeys(urls))


@register("urls")
class UrlsSource(PeopleSource):
    enrichers: list[Enricher] = []   # set by the pool

    def search(self, campaign: "Campaign", limit: int) -> Iterator[Prospect]:
        for url in read_urls(self.options)[:limit]:
            bare = Prospect(first_name="", last_name="", company=Company(""), source="urls", source_ref=f"urls:{url}",
                            linkedin_url=url, fetched_at=iso())
            p = None
            for e in self.enrichers:
                p = e.enrich(bare)
                if p:
                    break
            p = p or bare
            p.source = "urls"
            p.match["user_selected"] = True   # you picked these people: campaign filters don't apply
            yield p
