from dataclasses import replace

from autonet.compose import Composed, load_templates
from autonet.draft import lint_draft
from tests.conftest import REPO, make_prospect

T = load_templates(REPO / "templates")["alumni_coffee_chat"]
P = make_prospect()


def draft(body="Hi Jordan,\n\nI saw you're at Evercore.\n\nWould you have 15 minutes?", subject="Question about Evercore", openers=None):
    return Composed(True, T.id, 1, 1, subject=subject, body=body, full_body=body + "\n\nBest,\nAlex",
                    phrase_ids={}, categories=openers or {"opener.shared_school": "os1"})


def lint(c=None, email="jordan.lee@evercore.com", others=(), recent=(), personal=("Evercore",)):
    return lint_draft(c or draft(), P, T, email, list(others), list(recent), list(personal))


def test_clean_draft_passes():
    r = lint()
    assert r.ok and r.warnings == []


def test_blocks():
    assert "placeholder left over" in lint(draft(body="Hi Jordan,\n\n{{company}}")).blocks
    assert "no accepted email address" in lint(email=None).blocks
    assert any("differs from first_name" in b for b in lint(draft(body="Hi Jordon,\n\nhello")).blocks)
    assert "greeting missing" in lint(draft(body="Jordan -- quick question")).blocks
    assert any("another prospect" in b for b in lint(others=[("Priya Shah", "Goldman Sachs")], c=draft(body="Hi Jordan,\n\nPriya Shah said hi")).blocks)
    assert any("another prospect" in b for b in lint(others=[("Priya Shah", "Goldman Sachs")], c=draft(body="Hi Jordan,\n\nGoldman Sachs is great")).blocks)
    assert any("words" in b for b in lint(draft(body="Hi Jordan,\n\n" + "word " * 151)).blocks)
    assert any("subject is" in b for b in lint(draft(subject="x" * 61)).blocks)


def test_own_company_and_past_employers_are_not_contamination():
    p_others = [("Hannah Kim", "Evercore"), ("Someone", "Houlihan Lokey")]
    p = replace(P, past_companies=["Houlihan Lokey"])
    c = draft(body="Hi Jordan,\n\nEvercore and Houlihan Lokey")
    assert lint_draft(c, p, T, "a@b.com", p_others, [], ["Evercore"]).ok


def test_warnings():
    assert "nothing personal beyond the first name" in lint(draft(body="Hi Jordan,\n\nHello there."), personal=["Evercore"]).warnings
    assert "more than one link" in lint(draft(body="Hi Jordan,\n\nEvercore https://a.com https://b.com")).warnings
    assert "more than one exclamation mark" in lint(draft(body="Hi Jordan,\n\nEvercore! Wow!")).warnings
    assert any("same opener" in w for w in lint(recent=["os1"]).warnings)
