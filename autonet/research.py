"""Stage 4: one public, work-related detail to open each email with (section 7).

Runs after Stage 3, so research is only spent on people you can email. Never opens LinkedIn.
When nothing usable turns up, the email uses the profile-based opener: research never blocks a draft.

Per prospect: gather pages (Hunter's sources, then up to 3 searches) -> filter -> fetch politely ->
identity check (name + company/school, then Jev at 0.8) -> one extraction call -> verify in code
(quote word for word, fields inside the quote, date, age, work topics only) -> keep a primary and a backup."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .errors import AuthError, DataError, QuotaError, TransientError
from .fetch import Page
from .matching import term_in
from .models import Prospect
from .search import SearchResult
from .util import short_hash

# type -> (tier, required fields, max age in days; None = no limit)
HOOK_RULES: dict[str, tuple[int, list[str], int | None]] = {
    "note": (0, ["text"], None),
    "talk": (1, ["event", "topic"], 730),
    "writing": (1, ["title", "outlet"], 730),
    "podcast": (1, ["show", "topic"], 730),
    "deal": (2, ["client", "deal_type"], 180),
    "promotion": (2, ["new_title"], 180),
    "company_news": (3, ["topic"], 180),
}
FIELDS = ["event", "topic", "title", "outlet", "show", "client", "deal_type", "new_title"]
# Anything outside work never counts as a hook (section 7).
PERSONAL = re.compile(
    r"\b(wife|husband|spouse|partner's|daughter|son|kids?|children|baby|pregnan\w*|wedding|married|divorce\w*|"
    r"family|funeral|passed away|illness|cancer|diagnos\w*|surgery|hospital\w*|health|therapy|church|mosque|"
    r"synagogue|temple|faith|religio\w*|prayer|election|democrat\w*|republican\w*|politic\w*|vot(e|ed|ing)|"
    r"campaign donation|lives in|his home|her home|their home|neighborhood|instagram|tiktok|facebook|snapchat)\b",
    re.I,
)
EXTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "hooks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": [t for t in HOOK_RULES if t != "note"]},
                    "page": {"type": "integer"},
                    "quote": {"type": "string"},
                    "date": {"type": "string"},
                    **{f: {"type": "string"} for f in FIELDS},
                },
                "required": ["type", "page", "quote", "date", *FIELDS],
                "additionalProperties": False,
            },
        }
    },
    "required": ["hooks"],
    "additionalProperties": False,
}
PROMPT_PATH = Path(__file__).resolve().parents[1] / "research" / "extract_prompt.md"
EXCERPT_CHARS = 6000


# ---------------------------------------------------------------------------- text helpers
def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("–", "-").replace("—", "-").replace(" ", " ")
    return re.sub(r"\s+", " ", s).strip()


def quote_on_page(quote: str, text: str) -> bool:
    """Word for word, ignoring only whitespace runs and typographic quote/dash variants."""
    q = _norm(quote)
    return len(q) >= 20 and q in _norm(text)


def parse_date(s: str | None) -> date | None:
    if not s:
        return None
    s = s.strip()[:10]
    for fmt, n in (("%Y-%m-%d", 10), ("%Y-%m", 7), ("%Y", 4)):
        try:
            from datetime import datetime

            return datetime.strptime(s[:n], fmt).date()
        except ValueError:
            continue
    return None


def excerpt(text: str, name: str, limit: int = EXCERPT_CHARS) -> str:
    """The parts of a page around the person's name (the quote check still runs on the full text)."""
    if len(text) <= limit:
        return text
    spans = [m.start() for m in re.finditer(re.escape(name), text, re.I)][:3]
    if not spans:
        return text[:limit]
    width = limit // max(1, len(spans))
    out, last = [], -1
    for s in spans:
        a, b = max(0, s - width // 2), min(len(text), s + width // 2)
        if a <= last:
            a = last
        out.append(text[a:b])
        last = b
    return "\n…\n".join(out)


# ---------------------------------------------------------------------------- identity
def ambiguous_name(results: list[SearchResult], p: Prospect) -> bool:
    """Several people with this name in the search results -> require both company and school."""
    others = [r for r in results if term_in(p.full_name, f"{r.title} {r.description}")
              and not term_in(p.company.name, f"{r.title} {r.description} {r.url}")]
    return len(others) >= 2


def page_mentions_person(page: Page, p: Prospect, strict: bool) -> bool:
    text = page.text
    if not term_in(p.full_name, text):
        return False
    company = bool(p.company.name and term_in(p.company.name, text)) or bool(
        p.company.domain and p.company.domain in page.url)
    school = any(term_in(s, text) for s in p.schools)
    if strict and p.schools:
        return company and school
    return company or school


# ---------------------------------------------------------------------------- verification
@dataclass
class Verdict:
    hook: dict[str, Any] | None
    reason: str = ""


def verify_hook(raw: dict[str, Any], pages: list[Page], p: Prospect, today: date | None = None) -> Verdict:
    today = today or date.today()
    typ = raw.get("type")
    if typ not in HOOK_RULES or typ == "note":
        return Verdict(None, f"unknown type {typ}")
    tier, required, max_age = HOOK_RULES[typ]
    idx = raw.get("page")
    if not isinstance(idx, int) or not (0 <= idx < len(pages)):
        return Verdict(None, "bad page number")
    page = pages[idx]
    quote = (raw.get("quote") or "").strip()
    if not quote_on_page(quote, page.text):
        return Verdict(None, "quote not found word for word on the page")
    fields = {f: (raw.get(f) or "").strip() for f in required}
    nq = _norm(quote).lower()
    for f, v in fields.items():
        if not v:
            return Verdict(None, f"missing {f}")
        if _norm(v).lower() not in nq:
            return Verdict(None, f"{f} '{v}' is not in the quote")
    if PERSONAL.search(quote) or any(PERSONAL.search(v) for v in fields.values()):
        return Verdict(None, "not a work topic")
    d = parse_date(raw.get("date")) or parse_date(page.date)
    if d is None:
        return Verdict(None, "no date that parses")
    if d > today + timedelta(days=1):
        return Verdict(None, "dated in the future")
    if max_age is not None and (today - d).days > max_age:
        return Verdict(None, f"older than {max_age} days")
    hook = {
        "hook_id": "h_" + short_hash(p.prospect_id, page.url, _norm(quote)),
        "prospect_id": p.prospect_id, "tier": tier, "type": typ, "fields": fields, "quote": quote,
        "source_url": page.url, "source_date": d.isoformat(), "found_via": page.found_via, "verified": True,
    }
    return Verdict(hook)


def choose(hooks: list[dict[str, Any]], keep: int = 2) -> list[dict[str, Any]]:
    """Lowest tier first, newest first; one primary and one backup."""
    seen, out = set(), []
    for h in sorted(hooks, key=lambda h: (h["tier"], -int(h["source_date"].replace("-", "")))):
        if h["hook_id"] not in seen:
            seen.add(h["hook_id"])
            out.append(h)
    return out[:keep]


# ---------------------------------------------------------------------------- extraction
def extraction_request(p: Prospect, pages: list[Page]) -> str:
    person = {"name": p.full_name, "company": p.company.name, "title": p.current_title, "schools": p.schools}
    blocks = [f"<person>{person}</person>", "<pages>"]
    for i, pg in enumerate(pages):
        blocks.append(f'<page number="{i}" url="{pg.url}" date="{pg.date or ""}">\n{excerpt(pg.text, p.full_name)}\n</page>')
    blocks.append("</pages>")
    blocks.append("Return the hooks for this person as JSON.")
    return "\n".join(blocks)


@dataclass
class ResearchResult:
    hooks: list[dict[str, Any]] = field(default_factory=list)
    searches: int = 0
    pages: int = 0
    rejected: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class Researcher:
    """Wires search, fetching, Jev's identity check and the extraction model together.
    Each collaborator may be None (that step is skipped with a note)."""

    def __init__(self, *, research_cfg: dict[str, Any], search: Any, fetcher: Any, llm: Any, jev: Any, ledger: Any,
                 search_ok: Any = None):
        self.cfg = research_cfg
        self.search = search
        self.fetcher = fetcher
        self.llm = llm
        self.jev = jev
        self.ledger = ledger
        self.search_ok = search_ok or (lambda: True)
        self.disabled: dict[str, str] = {}
        self.prompt = PROMPT_PATH.read_text()

    def queries(self, p: Prospect) -> list[str]:
        q = [f'"{p.full_name}" "{p.company.name}"', f'"{p.full_name}" talk OR panel OR podcast OR article']
        if p.company.domain:
            q.append(f'site:{p.company.domain} "{p.full_name}"')
        return q[: int(self.cfg.get("max_searches_per_prospect", 3))]

    def _search(self, p: Prospect, res: ResearchResult) -> list[SearchResult]:
        out: list[SearchResult] = []
        if self.search is None or "search" in self.disabled:
            return out
        for q in self.queries(p):
            try:
                self.ledger.require_search()
            except QuotaError as e:
                self.disabled["search"] = str(e)
                break
            if not self.search_ok():
                self.disabled["search"] = "monthly search budget reached"
                break
            try:
                hits = self.search.search(q)
            except AuthError as e:
                self.disabled["search"] = str(e)
                break
            except (TransientError, DataError) as e:
                res.notes.append(f"search failed: {e}")
                continue
            finally:
                self.ledger.record(getattr(self.search, "name", "search"), "web-search", searches=1)
            res.searches += 1
            out += hits
        return out

    def _same_person(self, p: Prospect, page: Page) -> bool:
        """Jev answers one yes/no question per page; below 0.8 the page is dropped."""
        if self.jev is None or "jev" in self.disabled:
            return True
        try:
            self.ledger.require_llm_call()
            prob = self.jev.same_person(p, page.url, excerpt(page.text, p.full_name, 3000))
            self.ledger.record("jev", "identity", llm_calls=1)
        except QuotaError as e:
            self.disabled["jev"] = str(e)
            return True
        except (AuthError, TransientError, DataError) as e:
            self.disabled["jev"] = f"identity check unavailable ({e}); pages checked by name only"
            return True
        return prob >= 0.8

    def run(self, p: Prospect, hunter_sources: list[dict[str, Any]]) -> ResearchResult:
        res = ResearchResult()
        max_pages = int(self.cfg.get("max_pages", 10))
        urls: list[tuple[str, str]] = [(s["uri"], "hunter_source") for s in hunter_sources if s.get("uri")]
        results = self._search(p, res)
        urls += [(r.url, "search") for r in results]
        strict = ambiguous_name(results, p)
        pages: list[Page] = []
        seen: set[str] = set()
        for url, via in urls:
            if len(pages) >= max_pages:
                break
            key = url.split("#")[0].rstrip("/")
            if key in seen:
                continue
            seen.add(key)
            page = self.fetcher.fetch(url, found_via=via)
            if page is None:
                continue
            res.pages += 1
            if not page_mentions_person(page, p, strict):
                res.rejected.append(f"{url}: doesn't name this person")
                continue
            if not self._same_person(p, page):
                res.rejected.append(f"{url}: Jev says a different person")
                continue
            pages.append(page)
        if not pages:
            return res
        if self.llm is None or "llm" in self.disabled:
            return res
        try:
            self.ledger.require_llm_call()
        except QuotaError as e:
            self.disabled["llm"] = str(e)
            return res
        try:
            out = self.llm.json(system=self.prompt, user=extraction_request(p, pages), schema=EXTRACT_SCHEMA,
                                effort=self.cfg.get("effort", "medium"))
        except AuthError as e:
            self.disabled["llm"] = f"extraction unavailable ({e})"
            return res
        except (TransientError, DataError) as e:
            res.notes.append(f"extraction failed for {p.full_name}: {e}")
            return res
        finally:
            self.ledger.record("claude", "extract", llm_calls=1)
        verified = []
        for raw in (out.get("hooks") or [])[:3]:
            v = verify_hook(raw, pages, p)
            if v.hook:
                verified.append(v.hook)
            else:
                res.rejected.append(f"{raw.get('type')}: {v.reason}")
        res.hooks = choose(verified)
        return res
