"""Stage 2 via Jev, TypeSafe's decision model (section 5). Jev answers typed questions with
probabilities; `selector.apply_jev_policy` turns them into a decision in code.

Routes (config `jev.provider`): typesafe | openrouter | cloudflare. All take the same state and
questions. On an HTTP error or timeout the client retries twice, then the rules fallback decides
(tagged `selector: rules`)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .compose import Template
from .errors import AuthError, ConfigError, DataError, TransientError
from .http import ApiClient, RateLimiter
from .models import Prospect
from .selector import Decision, RulesSelector, SelectionContext, apply_jev_policy, eligible_templates
from .util import short_hash, stable_json

ROUTES: dict[str, dict[str, str]] = {
    "typesafe": {"url": "https://api.typesafe.ai/v1/systemone", "key_env": "TYPESAFE_API_KEY", "model": "jev-latest"},
    "openrouter": {"url": "https://openrouter.ai/api/alpha/decisions", "key_env": "OPENROUTER_API_KEY", "model": "typesafe/jev-1.14"},
    "cloudflare": {
        "url": "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run",
        "key_env": "CLOUDFLARE_API_TOKEN", "model": "typesafe/jev", "account_env": "CLOUDFLARE_ACCOUNT_ID",
    },
}
POLICY_VERSION = "jev-policy-v1"
EXPECTED = {"good_fit": "noul", "template": "choice", "tone": "choice", "seniority_gap": "score"}


# ---------------------------------------------------------------------------- request building
def sender_line(sender: dict[str, Any]) -> str:
    return f"{sender.get('year', '')} at {sender.get('school', '')} interested in {sender.get('topic', '')}".strip()


def build_state(campaign: Any, sc: SelectionContext) -> dict[str, Any]:
    """Profile fields go in as quoted JSON data only."""
    p: Prospect = sc.prospect
    edu = [", ".join(str(x) for x in [e.get("school"), e.get("degree"), e.get("end_year")] if x) for e in p.education]
    state: dict[str, Any] = {
        "campaign": {"goal": campaign.sender.get("goal") or "15-minute call", "sender": sender_line(campaign.sender)},
        "prospect": {
            "current_title": p.current_title, "seniority": p.seniority, "company": p.company.name,
            "education": edu, "past_companies": p.past_companies, "headline": p.headline,
            "keywords_hit": (p.match or {}).get("keywords_hit", []),
        },
        "shared_context": {
            "same_school": sc.same_school, "same_past_employer": sc.same_past_employer,
            "prior_contact": bool(sc.prior_contact or p.prior_contact),
        },
    }
    if sc.hooks:
        state["public_detail"] = [{"type": h["type"], **h["fields"]} for h in sc.hooks if h["tier"] in (1, 2)][:1]
    return state


def build_questions(campaign: Any, eligible: list[Template]) -> dict[str, Any]:
    topic = campaign.sender.get("topic", "the target field")
    goal = campaign.sender.get("goal") or "a short call"
    return {
        "good_fit": {
            "type": "noul",
            "instructions": f"Is this person a useful contact for the campaign goal ({goal} about {topic})?",
            "criteria": {"true": "Works in the target field at a level that can speak to the goal",
                         "false": "Wrong field, wrong level, or unrelated role"},
        },
        "template": {
            "type": "choice",
            "instructions": "Which outreach email template best fits this person?",
            "criteria": {**{t.id: t.use_when for t in eligible}, "none": "None of these fits this person"},
        },
        "tone": {
            "type": "choice",
            "instructions": "Which tone suits a first email to this person?",
            "criteria": {"warm": "Peer or near-peer, shared background", "formal": "Senior, or no shared tie"},
        },
        "seniority_gap": {
            "type": "score",
            "instructions": "How senior is this person relative to the sender?",
            "criteria": ["Near-peer", "Some gap", "Very senior"],
        },
    }


def validate_answers(answers: Any, questions: dict[str, Any]) -> list[str]:
    """Fail closed: a refusal, missing key or unexpected type counts as not answered."""
    problems = []
    if not isinstance(answers, dict):
        return ["answers missing"]
    for name, typ in EXPECTED.items():
        a = answers.get(name)
        if not isinstance(a, dict) or a.get("type") != typ:
            problems.append(f"{name}: {'refused' if isinstance(a, dict) and a.get('type') == 'refusal' else 'not answered'}")
            continue
        if typ == "noul" and not isinstance(a.get("noul"), (int, float)):
            problems.append(f"{name}: no probability")
        if typ == "choice" and a.get("choice") not in questions[name]["criteria"]:
            problems.append(f"{name}: '{a.get('choice')}' was not offered")
        if typ == "score" and not isinstance(a.get("score"), (int, float)):
            problems.append(f"{name}: no score")
    return problems


# ---------------------------------------------------------------------------- client
@dataclass
class JevReply:
    answers: dict[str, Any]
    model: str
    usage: dict[str, Any] = field(default_factory=dict)


class JevClient:
    def __init__(self, route: str = "typesafe", model: str | None = None, key: str | None = None,
                 account_id: str | None = None, timeout: float = 10.0):
        if route not in ROUTES:
            raise ConfigError(f"jev.provider '{route}': use one of {', '.join(ROUTES)} or off")
        spec = ROUTES[route]
        self.route = route
        self.model = model or spec["model"]
        key = key or os.environ.get(spec["key_env"])
        if not key:
            raise ConfigError(f"jev.provider {route}: set {spec['key_env']} in the environment")
        url = spec["url"]
        if "{account_id}" in url:
            acct = account_id or os.environ.get(spec["account_env"])
            if not acct:
                raise ConfigError(f"jev.provider cloudflare: set {spec['account_env']}")
            url = url.format(account_id=acct)
        self.url = url
        self.http = ApiClient(
            service=f"jev ({route})", headers={"Authorization": f"Bearer {key}"},
            errors={"400": "data", "401": "auth", "403": "auth", "422": "data", "429": "transient", "5xx": "transient"},
            limiter=RateLimiter(per_second=5), timeout=timeout, backoff=(1, 2), secrets=[key],
        )

    def ask(self, state: Any, questions: dict[str, Any]) -> JevReply:
        if self.route == "cloudflare":
            body = {"model": self.model, "input": {"state": state, "questions": questions}}
        else:
            body = {"model": self.model, "state": state, "questions": questions}
        r = self.http.request("POST", self.url, json=body)
        data = r.body if isinstance(r.body, dict) else {}
        if "answers" not in data and isinstance(data.get("result"), dict):
            data = data["result"]   # Cloudflare's usual {"result": ..., "success": true} envelope
        return JevReply(answers=data.get("answers") or {}, model=str(data.get("model") or self.model), usage=data.get("usage") or {})


# ---------------------------------------------------------------------------- selector
class JevSelector:
    name = "jev"

    def __init__(self, client: JevClient, campaign: Any, min_confidence: float = 0.6, pinned: str | None = None):
        self.client = client
        self.campaign = campaign
        self.min_confidence = min_confidence
        self.pinned = pinned
        self.rules = RulesSelector()
        self.disabled: str | None = None   # set after an auth failure: rules for the rest of the run
        self.fallbacks = 0
        self.calls = 0

    def question_hash(self, questions: dict[str, Any]) -> str:
        return short_hash(stable_json(questions), POLICY_VERSION, self.client.model, length=12)

    def select(self, store: Any, campaign_id: str, sc: SelectionContext, allowed: list[Template], fallback: str,
               ledger: Any) -> Decision:
        elig = eligible_templates(allowed, sc)
        if not elig:
            d = self.rules.decide(sc, allowed, fallback)
            store.save_decision(campaign_id, sc.prospect.prospect_id, "no_eligible", "rules", d.as_record())
            return d
        questions = build_questions(self.campaign, elig)
        qhash = self.question_hash(questions)
        cached = store.cached_decision(sc.prospect.prospect_id, qhash, self.pinned)
        if cached and cached["selector"] == "jev":
            r = cached["reason"] or {}
            return Decision(r.get("route", "continue"), cached["template"], "jev", cached["confidence"], r,
                            cached["hints"], r.get("review_reason"))
        save = lambda d: store.save_decision(campaign_id, sc.prospect.prospect_id, qhash, "rules-fallback", d.as_record()) or d  # noqa: E731
        if self.disabled:
            return save(self._fallback(sc, allowed, fallback, self.disabled))
        ledger.require_llm_call()          # QuotaError stops Stage 2; prospects stay qualified
        try:
            reply = self.client.ask(build_state(self.campaign, sc), questions)
        except AuthError as e:
            self.disabled = f"Jev key rejected ({e}); rules fallback for the rest of this run"
            ledger.record("jev", self.client.route, llm_calls=1, http_status=401)
            return save(self._fallback(sc, allowed, fallback, self.disabled))
        except (TransientError, DataError) as e:
            ledger.record("jev", self.client.route, llm_calls=1)
            return save(self._fallback(sc, allowed, fallback, str(e)))
        self.calls += 1
        usage = reply.usage or {}
        ledger.record("jev", self.client.route, llm_calls=1, tokens=int(usage.get("input_tokens") or 0), http_status=200)
        problems = validate_answers(reply.answers, questions)
        if problems:
            return save(self._fallback(sc, allowed, fallback, "; ".join(problems)))
        d = apply_jev_policy(reply.answers, self.min_confidence)
        d.reason["model"] = reply.model
        store.save_decision(campaign_id, sc.prospect.prospect_id, qhash, reply.model, d.as_record())
        return d

    def _fallback(self, sc: SelectionContext, allowed: list[Template], fallback: str, why: str) -> Decision:
        self.fallbacks += 1
        d = self.rules.decide(sc, allowed, fallback)
        d.reason["jev_unavailable"] = why
        return d


# ---------------------------------------------------------------------------- eval
def eval_jev(selector: JevSelector, cases: list[dict[str, Any]], allowed: list[Template], schools: list[str],
             sender_past: list[str]) -> dict[str, Any]:
    """Agreement with your labels, and calibration: of picks at 0.8-0.9, ~80-90% should match."""
    from .ledger import Ledger, RunBudget
    from .store import Store

    store = Store(":memory:")
    store.start_run("eval", "eval", False)
    ledger = Ledger(store, "eval", RunBudget(0, 10_000, 0))
    rows = []
    for c in cases:
        p = Prospect.from_dict({"source": "eval", **c["prospect"]})
        sc = SelectionContext(p, schools, sender_past, bool(c.get("prior_contact")), c.get("hooks", []))
        d = selector.select(store, "eval", sc, allowed, allowed[0].id, ledger)
        rows.append({"name": p.full_name, "label": c["label"], "pick": d.template if d.route != "skip" else "none",
                     "confidence": d.confidence, "selector": d.selector, "route": d.route})
    jev_rows = [r for r in rows if r["selector"] == "jev"]
    agree = sum(r["pick"] == r["label"] for r in jev_rows)
    buckets: dict[str, list[bool]] = {}
    for r in jev_rows:
        if r["confidence"] is None:
            continue
        lo = min(9, int(r["confidence"] * 10))
        buckets.setdefault(f"{lo / 10:.1f}-{(lo + 1) / 10:.1f}", []).append(r["pick"] == r["label"])
    return {
        "cases": len(rows), "answered_by_jev": len(jev_rows),
        "agreement": (agree / len(jev_rows)) if jev_rows else None,
        "calibration": {k: (sum(v) / len(v), len(v)) for k, v in sorted(buckets.items())},
        "rows": rows,
    }


def load_cases(path: Path) -> list[dict[str, Any]]:
    out = []
    for i, line in enumerate(path.read_text().splitlines(), 1):
        if line.strip() and not line.lstrip().startswith("//"):
            c = json.loads(line)
            if "prospect" not in c or "label" not in c:
                raise ConfigError(f"{path.name}:{i}: each case needs 'prospect' and 'label'")
            out.append(c)
    return out


# ---------------------------------------------------------------------------- identity (Stage 4)
class JevIdentity:
    """One yes/no question per fetched page: is the person on this page the prospect?"""

    def __init__(self, client: JevClient):
        self.client = client

    def same_person(self, p: Prospect, url: str, page_excerpt: str) -> float:
        state = {
            "prospect": {"name": p.full_name, "company": p.company.name, "title": p.current_title,
                         "schools": p.schools, "location": p.location},
            "page": {"url": url, "excerpt": page_excerpt},
        }
        q = {"same_person": {
            "type": "noul",
            "instructions": "Is the person this page describes the same person as the prospect?",
            "criteria": {"true": "Same name and the same employer or school, with nothing contradicting it",
                         "false": "A different person who shares the name, or not enough to tell"},
        }}
        reply = self.client.ask(state, q)
        a = reply.answers.get("same_person") or {}
        if a.get("type") != "noul" or not isinstance(a.get("noul"), (int, float)):
            raise DataError("jev: identity question not answered")
        return float(a["noul"])
