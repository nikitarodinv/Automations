"""Hunter adapters (section 6): Email Finder + Verifier, Domain Search as a people source,
and the free /v2/account balance. Docs: https://hunter.io/api-documentation/v2"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterator

from .. import http
from ..errors import DataError, TransientError
from ..http import dig
from ..models import Company, EmailResult, Prospect, linkedin_handle
from ..util import iso
from .base import EmailFinder, PeopleSource
from .registry import register

if TYPE_CHECKING:
    from ..campaign import Campaign

API = "https://api.hunter.io/v2"
TEST_KEY = "test-api-key"   # validates parameters, returns the same dummy reply, costs nothing
VERIFY_COST = 0.5
POLL_WAIT, POLL_TRIES = 5, 3

# Verifier statuses -> shared statuses. Webmail isn't a work address; disposable never is.
VERIFIER_STATUS = {"valid": "valid", "invalid": "invalid", "accept_all": "accept_all",
                   "webmail": "unknown", "disposable": "invalid", "unknown": "unknown"}
SENIORITY = {"entry": "junior", "junior": "junior", "mid": "senior", "senior": "senior", "executive": "executive"}


def _credits(headers: Any, default: float) -> float:
    v = headers.get("X-Credits-Used") if headers is not None else None
    try:
        return float(v) if v is not None else default
    except ValueError:
        return default


def hunter_balance(client: Any, manifest: Any) -> tuple[float, str | None] | None:
    spec = manifest.free_plan.get("balance") if manifest else None
    if not spec:
        return None
    r = client.request(spec.get("method", "GET"), spec["url"])
    left = dig(r.body, spec["field"])
    if left is None and spec.get("fallback_field"):
        left = dig(r.body, spec["fallback_field"])
    reset = dig(r.body, spec.get("reset_field")) if spec.get("reset_field") else None
    return (float(left), str(reset) if reset else None) if left is not None else None


@register("hunter")
class HunterFinder(EmailFinder):
    cost = {"found": 1, "miss": 0, "verify": VERIFY_COST}

    def balance(self) -> tuple[float, str | None] | None:
        return hunter_balance(self.client, self.manifest)

    def find(self, p: Prospect) -> EmailResult:
        assert self.client
        handle = linkedin_handle(p.linkedin_url)
        params: dict[str, Any] = {"first_name": p.first_name or None, "last_name": p.last_name or None}
        if p.company.domain:
            params["domain"] = p.company.domain
        elif p.company.name:
            params["company"] = p.company.name
        if handle and not (p.first_name and p.last_name and (p.company.domain or p.company.name)):
            params["linkedin_handle"] = handle
        if not (params.get("domain") or params.get("company") or params.get("linkedin_handle")):
            raise DataError("hunter: no company domain, company name or LinkedIn handle to search with")
        r = self.client.request("GET", f"{API}/email-finder", params=params)
        if r.cls == "claimed":
            return EmailResult(None, None, "claimed", self.name)
        data = (r.body or {}).get("data") if isinstance(r.body, dict) else None
        if r.cls == "not_found" or not data or not data.get("email"):
            return EmailResult(None, None, "not_found", self.name, credits_used=0)
        status = (data.get("verification") or {}).get("status") or ("accept_all" if data.get("accept_all") else "unknown")
        result = EmailResult(
            email=data["email"], score=data.get("score"), status=status, provider=self.name,
            sources=[{"uri": s.get("uri"), "domain": s.get("domain"), "last_seen_on": s.get("last_seen_on")}
                     for s in data.get("sources") or []][:20],
            credits_used=_credits(r.headers, self.cost["found"]),
            person={k: v for k, v in {
                "first_name": data.get("first_name"), "last_name": data.get("last_name"),
                "company": data.get("company"), "domain": data.get("domain"), "position": data.get("position"),
                "linkedin_url": data.get("linkedin_url"),
            }.items() if v},
        )
        if status == "unknown" and self.can_spend(VERIFY_COST):
            self._verify_into(result)
        return result

    def _verify_into(self, result: EmailResult) -> None:
        """Step 3: verify only when the finder couldn't tell. 202 = still running: poll; 222 = odd
        mail server answer: keep the address and retry the check on the next run."""
        assert self.client and result.email
        for attempt in range(POLL_TRIES + 1):
            try:
                r = self.client.request("GET", f"{API}/email-verifier", params={"email": result.email})
            except TransientError:
                result.raw["retry_verify"] = True   # 403 rate limit or 5xx, even after backoff
                return
            if r.status == 222:
                result.raw["retry_verify"] = True
                return
            if r.status == 202 and attempt < POLL_TRIES:
                http.SLEEP(POLL_WAIT)
                continue
            if r.status == 202:
                result.raw["retry_verify"] = True
                return
            if r.cls == "claimed":
                result.status, result.email = "claimed", None
                return
            data = (r.body or {}).get("data") or {}
            result.status = VERIFIER_STATUS.get(str(data.get("status")), "unknown")
            if data.get("score") is not None:
                result.score = data["score"]
            result.credits_used += _credits(r.headers, VERIFY_COST)
            result.verified_with = "hunter_verifier"
            return

    def reverify(self, email: str, score: int | None) -> EmailResult:
        """Used on the run after a 222/202: check the stored address again, without a new find."""
        res = EmailResult(email, score, "unknown", self.name)
        if self.can_spend(VERIFY_COST):
            self._verify_into(res)
        return res


@register("hunter_domain")
class HunterDomainSource(PeopleSource):
    """Domain Search: people Hunter already knows at each campaign company, with an address attached."""

    manifest_name = "hunter"
    cost = {"search": 1}

    def balance(self) -> tuple[float, str | None] | None:
        return hunter_balance(self.client, self.manifest)

    def supported_filters(self) -> set[str]:
        return {"companies", "seniority"}

    def search(self, campaign: "Campaign", limit: int) -> Iterator[Prospect]:
        assert self.client
        targeting = campaign.targeting
        companies = [c for c in targeting.get("companies", {}).get("include", []) if c.get("domain")]
        seniority = sorted({SENIORITY[s] for s in targeting.get("seniority", []) if s in SENIORITY})
        given = 0
        for c in companies:
            if given >= limit:
                return
            key = c["domain"]
            cursor = self.get_cursor(key)
            if cursor == "done":
                continue
            offset = int(cursor or 0)
            if not self.can_spend(self.cost["search"]):
                return
            try:
                r = self.client.request("GET", f"{API}/domain-search", params={
                    "domain": c["domain"], "limit": min(10, max(1, limit - given)), "offset": offset,
                    "type": "personal", "seniority": ",".join(seniority) or None,
                })
            except DataError:  # e.g. pagination_error: the free plan stops at 10 results per domain
                self.set_cursor(key, "done")
                continue
            emails = ((r.body or {}).get("data") or {}).get("emails") or []
            total = ((r.body or {}).get("meta") or {}).get("results") or 0
            self.charge("domain-search", credits=_credits(r.headers, self.cost["search"]) if emails else 0, status=r.status)
            self.set_cursor(key, "done" if not emails or offset + len(emails) >= total else str(offset + len(emails)))
            org = ((r.body or {}).get("data") or {}).get("organization") or c["name"]
            for e in emails:
                if not (e.get("first_name") and e.get("last_name")):
                    continue
                given += 1
                yield Prospect(
                    first_name=e["first_name"], last_name=e["last_name"], company=Company(org, c["domain"]),
                    source=self.name, source_ref=f"hunter_domain:{c['domain']}:{e.get('value')}",
                    linkedin_url=e.get("linkedin") or None, current_title=e.get("position") or "",
                    headline=e.get("position_raw") or e.get("position") or "",
                    known_email=e.get("value"), known_email_score=e.get("confidence"),
                    known_email_status=(e.get("verification") or {}).get("status") or "unknown",
                    known_email_provider="hunter", fetched_at=iso(),
                )
