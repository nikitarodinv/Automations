"""Campaign filters applied locally, plus qualification scoring (sections 3 and 4)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from .models import SENIORITIES, Prospect
from .util import normalize_company, normalize_text

# Locations excluded unless the campaign sets targeting.allow_eu_uk (section 12, GDPR Art. 14).
EU_UK_TERMS = [
    "united kingdom", "uk", "england", "scotland", "wales", "northern ireland", "london",
    "manchester", "edinburgh", "austria", "belgium", "bulgaria", "croatia", "cyprus", "czechia",
    "czech republic", "denmark", "estonia", "finland", "france", "paris", "germany", "berlin",
    "frankfurt", "munich", "greece", "hungary", "ireland", "dublin", "italy", "milan", "rome",
    "latvia", "lithuania", "luxembourg", "malta", "netherlands", "amsterdam", "poland", "warsaw",
    "portugal", "lisbon", "romania", "slovakia", "slovenia", "spain", "madrid", "barcelona",
    "sweden", "stockholm", "iceland", "norway", "oslo", "liechtenstein",
]


@lru_cache(maxsize=1024)
def _term_regex(term: str) -> re.Pattern[str]:
    """Whole-word, case-insensitive phrase match. Surrounding quotes are optional."""
    t = term.strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
        t = t[1:-1]
    words = [re.escape(w) for w in t.split()]
    return re.compile(r"(?<!\w)" + r"\s+".join(words) + r"(?!\w)", re.IGNORECASE)


def term_in(term: str, text: str) -> bool:
    return bool(_term_regex(term).search(text or ""))


def keyword_text(p: Prospect) -> str:
    return " \n ".join([p.current_title or "", p.headline or "", p.summary or ""])


def keyword_hits(p: Prospect, terms: list[str]) -> list[str]:
    text = keyword_text(p)
    return [t for t in terms if term_in(t, text)]


def company_matches(p: Prospect, entry: dict[str, Any]) -> bool:
    """Domain first, then normalized name."""
    dom = (entry.get("domain") or "").lower()
    if dom and p.company.domain and p.company.domain.lower() == dom:
        return True
    return normalize_company(entry["name"]) == p.company.normalized


def fill_company_domain(p: Prospect, companies: list[dict[str, Any]]) -> None:
    """Sources like the LinkedIn export carry no domain; borrow it from the campaign list."""
    if p.company.domain:
        return
    for c in companies:
        if c.get("domain") and normalize_company(c["name"]) == p.company.normalized:
            p.company.domain = c["domain"]
            return


def location_matches(location: str, wanted: list[str]) -> bool:
    if not wanted:
        return True
    loc = normalize_text(location or "")
    for w in wanted:
        city = normalize_text(w.split(",")[0])
        if city and city in loc:
            return True
    return False


def is_eu_uk(location: str) -> bool:
    loc = location or ""
    return any(term_in(t, loc) for t in EU_UK_TERMS)


def shared_school(p: Prospect, schools: list[str]) -> str | None:
    """The campaign's spelling of the first school the prospect shares, if any."""
    mine = {normalize_company(s): s for s in schools}
    for s in p.schools:
        if normalize_company(s) in mine:
            return mine[normalize_company(s)]
    return None


def shared_employer(p: Prospect, sender_past: list[str]) -> str | None:
    mine = {normalize_company(s) for s in sender_past}
    for c in p.past_companies:
        if normalize_company(c) in mine:
            return c
    return None


def infer_seniority(title: str) -> str | None:
    t = (title or "").lower()
    rules = [
        ("executive", r"\b(managing director|md|partner|chief|ceo|cfo|coo|cto|founder|(?<!vice )president|head of)\b"),
        ("senior", r"\b(director|principal|senior vice president|svp|executive director)\b"),
        ("mid", r"\b(vice president|vp|manager|lead)\b"),
        ("junior", r"\b(associate|senior analyst)\b"),
        ("entry", r"\b(analyst|intern|assistant|coordinator|junior)\b"),
    ]
    for level, pat in rules:
        if re.search(pat, t):
            return level
    return None


@dataclass
class FilterResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)  # why it failed
    keywords_hit: list[str] = field(default_factory=list)


def apply_filters(p: Prospect, targeting: dict[str, Any]) -> FilterResult:
    """Campaign filters, run locally. Excludes always beat includes."""
    reasons: list[str] = []
    kw = targeting.get("keywords", {})
    text = keyword_text(p)
    hits = keyword_hits(p, kw.get("any", []))
    if kw.get("any") and not hits:
        reasons.append("keywords.any: no match")
    missing_all = [t for t in kw.get("all", []) if not term_in(t, text)]
    if missing_all:
        reasons.append(f"keywords.all: missing {missing_all}")
    excl = keyword_hits(p, kw.get("exclude", []))
    if excl:
        reasons.append(f"keywords.exclude: {excl}")

    titles = targeting.get("titles", {})
    title = p.current_title or p.headline
    if titles.get("include") and not any(term_in(t, title) for t in titles["include"]):
        reasons.append("titles.include: no match")
    tex = [t for t in titles.get("exclude", []) if term_in(t, title)]
    if tex:
        reasons.append(f"titles.exclude: {tex}")

    comps = targeting.get("companies", {})
    if comps.get("include") and not any(company_matches(p, c) for c in comps["include"]):
        reasons.append("companies.include: no match")
    if any(company_matches(p, c) for c in comps.get("exclude", [])):
        reasons.append("companies.exclude")

    if not location_matches(p.location, targeting.get("locations", [])):
        reasons.append("locations: no match")
    if not targeting.get("allow_eu_uk", False) and is_eu_uk(p.location):
        reasons.append("locations: EU/UK excluded by default")

    return FilterResult(passed=not reasons, reasons=reasons, keywords_hit=hits)


def qualification_score(p: Prospect, targeting: dict[str, Any], sender: dict[str, Any], hits: list[str]) -> int:
    score = 3 * len(hits)
    if shared_school(p, targeting.get("schools", [])):
        score += 5
    if shared_employer(p, sender.get("past_companies", [])):
        score += 5
    title = normalize_text(p.current_title or "")
    if title and any(normalize_text(t) == title for t in targeting.get("titles", {}).get("include", [])):
        score += 2
    wanted = targeting.get("seniority", [])
    if wanted and p.seniority in SENIORITIES and p.seniority not in wanted:
        score -= 5
    return score
