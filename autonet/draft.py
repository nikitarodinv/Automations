"""Stage 6 checks (section 9). Polish (an optional model call) arrives in milestone 3."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .compose import Composed, Template
from .matching import term_in
from .models import Prospect
from .util import word_count

GREETING_RE = re.compile(r"^(?:hi|hello|dear|hey|good (?:morning|afternoon))\s+(.+?)\s*[,!:]?\s*$", re.I)
LINK_RE = re.compile(r"https?://|www\.", re.I)


@dataclass
class LintResult:
    blocks: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blocks

    def as_dict(self) -> dict[str, list[str]]:
        return {"blocks": self.blocks, "warnings": self.warnings}


def lint_draft(
    c: Composed,
    p: Prospect,
    template: Template,
    to_email: str | None,
    others: list[tuple[str, str]],
    recent_openers: list[str],
    personal_values: list[str],
) -> LintResult:
    """`others` holds (full_name, company_name) for every other prospect in the campaign."""
    r = LintResult()
    text = f"{c.subject}\n{c.full_body}"

    if "{{" in text or "}}" in text:
        r.blocks.append("placeholder left over")
    if not to_email:
        r.blocks.append("no accepted email address")

    first_line = next((ln for ln in c.body.splitlines() if ln.strip()), "")
    m = GREETING_RE.match(first_line)
    if not m:
        r.blocks.append("greeting missing")
    elif m.group(1).strip() != p.first_name.strip():
        r.blocks.append(f"greeting name '{m.group(1).strip()}' differs from first_name '{p.first_name}'")

    own = {p.company.name.lower(), *(x.lower() for x in p.past_companies)}
    leaked = set()
    for name, company in others:
        if name and name.lower() != p.full_name.lower() and term_in(name, text):
            leaked.add(name)
        if company and company.lower() not in own and term_in(company, text):
            leaked.add(company)
    if leaked:
        r.blocks.append(f"another prospect's name or company appears: {', '.join(sorted(leaked))}")

    words = word_count(c.body)
    if words > template.max_words:
        r.blocks.append(f"body is {words} words (max {template.max_words})")
    if len(c.subject) > 60:
        r.blocks.append(f"subject is {len(c.subject)} characters (max 60)")
    if not c.subject.strip():
        r.blocks.append("subject is empty")

    if not any(v and term_in(v, c.body) for v in personal_values):
        r.warnings.append("nothing personal beyond the first name")
    if len(LINK_RE.findall(c.full_body)) > 1:
        r.warnings.append("more than one link")
    if c.full_body.count("!") > 1:
        r.warnings.append("more than one exclamation mark")
    opener = next((pid for cat, pid in c.categories.items() if cat.startswith("opener")), None)
    if opener and opener in recent_openers:
        r.warnings.append(f"same opener ({opener}) as one of your last 3 drafts")
    return r


def personal_values(p: Prospect, ctx_school: str | None, ctx_past: str | None, hook: dict[str, Any] | None) -> list[str]:
    vals = [p.company.name, p.team or "", p.current_title, ctx_school or "", ctx_past or ""]
    if hook:
        vals += [v for v in hook["fields"].values() if isinstance(v, str)]
    return [v for v in vals if v]
