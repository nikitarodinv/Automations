"""Golden drafts: 10 fixture prospects x each template, with and without a hook.

A phrase-bank or template edit shows up as a readable diff in tests/golden/*.txt.
Regenerate after an intended change with:  UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from autonet.compose import compose, load_phrase_bank, load_templates
from autonet.selector import SelectionContext, default_hints
from tests.conftest import REPO, fixture_prospects

GOLDEN = Path(__file__).parent / "golden"
SENDER = {"name": "Your Name", "school": "Your University", "year": "junior", "topic": "investment banking"}
SCHOOLS = ["Your University"]
HOOK = {"hook_id": "h_golden", "tier": 1, "type": "talk", "fields": {"event": "Midwest Finance Forum", "topic": "private credit"}}
TEMPLATES = load_templates(REPO / "templates")
BANK = load_phrase_bank(REPO / "phrases/networking.yaml")


def render(template_id: str) -> str:
    out = [f"# {template_id} · template v{TEMPLATES[template_id].version} · phrase bank v{BANK.version}", ""]
    for p in fixture_prospects()[:10]:
        hints = default_hints(SelectionContext(p, SCHOOLS, [], p.prior_contact, []))
        for hook in (None, HOOK):
            c = compose(
                template=TEMPLATES[template_id], bank=BANK, prospect=p, sender=SENDER, schools=SCHOOLS, hints=hints,
                hook=hook, signature="Best,\nYour Name", opt_out_line="If you'd rather I not follow up, just let me know.",
                used_at_company=set(),
            )
            out.append(f"=== {p.full_name} · {p.company.name} · {'hook: talk' if hook else 'no hook'} · hints {hints}")
            if not c.ok:
                out += [f"(no draft: {c.error})", ""]
                continue
            out += [f"phrases: {', '.join(f'{k}={v}' for k, v in c.phrase_ids.items())}", f"Subject: {c.subject}", "", c.full_body, ""]
    return "\n".join(out)


@pytest.mark.parametrize("template_id", sorted(TEMPLATES))
def test_golden(template_id):
    path = GOLDEN / f"{template_id}.txt"
    actual = render(template_id)
    if os.environ.get("UPDATE_GOLDEN") or not path.exists():
        path.parent.mkdir(exist_ok=True)
        path.write_text(actual)
    assert actual == path.read_text(), f"{path.name} changed; review the diff, then UPDATE_GOLDEN=1 to accept"
