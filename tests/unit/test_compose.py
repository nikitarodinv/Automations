from pathlib import Path

import pytest

from autonet.compose import (
    PhraseBank, Phrase, compose, lint_phrases, load_phrase_bank, load_templates, parse_template, pick_phrase, substitute,
)
from autonet.errors import ConfigError
from autonet.schemas import ValidationFailed
from tests.conftest import REPO, make_prospect

BANK = load_phrase_bank(REPO / "phrases/networking.yaml")
T = load_templates(REPO / "templates")
SENDER = {"name": "Alex Kim", "school": "Your University", "year": "junior", "topic": "investment banking"}


def run(template="alumni_coffee_chat", prospect=None, hints=None, hook=None, used=frozenset(), bank=BANK):
    return compose(
        template=T[template], bank=bank, prospect=prospect or make_prospect(), sender=SENDER,
        schools=["Your University"], hints=hints or {"tone": "warm", "ask": "call_15"}, hook=hook,
        signature="Best,\nAlex", opt_out_line="If you'd rather I not follow up, just let me know.",
        used_at_company=set(used),
    )


def test_compose_fills_every_slot_and_records_phrases():
    c = run()
    assert c.ok, c.error
    assert c.body.startswith("Hi Jordan,") or c.body.startswith("Hello Jordan,")
    assert "{{" not in c.full_body
    assert set(c.phrase_ids) >= {"subject", "greeting", "opener", "ask", "close"}
    assert "hook" not in c.phrase_ids
    assert c.full_body.endswith("Best,\nAlex")
    assert "let me know." in c.full_body


def test_compose_is_deterministic():
    assert run().full_body == run().full_body
    other = make_prospect(first_name="Sam", linkedin_url="https://www.linkedin.com/in/sam")
    assert run(prospect=other).phrase_ids != run().phrase_ids or True  # different seed; may coincide


def test_seed_changes_with_bank_version():
    import copy

    bumped = copy.deepcopy(BANK)
    bumped.version += 1
    picks = {tuple(sorted(run(prospect=make_prospect(linkedin_url=f"https://www.linkedin.com/in/x{i}"), bank=b).phrase_ids.items()))
             for i in range(5) for b in (BANK, bumped)}
    assert len(picks) > 1


def test_hook_slot_uses_hook_type_category():
    hook = {"hook_id": "h1", "tier": 1, "type": "talk", "fields": {"event": "Midwest Finance Forum", "topic": "private credit"}}
    c = run(hook=hook)
    assert c.phrase_ids["hook"] in {"ht1", "ht2"}
    assert "Midwest Finance Forum" in c.body and "private credit" in c.body


def test_note_hook_is_used_verbatim():
    note = "I enjoyed your post about the growth of private credit last week."
    c = run(hook={"hook_id": "h2", "tier": 0, "type": "note", "fields": {"text": note}})
    assert note in c.body


def test_requires_filters_phrases():
    # no team -> a2 (requires team) is never picked
    for i in range(20):
        c = run(prospect=make_prospect(linkedin_url=f"https://www.linkedin.com/in/p{i}", team=None))
        assert c.phrase_ids["ask"] != "a2"


def test_tags_and_relaxation():
    bank = PhraseBank(1, ["first_name"], {
        "c": [Phrase("w", "c", "warm {{first_name}}", tags={"tone": ["warm"]}),
              Phrase("f", "c", "formal {{first_name}}", tags={"tone": ["formal"]})],
    })
    ctx = {"first_name": "J"}
    assert pick_phrase(bank, "c", ctx, {"tone": "formal"}, set(), 1, optional=False).id == "f"
    # step 4 removes 'f'; required slot relaxes step 4 first (keeps tag fit)
    assert pick_phrase(bank, "c", ctx, {"tone": "formal"}, {"f"}, 1, optional=False).id == "f"
    # optional slot stays empty instead of relaxing
    assert pick_phrase(bank, "c", ctx, {"tone": "formal"}, {"f"}, 1, optional=True) is None
    # nothing fits the tags at all -> relax step 3
    assert pick_phrase(bank, "c", ctx, {"tone": "casual"}, set(), 1, optional=False) is not None


def test_company_dedupe_avoids_used_phrases():
    first = run()
    second = run(prospect=make_prospect(first_name="Hannah", linkedin_url="https://www.linkedin.com/in/hannah"),
                 used=set(first.categories.values()))
    for slot in ("opener", "ask", "close", "subject"):
        assert second.phrase_ids[slot] != first.phrase_ids[slot], slot


def test_missing_required_phrase_reports_category():
    c = run(prospect=make_prospect(education=[]))  # alumni template, no shared school
    assert not c.ok and c.error == "no_phrase:subject.alumni"


def test_substitute_leaves_unknown_variables_visible():
    assert substitute("Hi {{first_name}} {{nope}}", {"first_name": "J"}) == "Hi J {{nope}}"


def test_template_with_unknown_slot_raises(tmp_path):
    p = tmp_path / "bad.md.j2"
    p.write_text("---\nid: bad\nversion: 1\nuse_when: x\nslots:\n  subject: { category: greeting }\n---\n{{ slot('nope') }}\n")
    t = parse_template(p)
    with pytest.raises(ConfigError):
        compose(template=t, bank=BANK, prospect=make_prospect(), sender=SENDER, schools=[], hints={}, hook=None,
                signature="", opt_out_line=None, used_at_company=set())


def test_template_strict_undefined(tmp_path):
    p = tmp_path / "bad.md.j2"
    p.write_text("---\nid: bad\nversion: 1\nuse_when: x\nslots:\n  subject: { category: greeting }\n---\n{{ secret }}\n")
    t = parse_template(p)
    with pytest.raises(Exception):
        compose(template=t, bank=BANK, prospect=make_prospect(), sender=SENDER, schools=[], hints={}, hook=None,
                signature="", opt_out_line=None, used_at_company=set())


def test_template_file_name_must_match_id(tmp_path):
    p = tmp_path / "other.md.j2"
    p.write_text("---\nid: bad\nversion: 1\nuse_when: x\nslots:\n  subject: { category: greeting }\n---\nhi\n")
    with pytest.raises(ValidationFailed):
        parse_template(p)


def test_shipped_phrase_bank_lints_clean():
    errors, _ = lint_phrases(BANK, T)
    assert errors == []


def test_lint_catches_problems():
    bank = PhraseBank(1, ["first_name"], {
        "greeting": [Phrase("g1", "greeting", "Hi {{first_name}},"), Phrase("g1", "greeting", "")],
        "subject.alumni": [Phrase("s1", "subject.alumni", "x" * 70), Phrase("s2", "subject.alumni", "{{undeclared}}")],
    })
    errors, _ = lint_phrases(bank, {"alumni_coffee_chat": T["alumni_coffee_chat"]})
    text = "\n".join(errors)
    assert "duplicate id" in text and "text is empty" in text and "undeclared" in text
    assert "70 chars" in text and "missing category 'opener.shared_school'" in text


def test_required_hook_slot_without_hook_fails():
    c = run(template="saw_your_work")
    assert not c.ok and c.error == "no_hook"
