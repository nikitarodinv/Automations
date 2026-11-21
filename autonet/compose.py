"""Stage 5: fill a template from the phrase bank (section 8).

Templates are Jinja2 files with YAML front matter, rendered in a sandbox with StrictUndefined.
Phrases are data: only {{variable}} substitution, no logic."""

from __future__ import annotations

import hashlib
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml
from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

from .errors import ConfigError
from .matching import shared_employer, shared_school
from .models import Prospect
from .schemas import ValidationFailed, schema_errors
from .util import stable_json, short_hash

VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)\s*\}\}")
SIGNATURE_MARK = "⁣SIGNATURE⁣"

HOOK_TIERS = {"note": 0, "talk": 1, "writing": 1, "podcast": 1, "deal": 2, "promotion": 2, "company_news": 3}


# --------------------------------------------------------------------------- templates
@dataclass
class Template:
    id: str
    version: int
    use_when: str
    requires: list[str]
    max_words: int
    slots: dict[str, dict[str, Any]]
    body: str
    path: Path
    _compiled: Any = None

    def catalog_entry(self) -> dict[str, Any]:
        return {"id": self.id, "version": self.version, "use_when": self.use_when, "requires": self.requires}


_env = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False, keep_trailing_newline=False)


def parse_template(path: Path) -> Template:
    text = path.read_text()
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.S)
    if not m:
        raise ValidationFailed(f"template {path.name}", ["missing YAML front matter between --- lines"])
    meta = yaml.safe_load(m.group(1)) or {}
    errs = schema_errors("template", meta)
    if errs:
        raise ValidationFailed(f"template {path.name}", errs)
    t = Template(
        id=meta["id"],
        version=meta["version"],
        use_when=meta["use_when"],
        requires=meta.get("requires", []),
        max_words=meta.get("max_words", 150),
        slots=meta["slots"],
        body=m.group(2),
        path=path,
    )
    if path.name.split(".")[0] != t.id:
        raise ValidationFailed(f"template {path.name}", [f"file name must match id '{t.id}'"])
    try:
        t._compiled = _env.from_string(t.body)
    except Exception as e:  # jinja2.TemplateSyntaxError
        raise ValidationFailed(f"template {path.name}", [f"Jinja syntax: {e}"]) from e
    return t


def load_templates(folder: Path) -> dict[str, Template]:
    out: dict[str, Template] = {}
    for p in sorted(folder.glob("*.md.j2")):
        t = parse_template(p)
        out[t.id] = t
    return out


def catalog_hash(templates: list[Template], extra: object = None) -> str:
    return short_hash(stable_json([t.catalog_entry() for t in templates]), stable_json(extra), length=12)


# --------------------------------------------------------------------------- phrase bank
@dataclass
class Phrase:
    id: str
    category: str
    text: str
    requires: list[str] = field(default_factory=list)
    tags: dict[str, list[str]] = field(default_factory=dict)
    weight: float = 1.0
    max_chars: int | None = None

    @property
    def variables(self) -> list[str]:
        return VAR_RE.findall(self.text)


@dataclass
class PhraseBank:
    version: int
    variables: list[str]
    categories: dict[str, list[Phrase]]
    path: Path | None = None

    def declared(self, var: str) -> bool:
        if var in self.variables:
            return True
        return any(v.endswith(".*") and var.startswith(v[:-1]) for v in self.variables)


def load_phrase_bank(path: Path) -> PhraseBank:
    if not path.is_file():
        raise ConfigError(f"phrase bank not found: {path}")
    raw = yaml.safe_load(path.read_text())
    errs = schema_errors("phrase_bank", raw)
    if errs:
        raise ValidationFailed(f"phrase bank {path.name}", errs)
    cats = {
        name: [
            Phrase(
                id=p["id"], category=name, text=p["text"], requires=p.get("requires", []),
                tags=p.get("tags", {}), weight=p.get("weight", 1.0), max_chars=p.get("max_chars"),
            )
            for p in items
        ]
        for name, items in raw["categories"].items()
    }
    return PhraseBank(version=raw["version"], variables=raw.get("variables", []), categories=cats, path=path)


def substitute(text: str, ctx: dict[str, Any]) -> str:
    def rep(m: re.Match[str]) -> str:
        v = ctx.get(m.group(1))
        return m.group(0) if v in (None, "") else str(v)

    return VAR_RE.sub(rep, text)


# --------------------------------------------------------------------------- context
def hook_category(hook: dict[str, Any] | None) -> str | None:
    return f"hook.{hook['type']}" if hook else None


def build_context(campaign_sender: dict[str, Any], schools: list[str], p: Prospect, hook: dict[str, Any] | None) -> dict[str, Any]:
    ctx: dict[str, Any] = {
        "first_name": p.first_name,
        "last_name": p.last_name,
        "company": p.company.name,
        "title": p.current_title,
        "team": p.team,
        "school": shared_school(p, schools),
        "past_company": shared_employer(p, campaign_sender.get("past_companies", [])),
        "sender_name": campaign_sender.get("name"),
        "sender_school": campaign_sender.get("school"),
        "sender_year": campaign_sender.get("year"),
        "topic": campaign_sender.get("topic"),
    }
    if hook:
        for k, v in hook["fields"].items():
            ctx[f"hook.{k}"] = v
    return ctx


def tag_context(p: Prospect, hints: dict[str, Any]) -> dict[str, str]:
    out = {}
    if hints.get("tone"):
        out["tone"] = hints["tone"]
    if hints.get("ask"):
        out["ask"] = hints["ask"]
    if p.seniority:
        out["seniority"] = p.seniority
    return out


# --------------------------------------------------------------------------- picking
def _available(ph: Phrase, ctx: dict[str, Any]) -> bool:
    needed = set(ph.requires) | set(ph.variables)  # used-but-undeclared vars count too
    if not ph.text.strip():
        return False
    if any(ctx.get(v) in (None, "") for v in needed):
        return False
    if ph.max_chars and len(substitute(ph.text, ctx)) > ph.max_chars:
        return False
    return True


def _tags_fit(ph: Phrase, tctx: dict[str, str]) -> bool:
    # A tag key the phrase doesn't set matches anything; a context key we don't know matches anything.
    return all(k not in tctx or tctx[k] in allowed for k, allowed in ph.tags.items())


def _seed(*parts: object) -> int:
    return int.from_bytes(hashlib.sha256("|".join(map(str, parts)).encode()).digest()[:8], "big")


def weighted_pick(cands: list[Phrase], seed: int) -> Phrase:
    cands = sorted(cands, key=lambda c: c.id)
    rng = random.Random(seed)
    total = sum(c.weight for c in cands)
    x = rng.random() * total
    for c in cands:
        x -= c.weight
        if x < 0:
            return c
    return cands[-1]


def pick_phrase(
    bank: PhraseBank,
    category: str,
    ctx: dict[str, Any],
    tctx: dict[str, str],
    used_at_company: set[str],
    seed: int,
    optional: bool,
) -> Phrase | None:
    pool = [ph for ph in bank.categories.get(category, []) if _available(ph, ctx)]      # steps 1-2
    tagged = [ph for ph in pool if _tags_fit(ph, tctx)]                                 # step 3
    fresh = [ph for ph in tagged if ph.id not in used_at_company]                        # step 4
    if fresh:
        return weighted_pick(fresh, seed)
    if optional:
        return None                                                                      # step 5
    for relaxed in (tagged, pool):
        if relaxed:
            return weighted_pick(relaxed, seed)
    return None


# --------------------------------------------------------------------------- compose
@dataclass
class Composed:
    ok: bool
    template_id: str
    template_version: int
    phrase_bank_version: int
    subject: str = ""
    body: str = ""            # without signature
    full_body: str = ""       # with opt-out line and signature
    phrase_ids: dict[str, str] = field(default_factory=dict)     # slot -> phrase id
    categories: dict[str, str] = field(default_factory=dict)     # category -> phrase id
    error: str | None = None


def _tidy(text: str) -> str:
    lines = [re.sub(r"[ \t]{2,}", " ", ln).strip() for ln in text.splitlines()]
    out = "\n".join(lines).strip()
    return re.sub(r"\n{3,}", "\n\n", out)


def compose(
    *,
    template: Template,
    bank: PhraseBank,
    prospect: Prospect,
    sender: dict[str, Any],
    schools: list[str],
    hints: dict[str, Any],
    hook: dict[str, Any] | None,
    signature: str,
    opt_out_line: str | None,
    used_at_company: set[str],
) -> Composed:
    ctx = build_context(sender, schools, prospect, hook)
    tctx = tag_context(prospect, hints)
    res = Composed(ok=False, template_id=template.id, template_version=template.version, phrase_bank_version=bank.version)
    filled: dict[str, str] = {}

    for slot, spec in template.slots.items():
        category = spec["category"]
        optional = bool(spec.get("optional"))
        if category == "hook":
            category = hook_category(hook) or ""
            if not category:
                if not optional:
                    res.error = "no_hook"
                    return res
                filled[slot] = ""
                continue
        seed = _seed(prospect.prospect_id, template.id, template.version, bank.version, slot)
        ph = pick_phrase(bank, category, ctx, tctx, used_at_company, seed, optional)
        if ph is None:
            if optional:
                filled[slot] = ""
                continue
            res.error = f"no_phrase:{category}"
            return res
        filled[slot] = substitute(ph.text, ctx)
        res.phrase_ids[slot] = ph.id
        res.categories[category] = ph.id

    def slot_fn(name: str) -> str:
        if name not in filled:
            raise ConfigError(f"template {template.id} uses slot '{name}' that its front matter doesn't define")
        return filled[name]

    rendered = template._compiled.render(slot=slot_fn, signature=SIGNATURE_MARK)
    before, _, after = rendered.partition(SIGNATURE_MARK)
    res.body = _tidy(before)
    tail = [opt_out_line] if opt_out_line else []
    res.full_body = _tidy("\n\n".join([res.body, *tail, signature.strip()]) + after)
    res.subject = _tidy(filled.get("subject", ""))
    res.ok = True
    return res


# --------------------------------------------------------------------------- lint phrases
TYPICAL = {
    "first_name": "Alexandra", "last_name": "Montgomery", "company": "Goldman Sachs", "title": "Associate",
    "team": "Technology, Media & Telecom", "school": "Northwestern University", "past_company": "Houlihan Lokey",
    "sender_name": "Alex Kim", "sender_school": "Northwestern University", "sender_year": "junior",
    "topic": "investment banking", "hook.topic": "private credit", "hook.event": "Midwest Finance Forum",
    "hook.title": "What the next rate cycle means for M&A", "hook.outlet": "Bloomberg",
    "hook.client": "Acme Industrial", "hook.deal_type": "acquisition", "hook.show": "Deal Talk",
    "hook.new_title": "Vice President", "hook.text": "I enjoyed your recent post.",
}


# Variables that are often empty for a given prospect; phrases using them should say so in requires.
OPTIONAL_VARS = {"team", "school", "past_company", "title"}


def lint_phrases(bank: PhraseBank, templates: dict[str, Template], typical: dict[str, str] | None = None) -> tuple[list[str], list[str]]:
    """Returns (errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []
    vals = {**TYPICAL, **(typical or {})}
    seen: dict[str, str] = {}
    for cat, phrases in bank.categories.items():
        for ph in phrases:
            where = f"{cat}/{ph.id}"
            if ph.id in seen:
                errors.append(f"{where}: duplicate id (also in {seen[ph.id]})")
            seen[ph.id] = cat
            if not ph.text.strip():
                errors.append(f"{where}: text is empty")
            for v in ph.requires:
                if not bank.declared(v):
                    errors.append(f"{where}: requires undeclared variable '{v}'")
            for v in ph.variables:
                if not bank.declared(v):
                    errors.append(f"{where}: uses undeclared variable '{v}'")
                elif v in OPTIONAL_VARS and v not in ph.requires:
                    warnings.append(f"{where}: uses '{v}' without listing it in requires")
            if "{%" in ph.text or "{#" in ph.text:
                errors.append(f"{where}: phrases allow only {{{{variable}}}} substitution, not Jinja logic")
            if cat.startswith("subject"):
                filled = substitute(ph.text, vals)
                limit = min(ph.max_chars or 60, 60)
                if len(filled) > limit:
                    msg = f"{where}: subject is {len(filled)} chars with typical values (max {limit}): {filled!r}"
                    if ph.max_chars:  # skipped automatically for people where it would overflow
                        warnings.append(msg + "; it will be skipped when too long")
                    else:
                        errors.append(msg + "; add max_chars or shorten it")
    for t in templates.values():
        for slot, spec in t.slots.items():
            cat = spec["category"]
            if cat == "hook":
                continue
            n = len(bank.categories.get(cat, []))
            if n == 0:
                errors.append(f"template {t.id}: slot '{slot}' uses missing category '{cat}'")
            elif n < 2:
                errors.append(f"template {t.id}: category '{cat}' has {n} phrase; at least 2 needed")
    return errors, warnings
