"""Apollo: People API Search (0 credits, last names hidden) + People Enrichment (1 credit per match,
full record and work email). Docs: https://docs.apollo.io/reference/people-api-search"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterator

from ..matching import term_in
from ..models import Company, EmailResult, Prospect
from ..util import iso
from .base import EmailFinder, Enricher, PeopleSource
from .registry import register

if TYPE_CHECKING:
    from ..campaign import Campaign

API = "https://api.apollo.io/api/v1"
SENIORITY = {
    "entry": ["entry", "intern"], "junior": ["entry", "senior"], "mid": ["manager", "senior"],
    "senior": ["director", "head", "vp"], "executive": ["c_suite", "partner", "owner", "founder", "vp"],
}
# Apollo gives no score and documents only "verified"; anything else goes to review.
VERIFIED_SCORE = 85


def _status(person: dict[str, Any]) -> tuple[str, int | None]:
    if str(person.get("email_status") or "").lower() == "verified":
        return "valid", VERIFIED_SCORE
    return "unknown", None


def person_to_prospect(person: dict[str, Any], source: str) -> Prospect | None:
    first, last = person.get("first_name") or "", person.get("last_name") or ""
    if not first or not last:
        return None
    org = person.get("organization") or {}
    domain = org.get("primary_domain") or None
    loc = ", ".join(x for x in [person.get("city"), person.get("state"), person.get("country")] if x)
    past = [e.get("organization_name") for e in person.get("employment_history") or []
            if e.get("organization_name") and not e.get("current")]
    email = person.get("email") if person.get("email") and "@" in str(person.get("email")) else None
    status, score = _status(person) if email else (None, None)
    return Prospect(
        first_name=first, last_name=last, company=Company(org.get("name") or "", domain), source=source,
        source_ref=f"apollo:{person.get('id', '')}", linkedin_url=person.get("linkedin_url") or None,
        headline=person.get("headline") or "", current_title=person.get("title") or "", location=loc,
        past_companies=list(dict.fromkeys(past))[:5], known_email=email, known_email_score=score,
        known_email_status=status, known_email_provider="apollo" if email else None, fetched_at=iso(),
    )


def _match(client: Any, params: dict[str, Any]) -> dict[str, Any] | None:
    r = client.request("POST", f"{API}/people/match", params={**params, "reveal_personal_emails": "false"})
    person = (r.body or {}).get("person") if isinstance(r.body, dict) else None
    if r.cls == "not_found" or not person or person.get("match_confidence") == "none":
        return None
    return person


@register("apollo")
class ApolloSource(PeopleSource):
    cost = {"search": 0, "enrich": 1}

    def supported_filters(self) -> set[str]:
        return {"companies", "titles", "locations", "seniority"}

    def _prefilter(self, campaign: "Campaign", title: str) -> bool:
        """Check what the free search shows before paying to enrich."""
        t = campaign.targeting
        inc, exc = t.get("titles", {}).get("include", []), t.get("titles", {}).get("exclude", [])
        kw_exc = t.get("keywords", {}).get("exclude", [])
        if inc and not any(term_in(x, title) for x in inc):
            return False
        return not any(term_in(x, title) for x in exc + kw_exc)

    def search(self, campaign: "Campaign", limit: int) -> Iterator[Prospect]:
        assert self.client
        t = campaign.targeting
        page = int(self.get_cursor("page") or 1)
        body = {
            "person_titles": t.get("titles", {}).get("include") or None,
            "person_seniorities": sorted({s for lvl in t.get("seniority", []) for s in SENIORITY.get(lvl, [])}) or None,
            "person_locations": t.get("locations") or None,
            "q_organization_domains_list": [c["domain"] for c in t.get("companies", {}).get("include", []) if c.get("domain")] or None,
            "page": page, "per_page": min(100, max(10, limit * 2)),
        }
        r = self.client.request("POST", f"{API}/mixed_people/api_search", json={k: v for k, v in body.items() if v})
        self.charge("mixed_people/api_search", credits=0, status=r.status)
        people = (r.body or {}).get("people") or [] if isinstance(r.body, dict) else []
        self.set_cursor("page", str(page + 1) if people else "1")
        given = 0
        for hit in people:
            if given >= limit or not self.can_spend(self.cost["enrich"]):
                return
            if not hit.get("id") or not self._prefilter(campaign, hit.get("title") or ""):
                continue
            person = _match(self.client, {"id": hit["id"]})
            if person is None:
                continue
            self.charge("people/match", credits=self.cost["enrich"], status=200)
            p = person_to_prospect(person, self.name)
            if p:
                given += 1
                yield p


@register("apollo_enrich")
class ApolloEnricher(Enricher):
    manifest_name = "apollo"
    cost = {"match": 1}

    def enrich(self, p: Prospect) -> Prospect | None:
        assert self.client
        if not p.linkedin_url or not self.can_spend(self.cost["match"]):
            return None
        person = _match(self.client, {"linkedin_url": p.linkedin_url})
        if person is None:
            return None
        self.charge("people/match", credits=self.cost["match"], status=200)
        out = person_to_prospect(person, p.source)
        if out:
            out.linkedin_url, out.prospect_id = p.linkedin_url, p.prospect_id
        return out


@register("apollo_finder")
class ApolloFinder(EmailFinder):
    manifest_name = "apollo"
    cost = {"found": 1, "miss": 0}

    def find(self, p: Prospect) -> EmailResult:
        assert self.client
        params = {"first_name": p.first_name, "last_name": p.last_name, "domain": p.company.domain,
                  "organization_name": None if p.company.domain else p.company.name, "linkedin_url": p.linkedin_url}
        person = _match(self.client, params)
        if person is None:
            return EmailResult(None, None, "not_found", self.name)
        email = person.get("email")
        # Apollo charges for a match with demographics even when there's no email.
        if not email or "@" not in str(email):
            return EmailResult(None, None, "not_found", self.name, credits_used=self.cost["found"])
        status, score = _status(person)
        return EmailResult(email, score, status, self.name, credits_used=self.cost["found"])
