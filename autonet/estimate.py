"""`autonet run --estimate`: the worst case before spending anything (section 11)."""

from __future__ import annotations

from pathlib import Path

from .campaign import load_campaign, resolve_campaign_path
from .config import Config
from .ledger import Ledger, RunBudget
from .pipeline import open_store
from .pool import Pool


def estimate_text(cfg: Config, campaign_ref: str, limit: int | None = None, db: Path | None = None) -> str:
    camp = load_campaign(resolve_campaign_path(cfg.root, campaign_ref))
    lim = camp.limits
    prospects = lim["max_prospects_per_run"]
    drafts = min(limit or lim["max_drafts_per_run"], lim["max_drafts_per_run"])
    research = bool(camp["research"].get("enabled"))
    searches = 3 * prospects if research else 0
    finder = prospects * 1.5
    source_names = [e["name"] for e in (cfg.data.get("providers") or {}).get("sources") or []] if camp["source"] == "pool" else [camp["source"]]
    enrich = {n: prospects for n in source_names if n in ("pdl", "apollo", "urls")}
    jev_on = (cfg.data.get("jev") or {}).get("provider", "off") not in (None, False, "off")
    model_calls = (prospects if jev_on else 0) + (prospects if research else 0)

    store = open_store(cfg, False, db)
    pool = Pool(cfg, store, Ledger(store, "estimate", RunBudget(0, 0, 0)))
    lines = [
        f"Estimate · {camp.id} (worst case; nothing is spent)",
        f"People: up to {prospects} from {', '.join(source_names)}; drafts capped at {drafts}",
        f"Finder credits: up to {finder:g} ({prospects} × (1 find + 0.5 verify)), capped by hunter_credit_budget {lim['hunter_credit_budget']:g}"
        f" and by the {drafts} draft slots",
    ]
    for n, c in enrich.items():
        lines.append(f"Source credits: {n} up to {c} ({'1 per record' if n == 'pdl' else '1 per person enriched'})")
    lines.append(f"Web searches: {searches} (research {'on' if research else 'off'}), budget {lim['search_budget']}")
    lines.append(f"Model calls: up to {model_calls} (Jev {'on' if jev_on else 'off'}), budget {lim['llm_call_budget']}")
    accounts = {"hunter"} | set(enrich) | {e["name"] for e in (cfg.data.get("providers") or {}).get("finders") or []}
    for a in sorted(accounts - {"urls", "csv"}):
        al = pool.allowance(a)
        lines.append(f"  {al.summary()} · today's pace allows {al.available_today():g}")
    return "\n".join(lines)
