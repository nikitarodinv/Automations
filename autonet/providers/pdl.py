"""People Data Labs: Person Search (source) and Person Enrichment (enricher for `source: urls`).
Docs: https://docs.peopledatalabs.com/docs/reference-person-search-api"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterator

from ..errors import DataError
from ..matching import infer_seniority
from ..models import Company, Prospect
from ..util import iso, normalize_company
from .base import Enricher, PeopleSource
from .registry import register

if TYPE_CHECKING:
    from ..campaign import Campaign

API = "https://api.peopledatalabs.com/v5"
LEVELS = {"cxo": "executive", "owner": "executive", "partner": "executive", "vp": "senior", "director": "senior",
          "manager": "mid", "senior": "junior", "entry": "entry", "training": "entry", "unpaid": "entry"}


def _domain(website: str | None) -> str | None:
    if not website:
        return None
    w = website.lower().removeprefix("https://").removeprefix("http://").removeprefix("www.")
    return w.split("/")[0] or None


def _year(date: str | None) -> int | None:
    try:
        return int(str(date)[:4]) if date else None
    except ValueError:
        return None


def record_to_prospect(r: dict[str, Any], source: str = "pdl") -> Prospect | None:
    first, last = r.get("first_name") or "", r.get("last_name") or ""
    if not first or not last:
        return None
    levels = r.get("job_title_levels") or []
    title = r.get("job_title") or ""
    seniority = infer_seniority(title) or next((LEVELS[l] for l in levels if l in LEVELS), None)
    edu = []
    for e in r.get("education") or []:
        school = (e.get("school") or {}).get("name")
        if school:
            edu.append({"school": school, "degree": ", ".join(e.get("degrees") or []) or None, "end_year": _year(e.get("end_date"))})
    past = []
    for x in r.get("experience") or []:
        name = (x.get("company") or {}).get("name")
        if name and not x.get("is_primary") and name not in past:
            past.append(name)
    company = r.get("job_company_name") or ""
    url = r.get("linkedin_url")
    return Prospect(
        first_name=first, last_name=last, company=Company(company, _domain(r.get("job_company_website"))),
        source=source, source_ref=f"pdl:{r.get('id', '')}", linkedin_url=url or None, headline=r.get("headline") or "",
        current_title=title, seniority=seniority, location=r.get("location_name") or "", education=edu,
        past_companies=past[:5], summary=r.get("summary") or r.get("job_summary") or "", fetched_at=iso(),
    )


def build_query(campaign: "Campaign") -> dict[str, Any]:
    """Elasticsearch query for the filters PDL can run. Keywords and seniority run locally."""
    t = campaign.targeting
    must: list[dict[str, Any]] = []
    must_not: list[dict[str, Any]] = []
    comps = t.get("companies", {})
    if comps.get("include"):
        should = []
        domains = [c["domain"] for c in comps["include"] if c.get("domain")]
        if domains:
            should.append({"terms": {"job_company_website": domains}})
        should.append({"terms": {"job_company_name": [normalize_company(c["name"]) for c in comps["include"]]}})
        must.append({"bool": {"should": should, "minimum_should_match": 1}})
    for c in comps.get("exclude", []):
        must_not.append({"term": {"job_company_name": normalize_company(c["name"])}})
    titles = t.get("titles", {})
    if titles.get("include"):
        must.append({"bool": {"should": [{"match_phrase": {"job_title": x.lower()}} for x in titles["include"]], "minimum_should_match": 1}})
    for x in titles.get("exclude", []):
        must_not.append({"match_phrase": {"job_title": x.lower()}})
    if t.get("locations"):
        must.append({"bool": {"should": [{"match_phrase": {"location_name": loc.split(",")[0].strip().lower()}} for loc in t["locations"]], "minimum_should_match": 1}})
    q: dict[str, Any] = {"bool": {"must": must}}
    if must_not:
        q["bool"]["must_not"] = must_not
    return q


@register("pdl")
class PdlSource(PeopleSource):
    cost = {"record": 1}

    def supported_filters(self) -> set[str]:
        return set() if self.options.get("pdl_sql") else {"companies", "titles", "locations"}

    def search(self, campaign: "Campaign", limit: int) -> Iterator[Prospect]:
        assert self.client
        size = min(100, limit)
        while size > 0 and not self.can_spend(size):
            size -= 1
        if size <= 0:
            return
        body: dict[str, Any] = {"size": size, "titlecase": True}
        if self.options.get("pdl_sql"):
            body["sql"] = self.options["pdl_sql"]
        else:
            body["query"] = build_query(campaign)
        cursor = self.get_cursor("search")
        if cursor:
            body["scroll_token"] = cursor
        try:
            r = self.client.request("POST", f"{API}/person/search", json=body)
        except DataError:
            if not cursor:
                raise
            self.set_cursor("search", None)  # scroll token expired: start over from the top
            body.pop("scroll_token")
            r = self.client.request("POST", f"{API}/person/search", json=body)
        if r.cls == "not_found" or not isinstance(r.body, dict):
            return
        data = r.body.get("data") or []
        self.charge("person/search", credits=len(data) * self.cost["record"], status=r.status)
        self.set_cursor("search", r.body.get("scroll_token") if data else None)
        for rec in data:
            p = record_to_prospect(rec, self.name)
            if p:
                yield p


@register("pdl_enrich")
class PdlEnricher(Enricher):
    manifest_name = "pdl"
    cost = {"match": 1}

    def enrich(self, p: Prospect) -> Prospect | None:
        assert self.client
        if not p.linkedin_url or not self.can_spend(self.cost["match"]):
            return None
        r = self.client.request("GET", f"{API}/person/enrich", params={"profile": p.linkedin_url, "min_likelihood": 6, "titlecase": "true"})
        if r.cls == "not_found" or not isinstance(r.body, dict) or not r.body.get("data"):
            return None
        self.charge("person/enrich", credits=self.cost["match"], status=r.status)
        out = record_to_prospect(r.body["data"], p.source)
        if out:
            out.linkedin_url = p.linkedin_url
            out.prospect_id = p.prospect_id
        return out
