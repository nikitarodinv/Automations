"""Shared contract tests every provider must pass (`autonet provider test <name>`), run against
recorded replies in tests/fixtures/providers/<name>/<case>.json:

    {"role": "finder", "kind": "found" | "not_found" | "error",
     "input": {prospect fields},
     "replies": [{"status": 200, "headers": {...}, "json": {...}}, ...],   # in call order
     "expect": {"email": "...", "status": "valid", "credits": 1, "error": "quota"}}
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .. import http
from ..errors import AuthError, AutonetError, DataError, QuotaError, TransientError
from ..models import Company, Prospect

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "providers"
STATUSES = {"valid", "accept_all", "unknown", "invalid", "not_found", "claimed"}
ERROR_CLASSES = {AuthError: "auth", QuotaError: "quota", TransientError: "transient", DataError: "data"}
TEST_KEY = "contract-test-key-123"


@dataclass
class Check:
    case: str
    name: str
    ok: bool
    detail: str = ""


def load_cases(provider: str, folder: Path | None = None) -> dict[str, dict[str, Any]]:
    d = (folder or FIXTURES) / provider
    return {p.stem: json.loads(p.read_text()) for p in sorted(d.glob("*.json"))} if d.is_dir() else {}


def _prospect(inp: dict[str, Any]) -> Prospect:
    comp = inp.get("company") or {}
    return Prospect(
        first_name=inp.get("first_name", ""), last_name=inp.get("last_name", ""),
        company=Company(comp.get("name", ""), comp.get("domain")), source="contract",
        linkedin_url=inp.get("linkedin_url"),
    )


def _replay(replies: list[dict[str, Any]], seen: list[httpx.Request]) -> httpx.MockTransport:
    queue = list(replies)

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        rep = queue.pop(0) if len(queue) > 1 else queue[0]
        return httpx.Response(rep.get("status", 200), json=rep.get("json"), headers=rep.get("headers") or {})

    return httpx.MockTransport(handler)


def run_case(provider: str, case_name: str, case: dict[str, Any]) -> list[Check]:
    from ..campaign import load_campaign
    from .base import EmailFinder, PeopleSource
    from .registry import build

    checks: list[Check] = []
    seen: list[httpx.Request] = []
    old_t, old_s = http.TRANSPORT, http.SLEEP
    http.TRANSPORT, http.SLEEP = _replay(case["replies"], seen), (lambda s: None)
    charged: list[float] = []
    try:
        role = EmailFinder if case.get("role", "finder") == "finder" else PeopleSource
        inst = build(provider, role, key=TEST_KEY)
        inst.charge = lambda endpoint, credits=0, status=None: charged.append(credits)
        expect = case.get("expect", {})
        try:
            if role is EmailFinder:
                r = inst.find(_prospect(case.get("input", {})))
                got = {"email": r.email, "status": r.status, "score": r.score, "credits": r.credits_used}
            else:
                from ..config import find_root

                camp = load_campaign(find_root() / "campaigns" / "ib-alumni-fall26.yaml")
                ps = list(inst.search(camp, int(case.get("limit", 10))))
                got = {"count": len(ps), "names": [p.full_name for p in ps], "credits": sum(charged)}
                checks.append(Check(case_name, "records have names", all(p.first_name and p.last_name for p in ps)))
            err = None
        except AutonetError as e:
            got, err = {}, next((v for k, v in ERROR_CLASSES.items() if isinstance(e, k)), e.kind)
        if case["kind"] == "error":
            checks.append(Check(case_name, "error class", err == expect.get("error"), f"got {err}, want {expect.get('error')}"))
        else:
            checks.append(Check(case_name, "no error", err is None, str(err or "")))
            if role is EmailFinder and err is None:
                checks.append(Check(case_name, "status is a shared status", got["status"] in STATUSES, str(got["status"])))
                if got["score"] is not None:
                    checks.append(Check(case_name, "score within 0-100", 0 <= got["score"] <= 100, str(got["score"])))
                if case["kind"] == "not_found":
                    checks.append(Check(case_name, "miss maps to not_found", got["status"] == "not_found", got["status"]))
            for k, v in expect.items():
                if k != "error":
                    checks.append(Check(case_name, f"field {k}", got.get(k) == v, f"got {got.get(k)!r}, want {v!r}"))
        for req in seen:
            checks.append(Check(case_name, "key not in URL", TEST_KEY not in str(req.url), str(req.url)))
        checks.append(Check(case_name, "key sent", any(TEST_KEY in str(req.headers) or TEST_KEY in str(req.url) for req in seen) or not seen))
    finally:
        http.TRANSPORT, http.SLEEP = old_t, old_s
    return checks


def run_contract(provider: str, folder: Path | None = None) -> list[Check]:
    from .registry import manifests

    m = manifests().get(provider)
    checks = [Check("manifest", "manifest exists", m is not None)]
    if m:
        checks.append(Check("manifest", "rate limit declared", bool(m.rate_limit), str(m.rate_limit)))
        checks.append(Check("manifest", "free plan declared", "monthly" in (m.free_plan or {}), str(m.free_plan)))
        checks.append(Check("manifest", "terms checked", bool(getattr(m, "terms_checked", True))))
    cases = load_cases(provider, folder)
    kinds = {c["kind"] for c in cases.values()}
    for need in ("found", "not_found", "error"):
        if any(c.get("role", "finder") == "finder" for c in cases.values()):
            checks.append(Check("fixtures", f"has a '{need}' case", need in kinds))
    for name, case in cases.items():
        checks += run_case(provider, name, case)
    return checks
