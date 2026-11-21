"""SQLite state store (section 10). One file, WAL mode, commit after every prospect."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterator

from .models import Prospect
from .util import iso, now_utc, parse_iso

# Main path, in order. Each stage picks up prospects in its input state.
MAIN_PATH = ["discovered", "qualified", "selected", "email_found", "researched", "drafted", "delivered", "sent"]
# Waiting state: no finder credits left today/this month; Stage 3 retries it next run.
WAITING = ["pending_email"]
# Exits: review returns to the main path once you act; skipped can be re-qualified; suppressed is permanent.
EXITS = ["review", "skipped", "suppressed"]
STATES = MAIN_PATH + WAITING + EXITS

SCHEMA = """
CREATE TABLE IF NOT EXISTS prospects (
  prospect_id TEXT PRIMARY KEY,
  record      TEXT NOT NULL,
  source      TEXT NOT NULL,
  first_seen  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS campaign_prospects (
  campaign_id   TEXT NOT NULL,
  prospect_id   TEXT NOT NULL REFERENCES prospects(prospect_id),
  state         TEXT NOT NULL,
  score         INTEGER,
  reason        TEXT,
  resume_state  TEXT,
  last_run      TEXT,
  updated_at    TEXT NOT NULL,
  PRIMARY KEY (campaign_id, prospect_id)
);
CREATE INDEX IF NOT EXISTS cp_state ON campaign_prospects(campaign_id, state);
CREATE TABLE IF NOT EXISTS template_decisions (
  prospect_id  TEXT NOT NULL,
  catalog_hash TEXT NOT NULL,
  jev_version  TEXT NOT NULL,
  campaign_id  TEXT NOT NULL,
  template     TEXT,
  confidence   REAL,
  reason       TEXT,
  selector     TEXT NOT NULL,
  hints        TEXT,
  decided_at   TEXT NOT NULL,
  PRIMARY KEY (prospect_id, catalog_hash, jev_version)
);
CREATE TABLE IF NOT EXISTS email_lookups (
  first_name   TEXT NOT NULL,
  last_name    TEXT NOT NULL,
  domain       TEXT NOT NULL,
  prospect_id  TEXT,
  email        TEXT,
  score        INTEGER,
  status       TEXT,
  accept_all   INTEGER,
  sources      TEXT,
  provider     TEXT,
  decision     TEXT,
  checked_at   TEXT NOT NULL,
  credits_used REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (first_name, last_name, domain)
);
CREATE TABLE IF NOT EXISTS hooks (
  hook_id      TEXT PRIMARY KEY,
  prospect_id  TEXT NOT NULL,
  tier         INTEGER NOT NULL,
  type         TEXT NOT NULL,
  fields       TEXT NOT NULL,
  quote        TEXT,
  source_url   TEXT,
  source_date  TEXT,
  found_via    TEXT,
  verified     INTEGER NOT NULL DEFAULT 0,
  used_in_draft TEXT,
  created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts (
  draft_id         TEXT PRIMARY KEY,
  campaign_id      TEXT NOT NULL,
  prospect_id      TEXT NOT NULL,
  run_id           TEXT,
  template_id      TEXT NOT NULL,
  template_version INTEGER NOT NULL,
  phrase_bank_version INTEGER NOT NULL,
  phrase_ids       TEXT NOT NULL,
  hook_id          TEXT,
  note             TEXT,
  to_email         TEXT,
  email_score      INTEGER,
  subject          TEXT,
  body             TEXT,
  lint             TEXT,
  audit            TEXT,
  state            TEXT NOT NULL,
  delivery         TEXT,
  delivery_ref     TEXT,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS drafts_campaign ON drafts(campaign_id, state);
CREATE TABLE IF NOT EXISTS phrase_usage (
  phrase_id      TEXT NOT NULL,
  draft_id       TEXT NOT NULL,
  campaign_id    TEXT NOT NULL,
  category       TEXT NOT NULL,
  company_domain TEXT,
  used_at        TEXT NOT NULL,
  PRIMARY KEY (phrase_id, draft_id)
);
CREATE TABLE IF NOT EXISTS contact_log (
  prospect_id  TEXT PRIMARY KEY,
  email        TEXT,
  last_sent_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS suppression (
  key       TEXT PRIMARY KEY,
  kind      TEXT NOT NULL,
  reason    TEXT NOT NULL,
  added_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  run_id      TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL,
  started_at  TEXT NOT NULL,
  ended_at    TEXT,
  dry_run     INTEGER NOT NULL DEFAULT 0,
  counts      TEXT,
  errors      TEXT
);
CREATE TABLE IF NOT EXISTS source_cursors (
  campaign_id TEXT NOT NULL,
  provider    TEXT NOT NULL,
  key         TEXT NOT NULL,
  cursor      TEXT,
  updated_at  TEXT NOT NULL,
  PRIMARY KEY (campaign_id, provider, key)
);
CREATE TABLE IF NOT EXISTS ledger (
  run_id    TEXT NOT NULL,
  seq       INTEGER NOT NULL,
  provider  TEXT NOT NULL,
  endpoint  TEXT NOT NULL,
  credits   REAL NOT NULL DEFAULT 0,
  searches  INTEGER NOT NULL DEFAULT 0,
  tokens    INTEGER NOT NULL DEFAULT 0,
  llm_calls INTEGER NOT NULL DEFAULT 0,
  http_status INTEGER,
  at        TEXT NOT NULL,
  PRIMARY KEY (run_id, seq)
);
"""


def _j(v: Any) -> str | None:
    return None if v is None else json.dumps(v, ensure_ascii=False, sort_keys=True)


def _uj(v: str | None) -> Any:
    return None if v is None else json.loads(v)


def merge_records(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Pool rule 4: each field from whichever source has it, the newest value winning."""
    out = dict(old)
    for k, v in new.items():
        if v in (None, "", [], {}):
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge_records(out[k], v)
        else:
            out[k] = v
    return out


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path) if str(path) != ":memory:" else path
        if isinstance(self.path, Path):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.db
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    # ---------- prospects ----------
    def upsert_prospect(self, p: Prospect) -> Prospect:
        now = iso()
        row = self.db.execute("SELECT record FROM prospects WHERE prospect_id=?", (p.prospect_id,)).fetchone()
        rec = p.to_dict()
        if row:
            rec = merge_records(json.loads(row["record"]), rec)
            self.db.execute(
                "UPDATE prospects SET record=?, source=?, updated_at=? WHERE prospect_id=?",
                (_j(rec), p.source, now, p.prospect_id),
            )
        else:
            self.db.execute(
                "INSERT INTO prospects VALUES (?,?,?,?,?)", (p.prospect_id, _j(rec), p.source, now, now)
            )
        return Prospect.from_dict(rec)

    def get_prospect(self, pid: str) -> Prospect | None:
        row = self.db.execute("SELECT record FROM prospects WHERE prospect_id=?", (pid,)).fetchone()
        return Prospect.from_dict(json.loads(row["record"])) if row else None

    # ---------- campaign state ----------
    def get_cp(self, campaign_id: str, pid: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM campaign_prospects WHERE campaign_id=? AND prospect_id=?", (campaign_id, pid)
        ).fetchone()

    def set_state(
        self,
        campaign_id: str,
        pid: str,
        state: str,
        *,
        reason: str | None = None,
        resume_state: str | None = None,
        score: int | None = None,
        run_id: str | None = None,
    ) -> None:
        assert state in STATES, state
        now = iso()
        cur = self.get_cp(campaign_id, pid)
        if cur is None:
            self.db.execute(
                "INSERT INTO campaign_prospects VALUES (?,?,?,?,?,?,?,?)",
                (campaign_id, pid, state, score, reason, resume_state, run_id, now),
            )
        else:
            self.db.execute(
                """UPDATE campaign_prospects SET state=?, reason=?, resume_state=?,
                   score=COALESCE(?, score), last_run=COALESCE(?, last_run), updated_at=?
                   WHERE campaign_id=? AND prospect_id=?""",
                (state, reason, resume_state, score, run_id, now, campaign_id, pid),
            )

    def in_state(self, campaign_id: str, state: str) -> list[sqlite3.Row]:
        return self.db.execute(
            """SELECT * FROM campaign_prospects WHERE campaign_id=? AND state=?
               ORDER BY COALESCE(score, 0) DESC, prospect_id""",
            (campaign_id, state),
        ).fetchall()

    def state_counts(self, campaign_id: str) -> dict[str, int]:
        rows = self.db.execute(
            "SELECT state, COUNT(*) n FROM campaign_prospects WHERE campaign_id=? GROUP BY state", (campaign_id,)
        ).fetchall()
        return {r["state"]: r["n"] for r in rows}

    def campaign_prospects(self, campaign_id: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM campaign_prospects WHERE campaign_id=?", (campaign_id,)
        ).fetchall()

    # ---------- suppression & cooldown ----------
    def suppress(self, key: str, kind: str, reason: str) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO suppression VALUES (?,?,?,?)", (key.lower(), kind, reason, iso())
        )
        if kind == "prospect":
            self.db.execute(
                "UPDATE campaign_prospects SET state='suppressed', reason=?, updated_at=? WHERE prospect_id=?",
                (reason, iso(), key),
            )

    def is_suppressed(self, pid: str, email: str | None = None) -> str | None:
        keys = [pid.lower()] + ([email.lower()] if email else [])
        row = self.db.execute(
            f"SELECT reason FROM suppression WHERE key IN ({','.join('?' * len(keys))})", keys
        ).fetchone()
        return row["reason"] if row else None

    def in_cooldown(self, pid: str, days: int) -> bool:
        row = self.db.execute("SELECT last_sent_at FROM contact_log WHERE prospect_id=?", (pid,)).fetchone()
        if not row:
            return False
        return parse_iso(row["last_sent_at"]) > now_utc() - timedelta(days=days)

    def log_contact(self, pid: str, email: str | None) -> None:
        self.db.execute(
            """INSERT INTO contact_log VALUES (?,?,?)
               ON CONFLICT(prospect_id) DO UPDATE SET email=excluded.email, last_sent_at=excluded.last_sent_at""",
            (pid, email, iso()),
        )

    # ---------- template decisions ----------
    def get_decision(self, pid: str, catalog_hash: str, version: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM template_decisions WHERE prospect_id=? AND catalog_hash=? AND jev_version=?",
            (pid, catalog_hash, version),
        ).fetchone()
        return self._decision(row)

    def cached_decision(self, pid: str, catalog_hash: str, version: str | None = None) -> dict[str, Any] | None:
        q = "SELECT * FROM template_decisions WHERE prospect_id=? AND catalog_hash=?"
        args: list[Any] = [pid, catalog_hash]
        if version:
            q += " AND jev_version=?"
            args.append(version)
        return self._decision(self.db.execute(q + " ORDER BY decided_at DESC, rowid DESC LIMIT 1", args).fetchone())

    def latest_decision(self, campaign_id: str, pid: str) -> dict[str, Any] | None:
        row = self.db.execute(
            """SELECT * FROM template_decisions WHERE campaign_id=? AND prospect_id=?
               ORDER BY decided_at DESC, rowid DESC LIMIT 1""",
            (campaign_id, pid),
        ).fetchone()
        return self._decision(row)

    @staticmethod
    def _decision(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if not row:
            return None
        d = dict(row)
        d["reason"] = _uj(d["reason"])
        d["hints"] = _uj(d["hints"]) or {}
        return d

    def save_decision(self, campaign_id: str, pid: str, catalog_hash: str, version: str, d: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO template_decisions VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                pid, catalog_hash, version, campaign_id, d.get("template"), d.get("confidence"),
                _j(d.get("reason")), d["selector"], _j(d.get("hints", {})), iso(),
            ),
        )

    # ---------- email lookups ----------
    def get_lookup(self, first: str, last: str, domain: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM email_lookups WHERE first_name=? AND last_name=? AND domain=?",
            (first.lower(), last.lower(), domain.lower()),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["sources"] = _uj(d["sources"]) or []
        return d

    def save_lookup(self, first: str, last: str, domain: str, pid: str, rec: dict[str, Any]) -> None:
        self.db.execute(
            """INSERT OR REPLACE INTO email_lookups VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                first.lower(), last.lower(), domain.lower(), pid, rec.get("email"), rec.get("score"),
                rec.get("status"), int(bool(rec.get("accept_all"))), _j(rec.get("sources", [])),
                rec.get("provider"), rec.get("decision"), rec.get("checked_at") or iso(),
                rec.get("credits_used", 0),
            ),
        )

    def accepted_email(self, p: Prospect) -> dict[str, Any] | None:
        lk = self.get_lookup(p.first_name, p.last_name, p.company.domain or p.company.normalized)
        if lk and lk["decision"] in ("accepted", "accepted_catch_all", "confirmed", "manual"):
            return lk
        return None

    # ---------- hooks ----------
    def save_hook(self, h: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO hooks VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                h["hook_id"], h["prospect_id"], h["tier"], h["type"], _j(h["fields"]), h.get("quote"),
                h.get("source_url"), h.get("source_date"), h.get("found_via", ""), int(h.get("verified", False)),
                h.get("used_in_draft"), iso(),
            ),
        )

    def get_hook(self, hook_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM hooks WHERE hook_id=?", (hook_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["fields"] = json.loads(d["fields"])
        d["verified"] = bool(d["verified"])
        return d

    def hooks_for(self, pid: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT hook_id FROM hooks WHERE prospect_id=? AND verified=1 ORDER BY tier, source_date DESC",
            (pid,),
        ).fetchall()
        return [self.get_hook(r["hook_id"]) for r in rows]  # type: ignore[misc]

    # ---------- drafts ----------
    def save_draft(self, d: dict[str, Any]) -> None:
        now = iso()
        existing = self.get_draft(d["draft_id"])
        created = existing["created_at"] if existing else now
        self.db.execute(
            "INSERT OR REPLACE INTO drafts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                d["draft_id"], d["campaign_id"], d["prospect_id"], d.get("run_id"), d["template_id"],
                d["template_version"], d["phrase_bank_version"], _j(d["phrase_ids"]), d.get("hook_id"),
                d.get("note"), d.get("to_email"), d.get("email_score"), d.get("subject"), d.get("body"),
                _j(d.get("lint", {})), _j(d.get("audit", {})), d["state"], d.get("delivery"),
                d.get("delivery_ref"), created, now,
            ),
        )

    def get_draft(self, draft_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM drafts WHERE draft_id=?", (draft_id,)).fetchone()
        return self._draft(row)

    @staticmethod
    def _draft(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if not row:
            return None
        d = dict(row)
        for k in ("phrase_ids", "lint", "audit"):
            d[k] = _uj(d[k])
        return d

    def drafts(self, campaign_id: str | None = None, states: list[str] | None = None) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM drafts WHERE 1=1", []
        if campaign_id:
            q += " AND campaign_id=?"
            args.append(campaign_id)
        if states:
            q += f" AND state IN ({','.join('?' * len(states))})"
            args += states
        q += " ORDER BY created_at, rowid"
        return [self._draft(r) for r in self.db.execute(q, args).fetchall()]  # type: ignore[misc]

    def set_draft_state(self, draft_id: str, state: str, delivery: str | None = None, ref: str | None = None) -> None:
        self.db.execute(
            """UPDATE drafts SET state=?, delivery=COALESCE(?, delivery), delivery_ref=COALESCE(?, delivery_ref),
               updated_at=? WHERE draft_id=?""",
            (state, delivery, ref, iso(), draft_id),
        )

    def drafts_created_since(self, since_iso: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM drafts WHERE created_at>=?", (since_iso,)).fetchone()[0]

    def recent_openers(self, exclude_draft: str, n: int = 3) -> list[str]:
        rows = self.db.execute(
            """SELECT pu.phrase_id FROM phrase_usage pu JOIN drafts d ON d.draft_id = pu.draft_id
               WHERE pu.category LIKE 'opener%' AND pu.draft_id != ?
               ORDER BY d.created_at DESC, d.draft_id DESC LIMIT ?""",
            (exclude_draft, n),
        ).fetchall()
        return [r["phrase_id"] for r in rows]

    # ---------- phrase usage ----------
    def phrases_used_at_company(self, campaign_id: str, domain: str, exclude_draft: str) -> set[str]:
        rows = self.db.execute(
            "SELECT phrase_id FROM phrase_usage WHERE campaign_id=? AND company_domain=? AND draft_id!=?",
            (campaign_id, domain, exclude_draft),
        ).fetchall()
        return {r["phrase_id"] for r in rows}

    def record_phrase_usage(self, draft_id: str, campaign_id: str, domain: str, used: dict[str, str]) -> None:
        self.db.execute("DELETE FROM phrase_usage WHERE draft_id=?", (draft_id,))
        now = iso()
        for category, pid in used.items():
            self.db.execute(
                "INSERT OR REPLACE INTO phrase_usage VALUES (?,?,?,?,?,?)",
                (pid, draft_id, campaign_id, category, domain, now),
            )

    # ---------- source cursors (paging continues across runs) ----------
    def get_cursor(self, campaign_id: str, provider: str, key: str) -> str | None:
        row = self.db.execute(
            "SELECT cursor FROM source_cursors WHERE campaign_id=? AND provider=? AND key=?", (campaign_id, provider, key)
        ).fetchone()
        return row["cursor"] if row else None

    def set_cursor(self, campaign_id: str, provider: str, key: str, value: str | None) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO source_cursors VALUES (?,?,?,?,?)", (campaign_id, provider, key, value, iso())
        )
        self.db.commit()

    # ---------- runs ----------
    def start_run(self, run_id: str, campaign_id: str, dry_run: bool) -> None:
        self.db.execute(
            "INSERT INTO runs (run_id, campaign_id, started_at, dry_run) VALUES (?,?,?,?)",
            (run_id, campaign_id, iso(), int(dry_run)),
        )
        self.db.commit()

    def run_exists(self, run_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone() is not None

    def end_run(self, run_id: str, counts: dict[str, Any], errors: list[str]) -> None:
        self.db.execute(
            "UPDATE runs SET ended_at=?, counts=?, errors=? WHERE run_id=?",
            (iso(), _j(counts), _j(errors), run_id),
        )
        self.db.commit()

    def latest_run(self, campaign_id: str | None = None) -> str | None:
        q = "SELECT run_id FROM runs"
        args: list[Any] = []
        if campaign_id:
            q += " WHERE campaign_id=?"
            args.append(campaign_id)
        row = self.db.execute(q + " ORDER BY started_at DESC, rowid DESC LIMIT 1", args).fetchone()
        return row["run_id"] if row else None
