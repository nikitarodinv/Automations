"""Normalized records passed between stages."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .util import iso, normalize_company, normalize_linkedin_url, short_hash

SENIORITIES = ["entry", "junior", "mid", "senior", "executive"]


def make_prospect_id(linkedin_url: str | None, first: str, last: str, domain: str | None) -> str:
    if linkedin_url:
        return "p_" + short_hash(normalize_linkedin_url(linkedin_url))
    key = f"{first.strip().lower()} {last.strip().lower()} {(domain or '').strip().lower()}"
    return "p_" + short_hash(key)


@dataclass
class Company:
    name: str
    domain: str | None = None

    @property
    def normalized(self) -> str:
        return normalize_company(self.name)


@dataclass
class Prospect:
    first_name: str
    last_name: str
    company: Company
    source: str
    source_ref: str = ""
    prospect_id: str = ""
    linkedin_url: str | None = None
    headline: str = ""
    current_title: str = ""
    seniority: str | None = None
    location: str = ""
    education: list[dict[str, Any]] = field(default_factory=list)
    past_companies: list[str] = field(default_factory=list)
    summary: str = ""
    team: str | None = None
    prior_contact: bool = False
    known_email: str | None = None  # an address the source already carries (csv, hunter_domain)
    fetched_at: str = ""
    match: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.linkedin_url:
            self.linkedin_url = normalize_linkedin_url(self.linkedin_url)
        if not self.prospect_id:
            self.prospect_id = make_prospect_id(
                self.linkedin_url, self.first_name, self.last_name, self.company.domain
            )
        if self.summary and len(self.summary) > 600:
            self.summary = self.summary[:600]
        if not self.fetched_at:
            self.fetched_at = iso()

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def schools(self) -> list[str]:
        return [e.get("school", "") for e in self.education if e.get("school")]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Prospect":
        d = dict(d)
        comp = d.pop("company", {}) or {}
        if isinstance(comp, str):
            comp = {"name": comp}
        known = {f for f in cls.__dataclass_fields__}
        d = {k: v for k, v in d.items() if k in known}
        return cls(company=Company(**comp), **d)


@dataclass
class EmailResult:
    """Shared finder result format (section 4, pool rule 6)."""

    email: str | None
    score: int | None
    status: str  # valid | accept_all | unknown | invalid | not_found | claimed
    provider: str
    sources: list[dict[str, Any]] = field(default_factory=list)
    credits_used: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)
