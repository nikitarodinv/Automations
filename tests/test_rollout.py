"""Milestone 4: approval gates, scheduled runs, run lock, retention, quality gate, XML import, setup tools."""

from __future__ import annotations

import json
import os
import shutil
import stat
from datetime import timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from autonet import cli
from autonet.approvals import approval_status, approve
from autonet.campaign import load_campaign
from autonet.errors import ConfigError
from autonet.importer import import_phrases
from autonet.lock import run_lock
from autonet.pipeline import mark, open_store, run_campaign
from autonet.quality import quality_report
from autonet.retention import prune
from autonet.schedule import openclaw_command, scheduled_gates, validate_cron
from autonet.setup_tools import doctor, install_skill, new_campaign
from autonet.util import iso, now_utc
from tests.conftest import REPO

CID = "ib-alumni-fall26"


@pytest.fixture
def gated(cfg, tmp_path):
    """Approvals on, with the campaign's phrase bank copied to a temp file we can edit."""
    cfg.data["approvals"] = {"required": True}
    cfg.data["jev"]["provider"] = "off"
    bank = tmp_path / "bank.yaml"
    shutil.copy(REPO / "phrases/networking.yaml", bank)
    return cfg, bank


def csv_campaign(campaign_file, tmp_path, bank, **kw):
    csv = tmp_path / "c.csv"
    csv.write_text("First Name,Last Name,Email Address,Company,Position,Headline,Location,School\n"
                   "Jordan,Lee,jordan.lee@evercore.com,Evercore,Associate,Investment Banking Associate,\"New York, NY\",Your University\n")
    return campaign_file(source_options={"path": str(csv)}, phrase_bank=str(bank), **kw)


# --------------------------------------------------------------------------- approvals
def test_new_campaign_holds_drafts_until_approved(gated, campaign_file, tmp_path, monkeypatch):
    cfg, bank = gated
    path = csv_campaign(campaign_file, tmp_path, bank)
    text, ctx = run_campaign(cfg, path)
    assert [d["state"] for d in ctx.store.drafts(CID)] == ["drafted"]
    assert "waiting for approval: new campaign" in text
    # `deliver` refuses until approved
    monkeypatch.setattr(cli, "_cfg", lambda: cfg)
    r = CliRunner().invoke(cli.app, ["deliver", path])
    assert r.exit_code != 0 and "unapproved" in (r.output + str(r.exception))
    approve(cfg, ctx.store, ctx.campaign)
    r = CliRunner().invoke(cli.app, ["deliver", path])
    assert r.exit_code == 0, r.output
    assert [d["state"] for d in open_store(cfg, False).drafts(CID)] == ["delivered"]


def test_phrase_bank_change_needs_new_approval(gated, campaign_file, tmp_path):
    cfg, bank = gated
    path = csv_campaign(campaign_file, tmp_path, bank)
    camp = load_campaign(Path(path))
    store = open_store(cfg, False)
    approve(cfg, store, camp)
    assert approval_status(cfg, store, camp) == (True, [])
    bank.write_text(bank.read_text().replace("version: 1", "version: 2"))
    ok, changes = approval_status(cfg, store, camp)
    assert not ok and changes == ["phrase_bank changed"]
    text, ctx = run_campaign(cfg, path)
    assert "phrase_bank changed" in text and ctx.store.drafts(CID)[0]["state"] == "drafted"


def test_review_required_always_holds(gated, campaign_file, tmp_path):
    cfg, bank = gated
    path = csv_campaign(campaign_file, tmp_path, bank, review_required=True)
    approve(cfg, open_store(cfg, False), load_campaign(Path(path)))
    text, ctx = run_campaign(cfg, path)
    assert "review_required" in text and ctx.store.drafts(CID)[0]["state"] == "drafted"


def test_dry_runs_ignore_approvals(gated):
    cfg, _ = gated
    _, ctx = run_campaign(cfg, CID, dry_run=True)
    assert len(ctx.store.drafts(CID, ["delivered"])) == 10


# --------------------------------------------------------------------------- scheduled runs
def test_scheduled_run_gates(gated, campaign_file, tmp_path):
    cfg, bank = gated
    cfg.data["jev"]["provider"] = "typesafe"
    path = csv_campaign(campaign_file, tmp_path, bank)
    camp = load_campaign(Path(path))
    store = open_store(cfg, False)
    gate = scheduled_gates(cfg, store, camp)
    assert not gate.ok and any("eval jev" in r for r in gate.reasons) and any("never approved" in r for r in gate.reasons)
    store.save_eval("jev", "jev-1.13.0", 40, 40, 0.80, {})
    assert any("80%" in r for r in scheduled_gates(cfg, store, camp).reasons)
    store.save_eval("jev", "jev-1.13.0", 12, 12, 0.95, {})
    assert any("12 cases" in r for r in scheduled_gates(cfg, store, camp).reasons)
    store.save_eval("jev", "jev-1.13.0", 35, 35, 0.89, {})
    approve(cfg, store, camp)
    assert scheduled_gates(cfg, store, camp).ok
    store.save_eval("jev", "m", 5, 5, 1.0, {})            # a newer, too-small eval re-closes the gate
    with pytest.raises(ConfigError, match="scheduled run of .* skipped: the last Jev eval had 5 cases"):
        run_campaign(cfg, path, scheduled=True)
    cfg.data["scheduling"] = {"require_jev_eval": False}  # you can turn the Jev gate off explicitly
    _, ctx = run_campaign(cfg, path, scheduled=True)
    assert ctx.store.drafts(CID)[0]["state"] == "delivered"


def test_scheduled_cli_posts_report_then_no_reply(gated, campaign_file, tmp_path, monkeypatch):
    cfg, bank = gated
    path = csv_campaign(campaign_file, tmp_path, bank)
    approve(cfg, open_store(cfg, False), load_campaign(Path(path)))
    monkeypatch.setattr(cli, "_cfg", lambda: cfg)
    r1 = CliRunner().invoke(cli.app, ["run", path, "--scheduled"])
    assert r1.exit_code == 0 and "→ 1 drafts → 1 delivered" in r1.output
    r2 = CliRunner().invoke(cli.app, ["run", path, "--scheduled"])
    assert r2.output.strip() == "NO_REPLY"                         # OpenClaw posts nothing


def test_scheduled_skip_is_posted(gated, campaign_file, tmp_path, monkeypatch):
    cfg, bank = gated
    monkeypatch.setattr(cli, "_cfg", lambda: cfg)
    r = CliRunner().invoke(cli.app, ["run", csv_campaign(campaign_file, tmp_path, bank), "--scheduled"])
    assert r.exit_code == 1 and "skipped: not approved" in r.output


def test_openclaw_job_shapes(cfg, campaign_file):
    camp = load_campaign(Path(campaign_file(schedule="30 8 * * 1-5")))
    argv = openclaw_command(cfg, camp, channel="slack", to="channel:C1", tz="America/Chicago")
    assert argv[:4] == ["openclaw", "automations", "create", "30 8 * * 1-5"]
    run = json.loads(argv[argv.index("--command-argv") + 1])
    assert run[-3] == "run" and run[-2].endswith(".yaml") and run[-1] == "--scheduled" and "--project" in run
    std = load_campaign(REPO / "campaigns" / f"{CID}.yaml")
    std.data["schedule"] = "0 9 * * *"
    a = openclaw_command(cfg, std, channel=None, to=None, tz=None)
    assert json.loads(a[a.index("--command-argv") + 1])[-2] == CID     # standard location: run by id
    assert ["--announce", "--channel", "slack", "--to", "channel:C1"] == argv[-5:] and "--tz" in argv
    camp2 = load_campaign(Path(campaign_file(schedule="0 9 * * *", delivery="outlook_draft")))
    argv2 = openclaw_command(cfg, camp2, channel=None, to=None, tz=None)
    assert "--message" in argv2 and "--command-argv" not in argv2 and "isolated" in argv2


@pytest.mark.parametrize("expr, ok", [("30 8 * * 1-5", True), ("*/15 * * * *", True), ("0 9 1,15 * 0", True),
                                      ("0 25 * * *", False), ("every day", False), ("0 9 * *", False)])
def test_cron_validation(expr, ok):
    assert (validate_cron(expr) == []) is ok


def test_run_lock_blocks_overlap(cfg):
    lock = Path(cfg.data["paths"]["dry_run_db"]).with_suffix(".lock")
    with run_lock(lock):
        with pytest.raises(ConfigError, match="in progress"):
            run_campaign(cfg, CID, dry_run=True)
    _, ctx = run_campaign(cfg, CID, dry_run=True)     # released afterwards
    assert len(ctx.store.drafts(CID)) == 10


# --------------------------------------------------------------------------- retention
def test_retention_prunes_old_and_keeps_suppression(cfg):
    _, ctx = run_campaign(cfg, CID, dry_run=True)
    st = ctx.store
    old = iso(now_utc() - timedelta(days=400))
    st.db.execute("UPDATE prospects SET updated_at=?", (iso(now_utc() - timedelta(days=100)),))
    drafted = {d["prospect_id"] for d in st.drafts(CID)}
    st.db.execute("UPDATE drafts SET updated_at=? WHERE draft_id=?", (old, st.drafts(CID)[0]["draft_id"]))
    st.db.execute("UPDATE ledger SET at=?", (old,))
    st.suppress("gone@example.com", "email", "opt_out")
    st.db.commit()
    preview = prune(st, cfg, dry_run=True)
    assert preview.counts["undrafted prospects"] == 16 - len(drafted) and preview.counts["old drafts"] == 1
    r = prune(st, cfg)
    assert r.counts["old ledger rows"] > 0
    left = {row[0] for row in st.db.execute("SELECT prospect_id FROM prospects")}
    assert left == drafted
    assert len(st.drafts(CID)) == 9
    assert st.is_suppressed("x", "gone@example.com") == "opt_out"


# --------------------------------------------------------------------------- quality gate
def test_quality_gate(cfg):
    _, ctx = run_campaign(cfg, CID, dry_run=True)
    st = ctx.store
    ids = [d["draft_id"] for d in st.drafts(CID)]
    for i in ids[:8]:
        mark(st, i, "sent", "none")
    mark(st, ids[8], "sent", "heavy")
    mark(st, ids[9], "skipped", "not_sendable")
    q = quality_report(st)
    assert q["reviewed"] == 10 and q["sendable"] == 8 and q["status"] == "in progress"
    st.db.execute("UPDATE drafts SET review_outcome='not_sendable' WHERE draft_id IN (?,?)", (ids[0], ids[1]))
    assert quality_report(st)["status"] == "failed"     # 4 unsendable: can't reach 17 of 20 any more


def test_quality_passes_at_the_gate_ratio(cfg):
    _, ctx = run_campaign(cfg, CID, dry_run=True)
    st = ctx.store
    for i, d in enumerate(st.drafts(CID)):
        st.set_review_outcome(d["draft_id"], "light" if i < 9 else "heavy")
    st.db.commit()
    q = quality_report(st, first=10)        # 17-of-20 scaled to 10 needs 8
    assert (q["needed"], q["sendable"], q["status"]) == (8, 9, "passed")


# --------------------------------------------------------------------------- XML import
def test_import_phrases_from_xml(tmp_path):
    xml = tmp_path / "phrases.xml"
    xml.write_text("""<?xml version="1.0"?>
<phrases version="3">
  <category name="greeting">
    <phrase id="g1">Hi {{first_name}},</phrase>
    <phrase tone="formal">Dear {{first_name}},</phrase>
  </category>
  <opener.shared_school>
    <phrase weight="3" requires="school, company" seniority="entry junior">I'm at {{school}} and saw you're at {{company}} &amp; loving M&amp;A.</phrase>
    <phrase><tags><tone>warm</tone></tags>Fellow {{school}} alum here.</phrase>
  </opener.shared_school>
</phrases>""")
    out = tmp_path / "bank.yaml"
    bank = import_phrases(xml, out)
    g = bank["categories"]["greeting"]
    assert g[0]["id"] == "g1" and g[1]["tags"] == {"tone": ["formal"]} and g[1]["id"].startswith("gre")
    o = bank["categories"]["opener.shared_school"]
    assert o[0]["requires"] == ["school", "company"] and o[0]["weight"] == 3.0 and "M&A" in o[0]["text"]
    assert o[0]["tags"] == {"seniority": ["entry", "junior"]} and o[1]["tags"] == {"tone": ["warm"]}
    assert bank["version"] == 3 and "school" in bank["variables"]
    from autonet.compose import load_phrase_bank

    assert load_phrase_bank(out).version == 3
    with pytest.raises(ConfigError, match="exists"):
        import_phrases(xml, out)


def test_import_rejects_entities(tmp_path):
    xml = tmp_path / "evil.xml"
    xml.write_text('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><phrases><c><phrase>&a;</phrase></c></phrases>')
    with pytest.raises(ConfigError, match="DOCTYPE"):
        import_phrases(xml, tmp_path / "o.yaml")


# --------------------------------------------------------------------------- setup tools
def test_skill_install_fills_in_project_path(cfg, tmp_path):
    target, changed = install_skill(cfg, tmp_path / "skills" / "autonetworker")
    from autonet.setup_tools import _tilde

    text = target.read_text()
    assert changed and f"uv run --project {_tilde(cfg.root)} autonet" in text and "Never send email" in text
    assert install_skill(cfg, tmp_path / "skills" / "autonetworker") == (target, False)
    target.write_text("edited by hand")
    with pytest.raises(ConfigError, match="--force"):
        install_skill(cfg, tmp_path / "skills" / "autonetworker")
    assert install_skill(cfg, tmp_path / "skills" / "autonetworker", force=True)[1]


def test_campaign_new_is_valid(cfg, tmp_path, monkeypatch):
    cfg2 = type(cfg)(root=tmp_path, data=cfg.data)
    (tmp_path / "campaigns").mkdir()
    shutil.copy(REPO / "campaigns/ib-alumni-fall26.yaml", tmp_path / "campaigns")
    path = new_campaign(cfg2, "hk-alumni")
    camp = load_campaign(path)
    assert camp.id == "hk-alumni" and "TODO" in camp["description"] and not camp.get("schedule")
    with pytest.raises(ConfigError):
        new_campaign(cfg2, "hk-alumni")
    with pytest.raises(ConfigError):
        new_campaign(cfg2, "Bad Name")


def test_doctor_flags_loose_token_permissions(cfg, tmp_path):
    tok = tmp_path / "ms.json"
    tok.write_text("{}")
    os.chmod(tok, 0o644)
    cfg.data["mailbox"] = {"microsoft": {"client_id": "x", "token_cache": str(tok)}}
    checks = {c.what: c for c in doctor(cfg)}
    assert checks["mailbox microsoft"].level == "fail"
    assert checks["draft quality gate"].level == "warn"
