"""End-to-end runs against fixtures. Maps to the milestone-1 items of the acceptance checklist."""

from __future__ import annotations

import json
from email import message_from_bytes
from pathlib import Path

import pytest

from autonet import pipeline
from autonet.errors import ConfigError, DataError
from autonet.pipeline import add_note, ack_deliveries, make_ctx, mark, open_store, run_campaign, run_stages, set_email
from autonet.campaign import load_campaign
from tests.conftest import CAMPAIGN

CID = "ib-alumni-fall26"


def drafts(ctx, states=None):
    return ctx.store.drafts(ctx.cid, states)


def test_dry_run_makes_10_drafts_without_network(cfg, monkeypatch):
    import httpx

    def no_network(*a, **kw):
        raise AssertionError("network call during dry run")

    monkeypatch.setattr(httpx.Client, "send", no_network)
    text, ctx = run_campaign(cfg, CID, dry_run=True)
    ds = drafts(ctx)
    assert len(ds) == 10, text
    assert all(d["state"] == "delivered" for d in ds)
    assert "Discovered 16 → qualified 13" in text
    assert (ctx.out_dir / "review.md").is_file()
    states = ctx.store.state_counts(CID)
    assert states == {"delivered": 10, "discovered": 3, "review": 1, "skipped": 1, "suppressed": 1}


def test_rerun_creates_no_new_drafts(cfg):
    run_campaign(cfg, CID, dry_run=True)
    text, ctx = run_campaign(cfg, CID, dry_run=True)
    assert "→ 0 drafts" in text
    assert len(drafts(ctx)) == 10


def test_every_draft_is_traceable(cfg):
    _, ctx = run_campaign(cfg, CID, dry_run=True)
    for d in drafts(ctx):
        assert d["template_id"] and d["template_version"] >= 1 and d["phrase_bank_version"] >= 1
        assert {"subject", "greeting", "opener", "ask", "close"} <= set(d["phrase_ids"])
        assert d["email_score"] is not None and d["to_email"]
        assert d["audit"]["selector"] == "rules" and d["audit"]["selector_reason"]
        dec = ctx.store.latest_decision(CID, d["prospect_id"])
        assert dec["selector"] == "rules"


def test_no_hook_still_drafts_with_profile_opener(cfg):
    _, ctx = run_campaign(cfg, CID, dry_run=True)
    assert all(d["hook_id"] is None for d in drafts(ctx))
    assert all("hook" not in d["phrase_ids"] for d in drafts(ctx))


def test_colleagues_get_different_phrases(cfg):
    _, ctx = run_campaign(cfg, CID, dry_run=True)
    by_co = {}
    for d in drafts(ctx):
        by_co.setdefault(d["audit"]["company"], []).append(d)
    evercore = by_co["Evercore"]
    assert len(evercore) == 2
    a, b = (set(d["phrase_ids"].items()) for d in evercore)
    shared = {k for k, v in a & b if k not in ("greeting",)}
    assert not shared, shared


def test_credit_budget_stops_stage_3(cfg, campaign_file):
    path = campaign_file(limits__hunter_credit_budget=3)
    text, ctx = run_campaign(cfg, path, dry_run=True)
    assert ctx.ledger.run_total("credits") <= 3
    assert "run credit budget reached" in text
    assert ctx.store.state_counts(CID).get("pending_email", 0) > 0
    # the next run picks up where this one stopped
    text2, ctx2 = run_campaign(cfg, path, dry_run=True)
    assert ctx2.ledger.run_total("credits") <= 3


def test_claimed_email_is_suppressed_for_good(cfg):
    _, ctx = run_campaign(cfg, CID, dry_run=True)
    ben = [r for r in ctx.store.campaign_prospects(CID) if ctx.store.get_prospect(r["prospect_id"]).first_name == "Ben"][0]
    assert ben["state"] == "suppressed"
    assert ctx.store.is_suppressed(ben["prospect_id"]) == "claimed_email"
    # even if someone resets its state, discovery refuses it again
    ctx.store.db.execute("DELETE FROM campaign_prospects WHERE prospect_id=?", (ben["prospect_id"],))
    ctx.store.db.commit()
    _, ctx2 = run_campaign(cfg, CID, dry_run=True)
    assert ctx2.store.get_cp(CID, ben["prospect_id"])["state"] == "suppressed"
    assert not [d for d in drafts(ctx2) if d["prospect_id"] == ben["prospect_id"]]


def test_note_appears_exactly_as_typed(cfg):
    # Stop at drafted (what review_before_delivery does), add a note, then deliver.
    ctx = make_ctx(cfg, load_campaign(CAMPAIGN), open_store(cfg, dry_run=True), dry_run=True)
    run_stages(ctx, list(pipeline.STAGES), deliver=False)
    assert all(d["state"] == "drafted" for d in drafts(ctx))
    d = drafts(ctx)[0]
    note = "I enjoyed your post about the growth of private credit last week."
    rec = add_note(ctx, d["draft_id"], note)
    assert note in rec["body"]
    assert rec["note"] == note and rec["hook_id"]
    assert rec["draft_id"] == d["draft_id"]  # same idempotency key
    pipeline.stage_deliver(ctx)
    delivered = ctx.store.get_draft(d["draft_id"])
    assert delivered["state"] == "delivered" and note in delivered["body"]
    with pytest.raises(DataError):
        add_note(ctx, d["draft_id"], "too late")
    with pytest.raises(DataError):
        add_note(ctx, drafts(ctx)[1]["draft_id"], "word " * 41)


def test_review_before_delivery_stops_at_drafted(cfg, campaign_file, tmp_path):
    csv = tmp_path / "contacts.csv"
    csv.write_text(
        "First Name,Last Name,URL,Email Address,Company,Position,Headline,Location,School\n"
        "Jordan,Lee,https://www.linkedin.com/in/j-lee,jordan.lee@evercore.com,Evercore,Associate,Investment Banking Associate,\"New York, NY\",Your University\n"
    )
    path = campaign_file(review_before_delivery=True, source_options={"path": str(csv)})
    text, ctx = run_campaign(cfg, path)
    assert [d["state"] for d in drafts(ctx)] == ["drafted"]
    assert "review_before_delivery" in text


def test_real_run_from_csv_writes_eml_drafts(cfg, campaign_file, tmp_path):
    csv = tmp_path / "contacts.csv"
    csv.write_text(
        "First Name,Last Name,URL,Email Address,Company,Position,Headline,Location,School\n"
        "Jordan,Lee,https://www.linkedin.com/in/j-lee,jordan.lee@evercore.com,Evercore,Associate,Investment Banking Associate,\"New York, NY\",Your University\n"
        "Sam,Rivera,https://www.linkedin.com/in/s-rivera,,Lazard,Analyst,M&A Analyst,\"New York, NY\",Other College\n"
    )
    path = campaign_file(source_options={"path": str(csv)})
    text, ctx = run_campaign(cfg, path)
    ds = drafts(ctx)
    assert len(ds) == 1 and ds[0]["state"] == "delivered"
    msg = message_from_bytes(Path(ds[0]["delivery_ref"]).read_bytes())
    assert msg["X-Unsent"] == "1" and msg["To"] == "jordan.lee@evercore.com"
    assert "Jordan" in msg.get_payload()
    # Sam has no address and there is no finder in milestone 1: waits rather than being dropped
    assert ctx.store.state_counts(CID)["pending_email"] == 1
    assert "1 waiting" in text


def test_outlook_draft_ack_is_idempotent(cfg, campaign_file, tmp_path):
    csv = tmp_path / "contacts.csv"
    csv.write_text(
        "First Name,Last Name,Email Address,Company,Position,Headline,Location,School\n"
        "Jordan,Lee,jordan.lee@evercore.com,Evercore,Associate,Investment Banking Associate,\"New York, NY\",Your University\n"
    )
    path = campaign_file(delivery="outlook_draft", source_options={"path": str(csv)})
    _, ctx = run_campaign(cfg, path)
    line = json.loads((ctx.out_dir / "drafts.jsonl").read_text().splitlines()[0])
    assert drafts(ctx)[0]["state"] == "drafted"
    acks = tmp_path / "acks.jsonl"
    acks.write_text(json.dumps({"draft_id": line["draft_id"], "mailbox_draft_id": "AAMk-1"}) + "\n")
    assert ack_deliveries(ctx.store, acks) == 1
    assert ack_deliveries(ctx.store, acks) == 0
    d = drafts(ctx)[0]
    assert d["state"] == "delivered" and d["delivery_ref"] == "AAMk-1"
    # a second deliver writes nothing for already-acked drafts
    assert pipeline.stage_deliver(ctx) == []


def test_mark_sent_starts_cooldown(cfg):
    _, ctx = run_campaign(cfg, CID, dry_run=True)
    d = drafts(ctx)[0]
    mark(ctx.store, d["draft_id"], "sent")
    assert ctx.store.get_draft(d["draft_id"])["state"] == "sent"
    assert ctx.store.in_cooldown(d["prospect_id"], 180)
    # a different campaign won't qualify this person during the cooldown
    ctx.store.db.execute("DELETE FROM campaign_prospects WHERE prospect_id=?", (d["prospect_id"],))
    ctx.store.db.commit()
    _, ctx2 = run_campaign(cfg, CID, dry_run=True)
    assert ctx2.store.get_cp(CID, d["prospect_id"])["reason"] == "recontact cooldown"


def test_set_email_resolves_unconfirmed(cfg):
    _, ctx = run_campaign(cfg, CID, dry_run=True)
    held = ctx.store.in_state(CID, "review")
    assert len(held) == 1 and held[0]["reason"] == "email_unconfirmed"
    set_email(ctx, held[0]["prospect_id"], "olivia.grant@pjtpartners.com")
    text, ctx2 = run_campaign(cfg, CID, dry_run=True)
    assert "→ 1 drafts" in text
    assert len(drafts(ctx2)) == 11


def test_crash_mid_stage_loses_nothing(cfg, monkeypatch):
    real = pipeline.compose_one
    calls = {"n": 0}

    def flaky(ctx, p, existing=None):
        calls["n"] += 1
        if calls["n"] == 4:
            raise KeyboardInterrupt("simulated crash")
        return real(ctx, p, existing)

    monkeypatch.setattr(pipeline, "compose_one", flaky)
    with pytest.raises(KeyboardInterrupt):
        run_campaign(cfg, CID, dry_run=True)
    store = open_store(cfg, dry_run=True)
    assert len(store.drafts(CID)) == 3  # committed before the crash
    monkeypatch.setattr(pipeline, "compose_one", real)
    _, ctx = run_campaign(cfg, CID, dry_run=True)
    assert len(drafts(ctx)) == 10
    assert len({d["prospect_id"] for d in drafts(ctx)}) == 10


def test_pool_without_keys_fails_before_spending(cfg, campaign_file):
    with pytest.raises(ConfigError, match="PDL_API_KEY is not set"):
        run_campaign(cfg, campaign_file(source="pool"))


def test_unimplemented_delivery_is_clear(cfg, campaign_file, tmp_path):
    csv = tmp_path / "c.csv"
    csv.write_text("First Name,Last Name,Email Address,Company,Position,Headline,Location,School\n"
                   "Jordan,Lee,j@evercore.com,Evercore,Associate,Investment Banking Associate,\"New York, NY\",Your University\n")
    with pytest.raises(ConfigError, match="milestone 3"):
        run_campaign(cfg, campaign_file(delivery="graph_draft", source_options={"path": str(csv)}))


def test_no_send_capability_exists():
    """Acceptance: asking to send fails because nothing can send. Guard against regressions."""
    import autonet
    import pkgutil
    import importlib

    for m in pkgutil.walk_packages(autonet.__path__, "autonet."):
        src = Path(importlib.import_module(m.name).__file__).read_text()
        assert "smtplib" not in src and "sendMail" not in src and "Mail.Send\"" not in src, m.name


def test_limit_caps_paid_lookups(cfg):
    text, ctx = run_campaign(cfg, CID, dry_run=True, limit=4)
    assert len(drafts(ctx)) == 4
    # 4 accepted, plus misses/unconfirmed met along the way; never all 11
    assert ctx.ledger.run_total("credits") < 11
    assert "lookups saved for the next run" in text
    text2, ctx2 = run_campaign(cfg, CID, dry_run=True)
    assert len(drafts(ctx2)) == 10
