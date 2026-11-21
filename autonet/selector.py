"""Stage 2: template selection (section 5).

Milestone 1 ships the rules selector, which is also Jev's fallback when Jev is off or
unreachable. `apply_jev_policy` holds the code-side decision rules for Jev's typed answers,
so the milestone 2 client only has to make the HTTP call."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .compose import Template
from .matching import shared_employer, shared_school
from .models import Prospect

SENIOR = {"senior", "executive"}
HOOK_TYPES = {"note", "talk", "writing", "podcast", "deal", "promotion", "company_news"}


@dataclass
class SelectionContext:
    prospect: Prospect
    schools: list[str]
    sender_past: list[str]
    prior_contact: bool = False
    hooks: list[dict[str, Any]] = field(default_factory=list)

    @property
    def same_school(self) -> bool:
        return shared_school(self.prospect, self.schools) is not None

    @property
    def same_past_employer(self) -> bool:
        return shared_employer(self.prospect, self.sender_past) is not None


def requirement_met(req: str, sc: SelectionContext) -> bool:
    if req == "shared_school":
        return sc.same_school
    if req == "shared_employer":
        return sc.same_past_employer
    if req == "prior_contact":
        return sc.prior_contact or sc.prospect.prior_contact
    if req == "hook":  # a researched tier 1 or 2 hook
        return any(h["tier"] in (1, 2) for h in sc.hooks)
    if req.startswith("hook."):
        return any(h["type"] == req.split(".", 1)[1] for h in sc.hooks)
    return False


def known_requirement(req: str) -> bool:
    return req in {"shared_school", "shared_employer", "prior_contact", "hook"} or (
        req.startswith("hook.") and req.split(".", 1)[1] in HOOK_TYPES
    )


def eligible_templates(templates: list[Template], sc: SelectionContext) -> list[Template]:
    """Only templates whose `requires` this person meets are offered (checked in code, before any call)."""
    return [t for t in templates if all(requirement_met(r, sc) for r in t.requires)]


def default_hints(sc: SelectionContext) -> dict[str, str]:
    senior = sc.prospect.seniority in SENIOR
    tie = sc.same_school or sc.same_past_employer or sc.prior_contact or sc.prospect.prior_contact
    return {"tone": "warm" if tie and not senior else "formal", "ask": "question" if senior else "call_15"}


@dataclass
class Decision:
    route: str                     # continue | review | skip
    template: str | None
    selector: str                  # jev | rules
    confidence: float | None = None
    reason: dict[str, Any] = field(default_factory=dict)
    hints: dict[str, str] = field(default_factory=dict)
    review_reason: str | None = None

    def as_record(self) -> dict[str, Any]:
        return {
            "template": self.template, "confidence": self.confidence, "selector": self.selector,
            "reason": {**self.reason, "route": self.route, "review_reason": self.review_reason},
            "hints": self.hints,
        }


class RulesSelector:
    """Section 5 fallback: shared school and alumni_coffee_chat allowed -> that template;
    senior or executive -> industry_question; otherwise the campaign's fallback.
    A pick whose `requires` this person doesn't meet falls through to the next eligible one."""

    name = "rules"
    version = "rules-v1"

    def decide(self, sc: SelectionContext, allowed: list[Template], fallback: str) -> Decision:
        elig = {t.id: t for t in eligible_templates(allowed, sc)}
        hints = default_hints(sc)
        order: list[tuple[str, str]] = []
        if sc.same_school:
            order.append(("alumni_coffee_chat", "shared school"))
        if sc.prospect.seniority in SENIOR:
            order.append(("industry_question", "senior or executive"))
        order.append((fallback, "campaign fallback"))
        order += [(t.id, "first eligible template without requirements") for t in allowed if not t.requires]
        for tid, why in order:
            if tid in elig:
                return Decision("continue", tid, self.name, reason={"rule": why}, hints=hints)
        return Decision(
            "review", None, self.name, reason={"rule": "no eligible template"}, hints=hints,
            review_reason="no_eligible_template",
        )


def apply_jev_policy(answers: dict[str, Any], min_confidence: float = 0.6) -> Decision:
    """Turn Jev's typed answers into a decision. The policy lives here, not in Jev."""
    fit = answers["good_fit"]["noul"]
    tmpl = answers["template"]
    probs = tmpl.get("probabilities", {})
    top2 = sorted(probs.items(), key=lambda kv: -kv[1])[:2]
    reason = {"answers": answers, "top2": top2}
    tone = answers.get("tone", {}).get("choice", "formal")
    gap = answers.get("seniority_gap", {}).get("score", 0.0)
    hints = {"tone": tone, "ask": "question" if gap >= 1.5 else "call_15"}
    if fit < 0.3:
        return Decision("skip", None, "jev", fit, reason, hints, "jev_not_a_fit")
    if tmpl["choice"] == "none":
        return Decision("skip", None, "jev", tmpl.get("confidence"), reason, hints, "jev_no_template")
    if fit < 0.6:
        return Decision("review", tmpl["choice"], "jev", fit, reason, hints, "jev_fit_uncertain")
    if tmpl.get("confidence", 0) < min_confidence:
        return Decision("review", tmpl["choice"], "jev", tmpl.get("confidence"), reason, hints, "jev_low_confidence")
    return Decision("continue", tmpl["choice"], "jev", tmpl.get("confidence"), reason, hints)
