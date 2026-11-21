"""Source adapter for lists you already hold: your own CSV, or the Connections.csv
from LinkedIn's "Get a copy of your data" export (its "Notes:" preamble is skipped)."""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from ..errors import ConfigError, DataError
from ..models import SENIORITIES, Company, Prospect
from ..util import iso
from .base import PeopleSource
from .registry import register

if TYPE_CHECKING:
    from ..campaign import Campaign

ALIASES = {
    "first_name": ["first_name", "first name", "firstname", "first"],
    "last_name": ["last_name", "last name", "lastname", "last"],
    "linkedin_url": ["linkedin_url", "url", "linkedin", "profile url", "linkedin url", "profile"],
    "email": ["email", "email address", "e-mail", "work email"],
    "company": ["company", "company name", "organization", "employer"],
    "company_domain": ["company_domain", "domain", "company domain", "website"],
    "title": ["title", "position", "current_title", "job title"],
    "headline": ["headline"],
    "location": ["location", "city"],
    "schools": ["school", "schools", "education"],
    "past_companies": ["past_companies", "past companies", "previous companies"],
    "summary": ["summary", "about"],
    "team": ["team", "group", "department"],
    "seniority": ["seniority"],
    "prior_contact": ["prior_contact", "prior contact", "met before"],
}


def _norm(h: str) -> str:
    return re.sub(r"[\s_]+", " ", h.strip().lower()).replace(" ", "_")


def _column_map(headers: list[str]) -> dict[str, str]:
    by_norm = {_norm(h): h for h in headers}
    out = {}
    for field, names in ALIASES.items():
        for n in names:
            if _norm(n) in by_norm:
                out[field] = by_norm[_norm(n)]
                break
    return out


def _split(v: str) -> list[str]:
    return [x.strip() for x in re.split(r"[;|]", v or "") if x.strip()]


def read_rows(path: Path) -> list[dict[str, str]]:
    text = path.read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    # LinkedIn exports start with a few "Notes:" lines before the real header.
    start = next((i for i, l in enumerate(lines) if re.search(r"first[ _]?name", l, re.I)), 0)
    return list(csv.DictReader(io.StringIO("\n".join(lines[start:]))))


def row_to_prospect(row: dict[str, str], cols: dict[str, str], ref: str) -> Prospect:
    g = lambda f: (row.get(cols[f]) or "").strip() if f in cols else ""  # noqa: E731
    first, last = g("first_name"), g("last_name")
    if not first or not last:
        raise DataError(f"{ref}: first and last name are required")
    domain = g("company_domain").lower().removeprefix("https://").removeprefix("http://").removeprefix("www.").strip("/")
    seniority = g("seniority").lower() or None
    if seniority and seniority not in SENIORITIES:
        seniority = None
    return Prospect(
        first_name=first,
        last_name=last,
        company=Company(name=g("company"), domain=domain or None),
        source="csv",
        source_ref=ref,
        linkedin_url=g("linkedin_url") or None,
        headline=g("headline"),
        current_title=g("title"),
        seniority=seniority,
        location=g("location"),
        education=[{"school": s} for s in _split(g("schools"))],
        past_companies=_split(g("past_companies")),
        summary=g("summary"),
        team=g("team") or None,
        prior_contact=g("prior_contact").lower() in ("1", "true", "yes", "y"),
        known_email=g("email") or None,
        fetched_at=iso(),
    )


@register("csv")
class CsvSource(PeopleSource):
    def search(self, campaign: "Campaign", limit: int) -> Iterator[Prospect]:
        path = self.options.get("path")
        if not path or not Path(path).is_file():
            raise ConfigError(f"csv source: file not found: {path}")
        rows = read_rows(Path(path))
        if not rows:
            return
        cols = _column_map(list(rows[0].keys()))
        for need in ("first_name", "last_name", "company"):
            if need not in cols:
                raise ConfigError(f"csv source: no column for {need} in {path}")
        for i, row in enumerate(rows, start=2):
            try:
                yield row_to_prospect(row, cols, f"csv:{Path(path).name}:{i}")
            except DataError:
                continue
