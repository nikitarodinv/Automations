"""Stage orchestration. Each stage reads prospects in its input state from SQLite, writes its
output back, and commits after every prospect, so any stage can be rerun alone."""

from __future__ import annotations

import os

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from .campaign import IMPLEMENTED_DELIVERY, Campaign, load_campaign, resolve_campaign_path
from .compose import Composed, PhraseBank, Template, catalog_hash, compose, load_phrase_bank, load_templates
from .config import Config
from .delivery import get_delivery, write_review
from .delivery.outlook_mcp import read_acks
from .draft import lint_draft, personal_values
from .emails import ACCEPTED, decide
from .errors import AuthError, ConfigError, DataError, QuotaError, TransientError
from .ledger import Ledger, RunBudget
from .logs import JsonLog
from .matching import apply_filters, fill_company_domain, infer_seniority, qualification_score, shared_employer, shared_school
from .models import EmailResult, Prospect
from .pool import Pool
from .providers import get_finder, get_source
from .selector import Decision, RulesSelector, SelectionContext, known_requirement
from .store import Store
from .util import iso, now_utc, parse_iso, short_hash, word_count

LOOKUP_REUSE_DAYS = 90
NOTE_MAX_WORDS = 40


@dataclass
class Report:
    run_id: str
    campaign_id: str
    dry_run: bool = False
    counts: Counter = field(default_factory=Counter)
    needs_you: Counter = field(default_factory=Counter)
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    filtered_locally: set[str] = field(default_factory=set)
    delivery: str = ""
    credits: float = 0
    credit_budget: float = 0
    llm_calls: float = 0
    llm_budget: int = 0
    providers: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)

    def render(self) -> str:
        c = self.counts
        tag = " (dry run)" if self.dry_run else ""
        lines = [
            f"Run {self.run_id} · {self.campaign_id}{tag}",
            f"Discovered {c['discovered']} → qualified {c['qualified']} → "
            f"templates: {c['selected']} picked, {c['select_skipped']} skipped, {c['select_review']} to review"
            f" (selector: {', '.join(sorted(k[9:] for k in c if k.startswith('selector:') )) or 'n/a'})",
            f"Emails: {c['email_found']} found, {c['email_not_found']} not found, {c['email_unconfirmed']} unconfirmed, "
            f"{c['email_pending']} waiting → {c['drafted']} drafts"
            + (f" → {c['delivered']} delivered ({self.delivery})" if self.delivery else ""),
            f"Finder credits {self.credits:g} of {self.credit_budget:g} · model calls {self.llm_calls:g} of {self.llm_budget}",
        ]
        if c["searches"] or c["pages_read"] or c["hooks_found"]:
            lines.insert(3, f"Research: {c['hooks_found']} with a public detail · {c['searches']} searches · "
                            f"{c['pages_read']} pages read" + (f" · {c['reselected']} template switches" if c["reselected"] else ""))
        if c["polished"] or c["polish_rejected"]:
            lines.append(f"Polish: {c['polished']} applied, {c['polish_rejected']} rejected by the guards")
        if self.needs_you:
            lines.append("Needs you: " + ", ".join(f"{n} {k}" for k, n in sorted(self.needs_you.items())))
        lines += [f"  {d}" for d in self.details]
        sources = sorted(k[7:] for k in c if k.startswith("source:"))
        if len(sources) > 1:
            lines.append("Sources: " + " · ".join(f"{s} {c['source:' + s]}" for s in sources))
        lines += self.providers
        if self.filtered_locally:
            lines.append("Filters applied locally: " + ", ".join(sorted(self.filtered_locally)))
        lines += [f"Note: {n}" for n in self.notes]
        lines += [f"Error: {e}" for e in self.errors]
        lines.append("Review: /autonetworker review")
        return "\n".join(lines)


@dataclass
class Ctx:
    cfg: Config
    campaign: Campaign
    store: Store
    run_id: str
    report: Report
    templates: dict[str, Template]
    bank: PhraseBank
    signature: str
    log: JsonLog
    ledger: Ledger
    dry_run: bool = False
    max_drafts: int = 15
    pool: Any = None
    selector: Any = None
    llm: Any = None
    researcher: Any = None
    disabled: dict[str, str] = field(default_factory=dict)
    finder_spent: float = 0.0
    live_jev: bool = False

    @property
    def cid(self) -> str:
        return self.campaign.id

    @property
    def out_dir(self) -> Path:
        return self.cfg.path("out") / self.run_id

    @property
    def allowed(self) -> list[Template]:
        return [self.templates[t] for t in self.campaign["templates"]["allowed"]]

    def commit(self) -> None:
        self.store.db.commit()


# --------------------------------------------------------------------------- setup
def open_store(cfg: Config, dry_run: bool, db: Path | None = None, fresh: bool = False) -> Store:
    path = db or cfg.path("dry_run_db" if dry_run else "db")
    if fresh:
        if not dry_run and db is None:
            raise ConfigError("--fresh only applies to dry runs")
        for suffix in ("", "-wal", "-shm"):
            Path(str(path) + suffix).unlink(missing_ok=True)
    return Store(path)


def load_signature(cfg: Config, key: str) -> str:
    path = cfg.path("signatures")
    sigs = yaml.safe_load(path.read_text()) if path.is_file() else {}
    if key not in (sigs or {}):
        raise ConfigError(f"signature '{key}' not found in {path}")
    return str(sigs[key])


def check_campaign(cfg: Config, campaign: Campaign, dry_run: bool = False) -> tuple[dict[str, Template], PhraseBank, list[str]]:
    """Everything `autonet validate` checks beyond the schema. Returns (templates, bank, warnings)."""
    templates = load_templates(cfg.path("templates"))
    problems: list[str] = []
    warnings: list[str] = []
    for tid in campaign["templates"]["allowed"]:
        if tid not in templates:
            problems.append(f"templates.allowed: unknown template '{tid}' (known: {', '.join(sorted(templates))})")
        else:
            for req in templates[tid].requires:
                if not known_requirement(req):
                    problems.append(f"template {tid}: unknown requirement '{req}'")
    bank = load_phrase_bank(cfg.resolve(campaign["phrase_bank"]))
    load_signature(cfg, campaign.sender.get("signature", "default"))
    if not dry_run:
        warnings += provider_warnings(cfg, campaign)
        if campaign["delivery"] not in IMPLEMENTED_DELIVERY:
            warnings.append(f"delivery '{campaign['delivery']}' is unknown")
        mailbox = cfg.data.get("mailbox") or {}
        if campaign["delivery"] == "graph_draft" and not (mailbox.get("microsoft") or {}).get("client_id"):
            warnings.append("delivery graph_draft: set mailbox.microsoft.client_id in config.yaml, then `autonet auth microsoft`")
        if campaign["delivery"] == "gmail_draft":
            warnings.append("delivery gmail_draft: run `autonet auth google` once before the first run")
        if campaign["research"].get("enabled"):
            from .search import PROVIDERS

            sp = campaign["research"].get("search_provider", "brave")
            env = getattr(PROVIDERS.get(sp), "key_env", "")
            if env and not os.environ.get(env):
                warnings.append(f"research: {env} not set; only Hunter's source pages will be read")
        if campaign["source"] == "csv":
            p = cfg.resolve(campaign["source_options"]["path"])
            if not p.is_file():
                warnings.append(f"source_options.path: {p} doesn't exist yet; real runs need it (dry runs don't)")
    if problems:
        raise ConfigError("campaign check failed:\n  - " + "\n  - ".join(problems))
    return templates, bank, warnings


def provider_warnings(cfg: Config, campaign: Campaign) -> list[str]:
    """Which providers a real run would skip, without any network call."""
    from .providers import manifests

    pc = cfg.data.get("providers") or {}
    out = []
    names = [e["name"] for e in pc.get("sources") or []] if campaign["source"] == "pool" else [campaign["source"]]
    names += [e["name"] for e in pc.get("finders") or []]
    for n in dict.fromkeys(names):
        m = manifests().get({"hunter_domain": "hunter"}.get(n, n))
        if m and not m.key():
            out.append(f"provider {n}: {m.key_env} not set; it will be skipped")
    if campaign["source"] == "pool" and not pc.get("sources"):
        out.append("source: pool but config.yaml has no providers.sources")
    if not pc.get("finders"):
        out.append("config.yaml has no providers.finders; only addresses your sources carry can be used")
    jev = (cfg.data.get("jev") or {}).get("provider", "off")
    if jev not in (None, False, "off"):
        from .jev import ROUTES

        env = ROUTES.get(jev, {}).get("key_env")
        if env and not __import__("os").environ.get(env):
            out.append(f"jev.provider {jev}: {env} not set; the rules fallback will decide")
    return out


def new_run_id(store: Store, dry_run: bool) -> str:
    base = ("dry_" if dry_run else "r_") + datetime.now().strftime("%Y%m%d_%H%M%S")
    rid, n = base, 1
    while store.run_exists(rid):
        n += 1
        rid = f"{base}_{n}"
    return rid


def make_ctx(
    cfg: Config,
    campaign: Campaign,
    store: Store,
    *,
    dry_run: bool = False,
    limit: int | None = None,
    debug: bool = False,
    run_id: str | None = None,
    live_jev: bool = False,
) -> Ctx:
    templates, bank, warnings = check_campaign(cfg, campaign, dry_run)
    rid = run_id or new_run_id(store, dry_run)
    if not store.run_exists(rid):
        store.start_run(rid, campaign.id, dry_run)
    lim = campaign.limits
    report = Report(
        run_id=rid, campaign_id=campaign.id, dry_run=dry_run,
        credit_budget=lim["hunter_credit_budget"], llm_budget=lim["llm_call_budget"],
    )
    report.notes += warnings
    ledger = Ledger(store, rid, RunBudget(lim["hunter_credit_budget"], lim["llm_call_budget"], lim["search_budget"]))
    ctx = Ctx(
        cfg=cfg, campaign=campaign, store=store, run_id=rid, report=report, templates=templates, bank=bank,
        signature=load_signature(cfg, campaign.sender.get("signature", "default")),
        log=JsonLog(cfg.path("logs"), debug=debug), ledger=ledger, dry_run=dry_run,
        max_drafts=min(limit or lim["max_drafts_per_run"], lim["max_drafts_per_run"]), live_jev=live_jev,
    )
    budget = lim["hunter_credit_budget"]
    ctx.pool = Pool(cfg, store, ledger, campaign, finder_budget_check=lambda c: ctx.finder_spent + c <= budget + 1e-9)
    return ctx


def selection_context(ctx: Ctx, p: Prospect) -> SelectionContext:
    prior = ctx.store.db.execute("SELECT 1 FROM contact_log WHERE prospect_id=?", (p.prospect_id,)).fetchone()
    return SelectionContext(
        prospect=p,
        schools=ctx.campaign.targeting.get("schools", []),
        sender_past=ctx.campaign.sender.get("past_companies", []),
        prior_contact=bool(prior),
        hooks=ctx.store.hooks_for(p.prospect_id),
    )


# --------------------------------------------------------------------------- stage 1
ALL_FILTERS = {"keywords", "titles", "companies", "locations", "seniority"}


def _sources(ctx: Ctx) -> list[Any]:
    camp = ctx.campaign
    if ctx.dry_run:
        return [get_source("fixture", path=str(ctx.cfg.resolve(ctx.cfg["dry_run"]["prospects"])))]
    opts = dict(camp["source_options"])
    if "path" in opts:
        opts["path"] = str(ctx.cfg.resolve(opts["path"]))
    sources = ctx.pool.sources(camp["source"], opts)
    if not sources:
        why = "; ".join(f"{k}: {v}" for k, v in ctx.pool.skipped.items()) or "none configured"
        raise ConfigError(f"no people source available ({why})")
    return sources


def stage_discover(ctx: Ctx) -> None:
    camp, store, rep = ctx.campaign, ctx.store, ctx.report
    targeting = camp.targeting
    lim = camp.limits
    candidates: list[tuple[Prospect, list[str]]] = []

    for source in _sources(ctx):
        need = lim["max_prospects_per_run"] - len(candidates)
        if need <= 0:
            break
        rep.filtered_locally |= ALL_FILTERS - source.supported_filters()
        try:
            for p in source.search(camp, need):
                rep.counts["discovered"] += 1
                rep.counts[f"source:{source.name}"] += 1
                fill_company_domain(p, targeting.get("companies", {}).get("include", []))
                if not p.seniority:
                    p.seniority = infer_seniority(p.current_title or p.headline)
                user_selected = bool((p.match or {}).get("user_selected"))
                p = store.upsert_prospect(p)
                cp = store.get_cp(ctx.cid, p.prospect_id)
                if cp and cp["state"] not in ("discovered",):
                    ctx.commit()
                    continue  # already further along (or suppressed/skipped) in this campaign
                fr = apply_filters(p, targeting)
                if not fr.passed and not user_selected:
                    store.set_state(ctx.cid, p.prospect_id, "discovered", reason="; ".join(fr.reasons), run_id=ctx.run_id)
                    rep.counts["filtered_out"] += 1
                    ctx.commit()
                    continue
                p.match = {"keywords_hit": fr.keywords_hit, "filtered_locally": sorted(rep.filtered_locally),
                           **({"user_selected": True} if user_selected else {})}
                store.upsert_prospect(p)
                store.set_state(ctx.cid, p.prospect_id, "discovered", reason=None, run_id=ctx.run_id)
                candidates.append((p, fr.keywords_hit))
                ctx.commit()
                if len(candidates) >= lim["max_prospects_per_run"]:
                    break
        except (QuotaError, TransientError, DataError) as e:
            rep.notes.append(f"source {source.name} stopped: {e}")
            ctx.commit()

    scored = []
    for p, hits in candidates:
        why = store.is_suppressed(p.prospect_id, p.known_email)
        if why:
            store.set_state(ctx.cid, p.prospect_id, "suppressed", reason=why, run_id=ctx.run_id)
            rep.counts["suppressed"] += 1
            continue
        if store.in_cooldown(p.prospect_id, lim["recontact_cooldown_days"]):
            store.set_state(ctx.cid, p.prospect_id, "discovered", reason="recontact cooldown", run_id=ctx.run_id)
            rep.counts["cooldown"] += 1
            continue
        scored.append((qualification_score(p, targeting, camp.sender, hits), p))
    ctx.commit()

    scored.sort(key=lambda sp: (-sp[0], sp[1].prospect_id))
    per_company: Counter = Counter()
    taken = 0
    for score, p in scored:
        key = (p.company.domain or p.company.normalized).lower()
        if taken >= lim["max_prospects_per_run"]:
            store.set_state(ctx.cid, p.prospect_id, "discovered", reason="over max_prospects_per_run", score=score)
            continue
        if key and per_company[key] >= lim["max_per_company_per_run"]:
            store.set_state(ctx.cid, p.prospect_id, "discovered", reason="over max_per_company_per_run", score=score)
            continue
        per_company[key] += 1
        taken += 1
        store.set_state(ctx.cid, p.prospect_id, "qualified", score=score, run_id=ctx.run_id)
        rep.counts["qualified"] += 1
        ctx.commit()
    ctx.commit()


# --------------------------------------------------------------------------- stage 2
def selector_for(ctx: Ctx) -> Any:
    jcfg = ctx.cfg["jev"]
    provider = jcfg.get("provider", "off")
    if provider in (None, False, "off"):
        return RulesSelector()
    if ctx.dry_run and not ctx.live_jev:
        ctx.report.notes.append("dry run: rules selector (add --live-jev to ask Jev; it costs a fraction of a cent)")
        return RulesSelector()
    from .jev import JevClient, JevSelector

    try:
        client = JevClient(route=provider, model=jcfg.get("model"), timeout=float(jcfg.get("timeout", 10)))
    except ConfigError as e:
        ctx.report.notes.append(f"{e}; the rules fallback decided every template")
        return RulesSelector()
    return JevSelector(client, ctx.campaign, float(jcfg.get("min_confidence", 0.6)), jcfg.get("pin_version"))


def stage_select(ctx: Ctx) -> None:
    sel = ctx.selector = ctx.selector or selector_for(ctx)
    fallback = ctx.campaign["templates"]["fallback"]
    for cp in ctx.store.in_state(ctx.cid, "qualified"):
        p = ctx.store.get_prospect(cp["prospect_id"])
        assert p
        d = sel.select(ctx.store, ctx.cid, selection_context(ctx, p), ctx.allowed, fallback, ctx.ledger)
        ctx.report.counts[f"selector:{d.selector}"] += 1
        if d.route == "continue":
            ctx.store.set_state(ctx.cid, p.prospect_id, "selected", run_id=ctx.run_id)
            ctx.report.counts["selected"] += 1
        elif d.route == "review":
            ctx.store.set_state(ctx.cid, p.prospect_id, "review", reason=d.review_reason, resume_state="selected", run_id=ctx.run_id)
            ctx.report.counts["select_review"] += 1
            ctx.report.needs_you["template picks to review"] += 1
            top2 = (d.reason or {}).get("top2")
            if top2:
                ctx.report.details.append(f"{p.full_name}: " + " · ".join(f"{k} {v:.2f}" for k, v in top2))
        else:
            ctx.store.set_state(ctx.cid, p.prospect_id, "skipped", reason=d.review_reason, run_id=ctx.run_id)
            ctx.report.counts["select_skipped"] += 1
        ctx.log.write(run_id=ctx.run_id, stage="select", prospect_id=p.prospect_id, provider=d.selector, status=d.route, template=d.template)
        ctx.commit()
    if getattr(sel, "fallbacks", 0):
        ctx.report.notes.append(f"Jev unavailable for {sel.fallbacks} prospect(s); used the rules fallback"
                                + (f" ({sel.disabled})" if getattr(sel, "disabled", None) else ""))


# --------------------------------------------------------------------------- stage 3
def _lookup_key(p: Prospect) -> tuple[str, str, str]:
    return p.first_name, p.last_name, (p.company.domain or p.company.normalized)


def _merge_person(ctx: Ctx, p: Prospect, person: dict[str, Any]) -> Prospect:
    """A finder that matched on a LinkedIn handle tells us the name and company."""
    if not person:
        return p
    from .models import Company

    upd = Prospect(
        first_name=person.get("first_name") or p.first_name, last_name=person.get("last_name") or p.last_name,
        company=Company(person.get("company") or p.company.name, person.get("domain") or p.company.domain),
        source=p.source, prospect_id=p.prospect_id, linkedin_url=p.linkedin_url,
        current_title=p.current_title or person.get("position") or "",
    )
    if not upd.seniority:
        upd.seniority = p.seniority or infer_seniority(upd.current_title)
    return ctx.store.upsert_prospect(upd)


def _apply_email_decision(ctx: Ctx, p: Prospect, decision: str, rec: dict[str, Any]) -> None:
    store, rep = ctx.store, ctx.report
    rec = {**rec, "decision": decision}
    store.save_lookup(*_lookup_key(p), p.prospect_id, rec)
    if decision in ACCEPTED:
        if store.is_suppressed(p.prospect_id, rec.get("email")):
            store.set_state(ctx.cid, p.prospect_id, "suppressed", reason="email on suppression list")
            return
        store.set_state(ctx.cid, p.prospect_id, "email_found", run_id=ctx.run_id)
        rep.counts["email_found"] += 1
        if decision == "accepted_catch_all":
            rep.needs_you["catch-all addresses to double-check"] += 1
    elif decision == "unconfirmed":
        store.set_state(ctx.cid, p.prospect_id, "review", reason="email_unconfirmed", resume_state="email_found", run_id=ctx.run_id)
        rep.counts["email_unconfirmed"] += 1
        rep.needs_you["unconfirmed emails"] += 1
    elif decision == "verify_retry":
        store.set_state(ctx.cid, p.prospect_id, "pending_email", reason="mail server answered oddly; verifying again next run")
        rep.counts["email_pending"] += 1
    elif decision == "claimed":
        store.suppress(p.prospect_id, "prospect", "claimed_email")
        rep.counts["suppressed"] += 1
    elif decision == "data_error":
        store.set_state(ctx.cid, p.prospect_id, "skipped", reason=f"data_error: {rec.get('error')}", run_id=ctx.run_id)
        rep.counts["data_error"] += 1
    else:
        store.set_state(ctx.cid, p.prospect_id, "skipped", reason=f"email_{decision}", run_id=ctx.run_id)
        rep.counts["email_not_found"] += 1


def _rec(r: EmailResult, **extra: Any) -> dict[str, Any]:
    return {"email": r.email, "score": r.score, "status": r.status, "accept_all": r.status == "accept_all",
            "sources": r.sources, "provider": r.provider, "credits_used": r.credits_used, "checked_at": iso(), **extra}


def _finders(ctx: Ctx) -> list[Any]:
    if ctx.dry_run:
        f = get_finder("fixture_finder", path=str(ctx.cfg.resolve(ctx.cfg["dry_run"]["emails"])))
        return [ctx.pool._wire(f, finder=True)]
    return ctx.pool.finders()


def waterfall(ctx: Ctx, finders: list[Any], p: Prospect, seed: list[tuple[EmailResult, str]] | None = None
              ) -> tuple[str, dict[str, Any], Prospect] | None:
    """Pool rule 5. Returns (decision, lookup record, prospect) or None = nothing could be tried now."""
    rules = ctx.campaign.email
    candidates: list[tuple[EmailResult, str]] = list(seed or [])
    transient = False
    answered = 0
    data_errors: list[str] = []
    for f in finders:
        if not f.can_spend(f.cost.get("found", 1)):
            continue
        try:
            r = f.find(p)
        except QuotaError as e:
            ctx.pool.exhausted.add(f.account or f.name)
            ctx.report.notes.append(f"{f.name}: {e}; skipping it for the rest of this run")
            continue
        except TransientError as e:
            transient = True
            ctx.report.notes.append(str(e))
            continue
        except DataError as e:
            data_errors.append(str(e))
            continue
        answered += 1
        f.charge("find", credits=r.credits_used, status=200)
        ctx.finder_spent += r.credits_used
        ctx.log.write(run_id=ctx.run_id, stage="find-emails", prospect_id=p.prospect_id, provider=f.name, status=r.status, email=r.email)
        if r.person:
            p = _merge_person(ctx, p, r.person)
        dec = decide(r, rules)
        if dec == "claimed":
            return "claimed", _rec(r), p
        if dec in ACCEPTED:
            return dec, _rec(r), p
        if r.email:
            same = [c for c in candidates if c[0].email and c[0].email.lower() == r.email.lower()]
            if same and dec != "invalid" and all(c[1] != "invalid" for c in same):
                return "confirmed", _rec(r, provider=f"{same[0][0].provider}+{r.provider}"), p
            candidates.append((r, dec))
    live = [c for c in candidates if c[1] != "invalid"]
    if any(c[0].raw.get("retry_verify") for c in live):
        r = next(c[0] for c in live if c[0].raw.get("retry_verify"))
        return "verify_retry", _rec(r), p
    if live:
        r = max(live, key=lambda c: c[0].score or 0)[0]
        return "unconfirmed", _rec(r), p
    if candidates:
        return "invalid", _rec(candidates[0][0]), p
    if answered == 0:
        if transient or not data_errors:
            return None   # nothing could be tried (out of credits, budget, or a provider hiccup): retry next run
        return "data_error", {"error": data_errors[0], "checked_at": iso()}, p
    return "not_found", {"email": None, "provider": "+".join(f.name for f in finders), "checked_at": iso()}, p


def stage_find_emails(ctx: Ctx) -> None:
    store, rep = ctx.store, ctx.report
    finders = _finders(ctx)
    todo = store.in_state(ctx.cid, "selected") + store.in_state(ctx.cid, "pending_email")
    # Don't pay for more addresses than this run can turn into drafts.
    slots = ctx.max_drafts - len(store.in_state(ctx.cid, "email_found")) - len(store.in_state(ctx.cid, "researched"))
    if not ctx.dry_run:
        slots = min(slots, ctx.cfg["limits"]["daily_draft_cap"] - store.drafts_created_since(_local_midnight_utc_iso()))
    deferred = 0
    budget_note = False

    for cp in todo:
        p = store.get_prospect(cp["prospect_id"])
        assert p
        if store.is_suppressed(p.prospect_id, p.known_email):
            store.set_state(ctx.cid, p.prospect_id, "suppressed", reason="suppression list")
            ctx.commit()
            continue
        # 1. Reuse what's known: an address the source carried, or a recent lookup.
        seed: list[tuple[EmailResult, str]] = []
        if p.known_email:
            if p.known_email_status is None:   # you supplied it
                _apply_email_decision(ctx, p, "manual", {"email": p.known_email, "score": None, "status": "user_supplied", "provider": p.source})
                ctx.commit()
                continue
            known = EmailResult(p.known_email, p.known_email_score, p.known_email_status, p.known_email_provider or p.source)
            dec = decide(known, ctx.campaign.email)
            if dec in ACCEPTED:
                _apply_email_decision(ctx, p, dec, _rec(known))
                ctx.commit()
                continue
            seed = [(known, dec)]
        prev = store.get_lookup(*_lookup_key(p))
        fresh = prev and prev["decision"] and parse_iso(prev["checked_at"]) > now_utc() - timedelta(days=LOOKUP_REUSE_DAYS)
        if fresh and prev["decision"] in ACCEPTED | {"unconfirmed", "not_found", "invalid"}:
            _apply_email_decision(ctx, p, prev["decision"], {**prev, "credits_used": 0})
            ctx.commit()
            continue
        if rep.counts["email_found"] >= slots:
            deferred += 1  # stays in its state; the next run looks it up
            continue
        if fresh and prev["decision"] == "verify_retry":
            hunter = next((f for f in finders if hasattr(f, "reverify")), None)
            if hunter and hunter.can_spend(0.5):
                r = hunter.reverify(prev["email"], prev["score"])
                hunter.charge("email-verifier", credits=r.credits_used, status=200)
                ctx.finder_spent += r.credits_used
                dec = decide(r, ctx.campaign.email)
                if r.raw.get("retry_verify"):
                    dec = "unconfirmed"   # retried once already
                _apply_email_decision(ctx, p, dec, _rec(r))
            else:
                _apply_email_decision(ctx, p, "unconfirmed", {**prev, "credits_used": 0})
            ctx.commit()
            continue
        # 2-4. Find with the waterfall, verify when needed, decide.
        out = waterfall(ctx, finders, p, seed)
        if out is None:
            if ctx.finder_spent >= ctx.ledger.budget.finder_credits and not budget_note:
                rep.notes.append(f"Stage 3 stopped: run credit budget reached ({ctx.finder_spent:g} of {ctx.ledger.budget.finder_credits:g} used)")
                budget_note = True
            why = "run credit budget reached" if budget_note else ("no email finder available" if not finders else "every finder is out of credits for now")
            store.set_state(ctx.cid, p.prospect_id, "pending_email", reason=why)
            rep.counts["email_pending"] += 1
            ctx.commit()
            continue
        decision, rec, p2 = out
        _apply_email_decision(ctx, p2, decision, rec)
        if decision in ACCEPTED and not (p.company.name and p.first_name) and p2.company.name:
            # The template was picked on an empty profile (a bare LinkedIn URL); pick again now we know who it is.
            sel = ctx.selector or selector_for(ctx)
            ctx.selector = sel
            sel.select(ctx.store, ctx.cid, selection_context(ctx, p2), ctx.allowed, ctx.campaign["templates"]["fallback"], ctx.ledger)
        ctx.commit()
    if deferred:
        rep.notes.append(f"draft limit reached: {deferred} email lookups saved for the next run")
    if not finders and not ctx.dry_run:
        skipped = ", ".join(f"{k} ({v})" for k, v in ctx.pool.skipped.items())
        rep.notes.append("no email finder available" + (f": {skipped}" if skipped else "; add providers.finders to config.yaml"))
    rep.credits = ctx.finder_spent


# --------------------------------------------------------------------------- stage 4
def _llm(ctx: Ctx) -> Any:
    if ctx.llm is None:
        from .llm import from_config

        ctx.llm = from_config(ctx.cfg)
    return ctx.llm


def _researcher(ctx: Ctx) -> Any:
    from .fetch import Fetcher
    from .research import Researcher
    from .search import get_search

    rcfg = ctx.campaign["research"]
    search = None
    try:
        search = get_search(rcfg.get("search_provider", "brave"))
    except ConfigError as e:
        ctx.report.notes.append(f"research: {e}; using Hunter's source pages only")
    jev = None
    jcfg = ctx.cfg["jev"]
    if jcfg.get("provider", "off") not in (None, False, "off"):
        from .jev import JevClient, JevIdentity

        try:
            jev = JevIdentity(JevClient(route=jcfg["provider"], model=jcfg.get("model"), timeout=float(jcfg.get("timeout", 10))))
        except ConfigError as e:
            ctx.report.notes.append(f"research: {e}; pages checked by name only")
    monthly = (ctx.cfg.data.get("budgets") or {}).get("search_monthly")
    name = getattr(search, "name", "search")

    def search_ok() -> bool:
        if monthly is None:
            return True
        start = now_utc().replace(day=1, hour=0, minute=0, second=0)
        return ctx.ledger.total_since(name, iso(start), "searches") < monthly

    blocked = list(dict.fromkeys(["linkedin.com", *rcfg.get("blocked_domains", [])]))
    return Researcher(research_cfg=rcfg, search=search, fetcher=Fetcher(blocked_domains=blocked), llm=_llm(ctx),
                      jev=jev, ledger=ctx.ledger, search_ok=search_ok)


def stage_research(ctx: Ctx) -> None:
    """Stage 4. Research never blocks a draft: whatever happens, the prospect moves on to `researched`."""
    todo = ctx.store.in_state(ctx.cid, "email_found")
    rcfg = ctx.campaign["research"]
    if not todo:
        return
    if not rcfg.get("enabled") or ctx.dry_run:
        if rcfg.get("enabled") and ctx.dry_run:
            ctx.report.notes.append("dry run: web research skipped (it would search and fetch live pages)")
        for cp in todo:
            ctx.store.set_state(ctx.cid, cp["prospect_id"], "researched", run_id=ctx.run_id)
            ctx.commit()
        return
    researcher = ctx.researcher or _researcher(ctx)
    ctx.researcher = researcher
    for cp in todo:
        p = ctx.store.get_prospect(cp["prospect_id"])
        assert p
        lk = ctx.store.get_lookup(*_lookup_key(p)) or {}
        try:
            res = researcher.run(p, lk.get("sources") or [])
        except Exception as e:  # never let research block a draft
            ctx.report.notes.append(f"research failed for {p.full_name}: {type(e).__name__}: {e}")
            res = None
        if res is not None:
            ctx.report.counts["searches"] += res.searches
            ctx.report.counts["pages_read"] += res.pages
            for h in res.hooks:
                ctx.store.save_hook(h)
            if res.hooks:
                ctx.report.counts["hooks_found"] += 1
                ctx.log.write(run_id=ctx.run_id, stage="research", prospect_id=p.prospect_id, status="hook",
                              hook=res.hooks[0]["type"], url=res.hooks[0]["source_url"])
                if rcfg.get("reselect", True) and any(h["tier"] in (1, 2) for h in res.hooks):
                    # Jev (or the rules) may switch to a template that uses this kind of detail.
                    sel = ctx.selector or selector_for(ctx)
                    ctx.selector = sel
                    before = (ctx.store.latest_decision(ctx.cid, p.prospect_id) or {}).get("template")
                    try:
                        d = sel.select(ctx.store, ctx.cid, selection_context(ctx, p), ctx.allowed,
                                       ctx.campaign["templates"]["fallback"], ctx.ledger)
                        if d.template and d.template != before and d.route == "continue":
                            ctx.report.counts["reselected"] += 1
                    except QuotaError as e:
                        ctx.report.notes.append(f"template re-check skipped: {e}")
            ctx.report.notes += res.notes
        ctx.store.set_state(ctx.cid, p.prospect_id, "researched", run_id=ctx.run_id)
        ctx.commit()
    for k, v in researcher.disabled.items():
        ctx.report.notes.append(f"research {k}: {v}")


# --------------------------------------------------------------------------- stage 5 + 6 (compose, lint)
def draft_id_for(campaign_id: str, pid: str) -> str:
    return "d_" + short_hash(campaign_id, pid)


def _best_hook(ctx: Ctx, p: Prospect, draft: dict[str, Any] | None) -> dict[str, Any] | None:
    if draft and draft.get("hook_id"):
        h = ctx.store.get_hook(draft["hook_id"])
        if h and h["type"] == "note":
            return h
    hooks = ctx.store.hooks_for(p.prospect_id)
    hooks = [h for h in hooks if h["type"] != "note"]  # notes belong to one draft
    return hooks[0] if hooks else None


def _others(ctx: Ctx, pid: str) -> list[tuple[str, str]]:
    out = []
    for cp in ctx.store.campaign_prospects(ctx.cid):
        if cp["prospect_id"] == pid:
            continue
        o = ctx.store.get_prospect(cp["prospect_id"])
        if o:
            out.append((o.full_name, o.company.name))
    return out


def compose_one(ctx: Ctx, p: Prospect, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build (or rebuild) one prospect's draft and lint it. Returns the stored draft record."""
    store = ctx.store
    did = draft_id_for(ctx.cid, p.prospect_id)
    existing = existing or store.get_draft(did)
    decision = store.latest_decision(ctx.cid, p.prospect_id)
    if not decision or not decision.get("template"):
        raise DataError("no template decision")
    tid = decision["template"]
    if tid not in ctx.templates:
        raise DataError(f"template '{tid}' no longer exists")
    template = ctx.templates[tid]
    hook = _best_hook(ctx, p, existing)
    email = store.accepted_email(p)
    domain = (p.company.domain or p.company.normalized).lower()
    used = store.phrases_used_at_company(ctx.cid, domain, did)
    opt_out = ctx.cfg["draft"]["opt_out_line"] if ctx.cfg["draft"].get("include_opt_out_line", True) else None
    schools = ctx.campaign.targeting.get("schools", [])

    c: Composed = compose(
        template=template, bank=ctx.bank, prospect=p, sender=ctx.campaign.sender, schools=schools,
        hints=decision.get("hints") or {}, hook=hook, signature=ctx.signature, opt_out_line=opt_out,
        used_at_company=used,
    )
    polish_audit = None
    if c.ok and ctx.cfg["draft"].get("polish") and not ctx.dry_run:
        c, polish_audit = _maybe_polish(ctx, c, template, existing)
    if c.ok:
        lint = lint_draft(
            c, p, template, email["email"] if email else None, _others(ctx, p.prospect_id),
            store.recent_openers(did), personal_values(p, shared_school(p, schools),
            shared_employer(p, ctx.campaign.sender.get("past_companies", [])), hook),
        ).as_dict()
    else:
        lint = {"blocks": [c.error or "compose failed"], "warnings": []}
    hook_summary = None
    if hook:
        hook_summary = f"tier {hook['tier']} {hook['type']}: " + "; ".join(f"{k}={v}" for k, v in hook["fields"].items())
        if hook.get("source_url"):
            hook_summary += f" ({hook['source_url']})"
    reason = decision.get("reason") or {}
    rec = {
        "draft_id": did, "campaign_id": ctx.cid, "prospect_id": p.prospect_id,
        "run_id": existing["run_id"] if existing else ctx.run_id,
        "template_id": template.id, "template_version": template.version, "phrase_bank_version": ctx.bank.version,
        "phrase_ids": c.phrase_ids, "hook_id": hook["hook_id"] if hook else None,
        "note": existing.get("note") if existing else None,
        "to_email": email["email"] if email else None, "email_score": email["score"] if email else None,
        "subject": c.subject, "body": c.full_body, "lint": lint,
        "audit": {
            "name": p.full_name, "company": p.company.name, "linkedin_url": p.linkedin_url, "source": p.source,
            "selector": decision["selector"], "selector_reason": reason.get("rule") or reason.get("top2"),
            "confidence": decision.get("confidence"), "hints": decision.get("hints"),
            "email_decision": email["decision"] if email else None, "email_provider": email["provider"] if email else None,
            "catch_all": bool(email and email["decision"] == "accepted_catch_all"),
            "hook_summary": hook_summary, "body_words": word_count(c.body),
            "hook_quote": hook.get("quote") if hook else None, "hook_url": hook.get("source_url") if hook else None,
            "polish": polish_audit,
        },
        "state": "drafted" if not lint["blocks"] else "blocked",
        "delivery": existing.get("delivery") if existing else None,
        "delivery_ref": existing.get("delivery_ref") if existing else None,
    }
    store.save_draft(rec)
    if c.ok:
        store.record_phrase_usage(did, ctx.cid, domain, c.categories)
    if hook:
        store.db.execute("UPDATE hooks SET used_in_draft=? WHERE hook_id=?", (did, hook["hook_id"]))
    if lint["blocks"]:
        store.set_state(ctx.cid, p.prospect_id, "review", reason="lint: " + "; ".join(lint["blocks"]), resume_state="drafted")
    else:
        store.set_state(ctx.cid, p.prospect_id, "drafted", run_id=ctx.run_id)
    return rec


def _maybe_polish(ctx: Ctx, c: Composed, template: Template, existing: dict[str, Any] | None) -> tuple[Composed, dict[str, Any]]:
    """Stage 6 step 1: one model call to smooth grammar; guarded, and reused while the input is unchanged."""
    from .draft import polish

    key = short_hash(c.subject, c.body, length=12)
    prev = ((existing or {}).get("audit") or {}).get("polish") or {}
    if prev.get("input") == key:
        if prev.get("applied"):
            return _apply_polish(c, prev["subject"], prev["body"]), prev
        return c, prev
    if "polish" in ctx.disabled:
        return c, {"input": key, "applied": False, "reason": ctx.disabled["polish"]}
    try:
        ctx.ledger.require_llm_call()
    except QuotaError as e:
        return c, {"input": key, "applied": False, "reason": str(e)}
    try:
        r = polish(c.subject, c.body, template.max_words, _llm(ctx))
    except AuthError as e:
        ctx.disabled["polish"] = f"polish unavailable ({e})"
        ctx.report.notes.append(ctx.disabled["polish"])
        return c, {"input": key, "applied": False, "reason": ctx.disabled["polish"]}
    except (TransientError, DataError) as e:
        return c, {"input": key, "applied": False, "reason": str(e)}
    finally:
        ctx.ledger.record("claude", "polish", llm_calls=1)
    audit = {"input": key, "applied": r.applied, "reason": r.reason, "subject": r.subject, "body": r.body}
    if r.applied:
        ctx.report.counts["polished"] += 1
        return _apply_polish(c, r.subject, r.body), audit
    ctx.report.counts["polish_rejected"] += 1
    return c, audit


def _apply_polish(c: Composed, subject: str, body: str) -> Composed:
    from dataclasses import replace

    tail = c.full_body[len(c.body):] if c.full_body.startswith(c.body) else "\n\n" + c.full_body.split("\n\n", 1)[-1]
    return replace(c, subject=subject, body=body, full_body=body + tail)


def _local_midnight_utc_iso() -> str:
    midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return iso(midnight.astimezone(now_utc().tzinfo))


def stage_compose(ctx: Ctx) -> None:
    store, rep = ctx.store, ctx.report
    daily_cap = ctx.cfg["limits"]["daily_draft_cap"]
    made = 0
    for cp in store.in_state(ctx.cid, "researched"):
        if made >= ctx.max_drafts:
            rep.notes.append(f"max_drafts_per_run ({ctx.max_drafts}) reached; the rest wait for the next run")
            break
        if not ctx.dry_run and store.drafts_created_since(_local_midnight_utc_iso()) >= daily_cap:
            rep.notes.append(f"daily draft cap ({daily_cap}) reached; the rest wait until tomorrow")
            break
        p = store.get_prospect(cp["prospect_id"])
        assert p
        existing = store.get_draft(draft_id_for(ctx.cid, p.prospect_id))
        if existing and existing["state"] in ("delivered", "sent"):
            store.set_state(ctx.cid, p.prospect_id, existing["state"])  # idempotent: never draft the same person twice
            ctx.commit()
            continue
        try:
            rec = compose_one(ctx, p, existing)
        except DataError as e:
            store.set_state(ctx.cid, p.prospect_id, "review", reason=f"data: {e}", resume_state="researched")
            rep.needs_you["data problems"] += 1
            ctx.commit()
            continue
        made += 1
        if rec["state"] == "drafted":
            rep.counts["drafted"] += 1
        else:
            rep.counts["blocked"] += 1
            rep.needs_you["blocked drafts"] += 1
        ctx.log.write(run_id=ctx.run_id, stage="compose", prospect_id=p.prospect_id, status=rec["state"], template=rec["template_id"])
        ctx.commit()


# --------------------------------------------------------------------------- delivery
def stage_deliver(ctx: Ctx, delivery: str | None = None) -> list[dict[str, Any]]:
    name = "markdown" if ctx.dry_run else (delivery or ctx.campaign["delivery"])
    pending = [d for d in ctx.store.drafts(ctx.cid, ["drafted"]) if not d.get("delivery_ref")]
    if not pending:
        return []
    adapter = get_delivery(name, ctx.out_dir, ctx.cfg)

    def record(r: Any) -> None:
        ctx.store.set_draft_state(r.draft_id, r.state, delivery=name, ref=r.ref)
        d = ctx.store.get_draft(r.draft_id)
        if r.state == "delivered" and d:
            ctx.store.set_state(ctx.cid, d["prospect_id"], "delivered")
            ctx.report.counts["delivered"] += 1
        ctx.commit()   # the mailbox id is saved before the next draft is created: no duplicates after a crash

    results = []
    if hasattr(adapter, "deliver_one"):
        for d in pending:
            r = adapter.deliver_one(d)
            record(r)
            results.append(r)
    else:
        results = adapter.deliver(pending)
        for r in results:
            record(r)
    ctx.report.delivery = name
    if name == "outlook_draft":
        ctx.report.notes.append(f"{len(results)} drafts written to {ctx.out_dir / 'drafts.jsonl'}; create them in Outlook, then run `autonet deliver --ack`")
    if name != "markdown":
        write_review(ctx.out_dir / "review.md", f"Drafts · {ctx.run_id}", [ctx.store.get_draft(r.draft_id) for r in results])  # type: ignore[misc]
    return pending


def ack_deliveries(store: Store, ack_file: Path) -> int:
    n = 0
    for did, ref in read_acks(ack_file):
        d = store.get_draft(did)
        if not d or d.get("delivery_ref"):
            continue  # unknown, or already has a mailbox draft (idempotency)
        store.set_draft_state(did, "delivered", delivery="outlook_draft", ref=ref)
        store.set_state(d["campaign_id"], d["prospect_id"], "delivered")
        n += 1
    store.db.commit()
    return n


# --------------------------------------------------------------------------- run
STAGES = {
    "discover": stage_discover,
    "select": stage_select,
    "find-emails": stage_find_emails,
    "research": stage_research,
    "compose": stage_compose,
}


def finish(ctx: Ctx) -> str:
    ctx.report.credits = ctx.finder_spent
    if ctx.pool is not None:
        ctx.report.providers = ctx.pool.summaries() + [f"skipped {k}: {v}" for k, v in ctx.pool.skipped.items()]
    ctx.report.llm_calls = ctx.ledger.run_total("llm_calls")
    ctx.store.end_run(ctx.run_id, dict(ctx.report.counts), ctx.report.errors)
    text = ctx.report.render()
    ctx.out_dir.mkdir(parents=True, exist_ok=True)
    (ctx.out_dir / "report.txt").write_text(text + "\n")
    return text


def run_stages(ctx: Ctx, stages: list[str], deliver: bool = True) -> str:
    for name in stages:
        try:
            STAGES[name](ctx)
        except QuotaError as e:
            ctx.report.notes.append(f"{name} stopped: {e}")
            ctx.commit()
    if deliver:
        hold = delivery_holds(ctx)
        if hold:
            n = len(ctx.store.drafts(ctx.cid, ["drafted"]))
            ctx.report.notes.append(f"{n} drafts wait for you at drafted ({'; '.join(hold)})")
        else:
            stage_deliver(ctx)
    return finish(ctx)


def delivery_holds(ctx: Ctx) -> list[str]:
    """Why drafts must wait at `drafted` instead of going to the mailbox (empty = deliver)."""
    if ctx.dry_run:
        return []
    out = []
    if ctx.campaign["review_before_delivery"]:
        out.append("review_before_delivery: add notes, then /autonetworker deliver")
    if ctx.campaign.get("review_required"):
        out.append("review_required: approve each run in chat, then /autonetworker deliver")
    if (ctx.cfg.data.get("approvals") or {}).get("required", True):
        from .approvals import approval_status

        approved, changes = approval_status(ctx.cfg, ctx.store, ctx.campaign)
        if not approved:
            out.append("waiting for approval: " + "; ".join(changes) + f"; review the drafts, then `autonet approve {ctx.cid}` and deliver")
    return out


def run_campaign(cfg: Config, campaign_ref: str, *, dry_run: bool = False, limit: int | None = None,
                 db: Path | None = None, fresh: bool = False, debug: bool = False,
                 live_jev: bool = False, scheduled: bool = False) -> tuple[str, Ctx]:
    from .lock import run_lock
    from .retention import prune

    campaign = load_campaign(resolve_campaign_path(cfg.root, campaign_ref))
    store = open_store(cfg, dry_run, db, fresh)
    db_path = Path(str(store.path)) if str(store.path) != ":memory:" else cfg.path("db")
    with run_lock(db_path.with_suffix(".lock")):
        if scheduled:
            from .schedule import scheduled_gates

            gate = scheduled_gates(cfg, store, campaign)
            if not gate.ok:
                raise ConfigError(f"scheduled run of {campaign.id} skipped: " + " · ".join(gate.reasons))
        pruned = prune(store, cfg, cooldown_days=campaign.limits["recontact_cooldown_days"]) if not dry_run else None
        ctx = make_ctx(cfg, campaign, store, dry_run=dry_run, limit=limit, debug=debug, live_jev=live_jev)
        if pruned and pruned.total:
            ctx.report.notes.append(f"retention: removed {pruned.summary()}")
        text = run_stages(ctx, list(STAGES))
    return text, ctx


# --------------------------------------------------------------------------- review actions
def add_note(ctx: Ctx, draft_id: str, text: str) -> dict[str, Any]:
    text = text.strip()
    if not text:
        raise DataError("note is empty")
    if word_count(text) > NOTE_MAX_WORDS:
        raise DataError(f"note is {word_count(text)} words (max {NOTE_MAX_WORDS})")
    d = ctx.store.get_draft(draft_id)
    if not d:
        raise DataError(f"no draft {draft_id}")
    if d["state"] in ("delivered", "sent"):
        raise DataError(f"{draft_id} is already {d['state']}; edit it in your Drafts folder instead")
    hook = {
        "hook_id": "h_" + short_hash(draft_id, "note"), "prospect_id": d["prospect_id"], "tier": 0, "type": "note",
        "fields": {"text": text}, "quote": None, "source_url": None, "source_date": iso()[:10],
        "found_via": "user_note", "verified": True,
    }
    ctx.store.save_hook(hook)
    d["note"], d["hook_id"] = text, hook["hook_id"]
    ctx.store.save_draft(d)
    p = ctx.store.get_prospect(d["prospect_id"])
    assert p
    rec = compose_one(ctx, p, d)
    ctx.commit()
    return rec


def mark(store: Store, ident: str, state: str, outcome: str | None = None) -> list[str]:
    """mark-sent / mark-skip by draft id or prospect id. Returns the draft ids changed.
    `outcome` (none | light | heavy | not_sendable) feeds the 17-of-20 quality gate."""
    drafts = [store.get_draft(ident)] if ident.startswith("d_") else []
    if ident.startswith("p_"):
        drafts = [d for d in store.drafts() if d["prospect_id"] == ident]
    drafts = [d for d in drafts if d]
    changed = []
    for d in drafts:
        if state == "sent" and d["state"] != "delivered":
            raise DataError(f"{d['draft_id']} is {d['state']}, not delivered")
        store.set_draft_state(d["draft_id"], state if state == "sent" else "skipped")
        store.set_state(d["campaign_id"], d["prospect_id"], "sent" if state == "sent" else "skipped", reason="marked by you")
        if state == "sent":
            store.log_contact(d["prospect_id"], d["to_email"])
        if outcome:
            store.set_review_outcome(d["draft_id"], outcome)
        changed.append(d["draft_id"])
    if not drafts and ident.startswith("p_") and state == "skipped":
        store.db.execute("UPDATE campaign_prospects SET state='skipped', reason='marked by you' WHERE prospect_id=?", (ident,))
        changed.append(ident)
    if not changed:
        raise DataError(f"nothing found for {ident}")
    store.db.commit()
    return changed


def set_email(ctx: Ctx, pid: str, email: str) -> None:
    cp = ctx.store.get_cp(ctx.cid, pid)
    p = ctx.store.get_prospect(pid)
    if not cp or not p:
        raise DataError(f"{pid} is not in campaign {ctx.cid}")
    if "@" not in email:
        raise DataError(f"'{email}' doesn't look like an email address")
    ctx.store.save_lookup(*_lookup_key(p), pid, {"email": email, "score": None, "status": "user_supplied", "provider": "you", "decision": "manual"})
    resume = cp["resume_state"] if cp["state"] == "review" and cp["reason"] == "email_unconfirmed" else None
    ctx.store.set_state(ctx.cid, pid, resume or "email_found")
    ctx.commit()


def pick_template(ctx: Ctx, pid: str, template_id: str) -> None:
    if template_id not in ctx.campaign["templates"]["allowed"]:
        raise DataError(f"'{template_id}' is not in this campaign's allowed templates")
    p = ctx.store.get_prospect(pid)
    if not p or not ctx.store.get_cp(ctx.cid, pid):
        raise DataError(f"{pid} is not in campaign {ctx.cid}")
    from .selector import default_hints

    sc = selection_context(ctx, p)
    rec = {"template": template_id, "confidence": 1.0, "selector": "you", "reason": {"rule": "picked by you", "route": "continue"}, "hints": default_hints(sc)}
    chash = "manual_" + short_hash(iso())
    ctx.store.save_decision(ctx.cid, pid, chash, "manual", rec)
    cp = ctx.store.get_cp(ctx.cid, pid)
    state = cp["state"]
    # Back to the stage after selection, or rebuild an existing draft.
    if state in ("review", "qualified", "skipped"):
        ctx.store.set_state(ctx.cid, pid, "selected")
    elif state in ("drafted",):
        ctx.store.set_state(ctx.cid, pid, "researched")
    ctx.commit()

