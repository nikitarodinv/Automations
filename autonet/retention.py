"""Retention (section 10): prospects never drafted go after 90 days; drafts, decisions, lookups,
hooks, the ledger, run records, logs and out/ folders after `retention_days` (default 365).
Suppression entries are kept for good. Contact history is kept as long as any cooldown needs it."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from .store import Store
from .util import iso, now_utc

UNDRAFTED_DAYS = 90


@dataclass
class PruneResult:
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def summary(self) -> str:
        return ", ".join(f"{n} {k}" for k, n in self.counts.items() if n) or "nothing to prune"


def prune(store: Store, cfg: Any, *, dry_run: bool = False, cooldown_days: int = 180) -> PruneResult:
    keep_days = int(cfg.data.get("retention_days", 365))
    old = iso(now_utc() - timedelta(days=keep_days))
    undrafted = iso(now_utc() - timedelta(days=UNDRAFTED_DAYS))
    db = store.db
    res = PruneResult()

    stale = [r[0] for r in db.execute(
        """SELECT p.prospect_id FROM prospects p
           WHERE p.updated_at < ?
             AND NOT EXISTS (SELECT 1 FROM drafts d WHERE d.prospect_id = p.prospect_id)
             AND NOT EXISTS (SELECT 1 FROM contact_log c WHERE c.prospect_id = p.prospect_id)""", (undrafted,))]
    old_drafts = [r[0] for r in db.execute("SELECT draft_id FROM drafts WHERE updated_at < ?", (old,))]
    res.counts = {
        "undrafted prospects": len(stale),
        "old drafts": len(old_drafts),
        "old decisions": db.execute("SELECT COUNT(*) FROM template_decisions WHERE decided_at < ?", (old,)).fetchone()[0],
        "old email lookups": db.execute("SELECT COUNT(*) FROM email_lookups WHERE checked_at < ?", (old,)).fetchone()[0],
        "old ledger rows": db.execute("SELECT COUNT(*) FROM ledger WHERE at < ?", (old,)).fetchone()[0],
    }
    if dry_run:
        return res
    for pid in stale:
        for table in ("campaign_prospects", "template_decisions", "hooks", "email_lookups"):
            db.execute(f"DELETE FROM {table} WHERE prospect_id=?", (pid,))
        db.execute("DELETE FROM prospects WHERE prospect_id=?", (pid,))
    for did in old_drafts:
        db.execute("DELETE FROM phrase_usage WHERE draft_id=?", (did,))
        db.execute("DELETE FROM drafts WHERE draft_id=?", (did,))
    db.execute("DELETE FROM template_decisions WHERE decided_at < ?", (old,))
    db.execute("DELETE FROM email_lookups WHERE checked_at < ?", (old,))
    db.execute("DELETE FROM hooks WHERE created_at < ?", (old,))
    db.execute("DELETE FROM ledger WHERE at < ?", (old,))
    db.execute("DELETE FROM runs WHERE started_at < ?", (old,))
    contact_cut = iso(now_utc() - timedelta(days=max(keep_days, cooldown_days)))
    db.execute("DELETE FROM contact_log WHERE last_sent_at < ?", (contact_cut,))
    db.commit()
    res.counts["old files"] = _prune_files(cfg, keep_days)
    return res


def _prune_files(cfg: Any, keep_days: int) -> int:
    import time

    cutoff = time.time() - keep_days * 86400
    n = 0
    logs: Path = cfg.path("logs")
    if logs.is_dir():
        for f in logs.glob("autonet-*.jsonl"):
            if f.stat().st_mtime < cutoff:
                f.unlink()
                n += 1
    out: Path = cfg.path("out")
    if out.is_dir():
        for d in out.iterdir():
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d)
                n += 1
    return n
