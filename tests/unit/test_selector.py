from pathlib import Path

import pytest

from autonet.compose import load_templates
from autonet.selector import RulesSelector, SelectionContext, apply_jev_policy, eligible_templates
from tests.conftest import REPO, make_prospect

T = load_templates(REPO / "templates")
ALLOWED = [T["alumni_coffee_chat"], T["industry_question"], T["referral_ask"], T["saw_your_work"]]


def sc(**kw):
    hooks = kw.pop("hooks", [])
    prior = kw.pop("prior", False)
    return SelectionContext(make_prospect(**kw), ["Your University"], [], prior, hooks)


def test_eligibility_checks_requires_in_code():
    ids = [t.id for t in eligible_templates(ALLOWED, sc())]
    assert ids == ["alumni_coffee_chat", "industry_question"]
    ids = [t.id for t in eligible_templates(ALLOWED, sc(education=[], prior=True, hooks=[{"tier": 1, "type": "talk"}]))]
    assert ids == ["industry_question", "referral_ask", "saw_your_work"]


def test_rules_shared_school_gets_alumni():
    d = RulesSelector().decide(sc(), ALLOWED, "alumni_coffee_chat")
    assert (d.route, d.template, d.selector) == ("continue", "alumni_coffee_chat", "rules")
    assert d.hints == {"tone": "warm", "ask": "call_15"}


def test_rules_senior_gets_industry_question_and_question_ask():
    d = RulesSelector().decide(sc(seniority="executive", education=[]), ALLOWED, "alumni_coffee_chat")
    assert d.template == "industry_question"
    assert d.hints == {"tone": "formal", "ask": "question"}


def test_rules_fallback_skips_ineligible():
    # fallback alumni_coffee_chat needs a shared school this person lacks
    d = RulesSelector().decide(sc(education=[]), ALLOWED, "alumni_coffee_chat")
    assert d.template == "industry_question"


def test_rules_no_eligible_template_goes_to_review():
    d = RulesSelector().decide(sc(education=[]), [T["alumni_coffee_chat"]], "alumni_coffee_chat")
    assert d.route == "review" and d.review_reason == "no_eligible_template"


def answers(fit=0.91, choice="alumni_coffee_chat", conf=0.88, tone="warm", gap=0.4):
    return {
        "good_fit": {"type": "noul", "noul": fit},
        "template": {"type": "choice", "choice": choice, "confidence": conf,
                     "probabilities": {choice: conf, "industry_question": round(1 - conf, 2)}},
        "tone": {"type": "choice", "choice": tone, "confidence": 0.8},
        "seniority_gap": {"type": "score", "score": gap, "confidence": 0.8},
    }


@pytest.mark.parametrize(
    "kw, route, reason",
    [
        ({}, "continue", None),
        ({"fit": 0.29}, "skip", "jev_not_a_fit"),
        ({"fit": 0.3}, "review", "jev_fit_uncertain"),
        ({"fit": 0.6}, "continue", None),
        ({"choice": "none"}, "skip", "jev_no_template"),
        ({"conf": 0.59}, "review", "jev_low_confidence"),
    ],
)
def test_jev_policy_thresholds(kw, route, reason):
    d = apply_jev_policy(answers(**kw))
    assert (d.route, d.review_reason) == (route, reason)


def test_jev_policy_hints():
    d = apply_jev_policy(answers(tone="formal", gap=1.5))
    assert d.hints == {"tone": "formal", "ask": "question"}
    assert d.reason["top2"][0] == ("alumni_coffee_chat", 0.88)
