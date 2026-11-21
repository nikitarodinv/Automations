"""Manifest-only finders: a REST API described entirely in providers/<name>.yaml."""

from __future__ import annotations

import re
from typing import Any

from ..http import Reply, default_classify, dig, _error_id
from ..models import EmailResult, Prospect, linkedin_handle
from .base import EmailFinder

_VAR = re.compile(r"\{\{\s*([a-z_.]+)\s*\}\}")


def prospect_vars(p: Prospect) -> dict[str, Any]:
    return {
        "first_name": p.first_name, "last_name": p.last_name, "full_name": p.full_name,
        "company.name": p.company.name, "company.domain": p.company.domain,
        "linkedin_url": p.linkedin_url, "linkedin_handle": linkedin_handle(p.linkedin_url),
    }


def render(value: Any, vars: dict[str, Any]) -> Any:
    """Fill {{vars}} in a request template. A string that is only an empty variable becomes None."""
    if isinstance(value, dict):
        out = {k: render(v, vars) for k, v in value.items()}
        return {k: v for k, v in out.items() if v not in (None, "", {})}
    if isinstance(value, list):
        return [render(v, vars) for v in value]
    if isinstance(value, str):
        m = _VAR.fullmatch(value.strip())
        if m:
            return vars.get(m.group(1)) or None
        return _VAR.sub(lambda mm: str(vars.get(mm.group(1)) or ""), value)
    return value


def make_classifier(errors: dict[str, str], error_codes: dict[str, str]):
    def classify(status: int, body: Any, headers: Any) -> tuple[str, str | None]:
        err = _error_id(body)
        if err and err in error_codes:
            return error_codes[err], err
        return default_classify(status, errors), err

    return classify


def map_status(spec: dict[str, Any], raw_status: Any) -> str:
    smap = spec.get("status_map") or {}
    if raw_status is None:
        return spec.get("status_default", "unknown")
    return smap.get(str(raw_status), smap.get(str(raw_status).lower(), spec.get("status_default", "unknown")))


def to_result(spec: dict[str, Any], reply: Reply, provider: str) -> EmailResult:
    """Map a reply to the shared format (pool rule 6): email, score 0-100, status."""
    if reply.cls == "not_found":
        return EmailResult(None, None, "not_found", provider, credits_used=spec.get("cost", {}).get("miss", 0))
    if reply.cls == "claimed":
        return EmailResult(None, None, "claimed", provider)
    resp = spec["response"]
    body = reply.body
    found_when = resp.get("found_when")
    email = dig(body, resp["email"])
    if not email or (found_when and not dig(body, found_when)) or "*" in str(email):
        return EmailResult(None, None, "not_found", provider, credits_used=spec.get("cost", {}).get("miss", 0))
    status = map_status(spec, dig(body, resp.get("status")))
    raw_score = dig(body, resp.get("score"))
    if raw_score is not None:
        score = round(float(raw_score) * 100 / float(spec.get("score_scale", 100)))
    else:
        score = (spec.get("status_scores") or {}).get(status)
    person = {k: dig(body, path) for k, path in (resp.get("person") or {}).items()}
    return EmailResult(
        email=str(email), score=None if score is None else max(0, min(100, int(score))), status=status,
        provider=provider, credits_used=spec.get("cost", {}).get("found", 1),
        person={k: v for k, v in person.items() if v},
    )


class GenericFinder(EmailFinder):
    """Built for any manifest with a `finder:` block and no registered class."""

    def find(self, p: Prospect) -> EmailResult:
        assert self.manifest and self.manifest.finder and self.client
        spec = self.manifest.finder
        req = spec["request"]
        vars = prospect_vars(p)
        self.client.classify = make_classifier(self.manifest.errors, self.manifest.error_codes)
        reply = self.client.request(
            req["method"], req["url"], params=render(req.get("params") or {}, vars),
            json=render(req["json"], vars) if req.get("json") else None,
        )
        return to_result(spec, reply, self.name)
