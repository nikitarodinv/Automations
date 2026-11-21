"""Stage 2 through Jev (against a fake API): request shape, policy, cache, fallbacks, routes."""

from __future__ import annotations

import httpx
import pytest

from autonet.jev import JevClient, build_questions, eval_jev, load_cases
from autonet.pipeline import run_campaign
from tests.conftest import REPO
from tests.fakeapi import FakeApi

CID = "ib-alumni-fall26"
TS = "https://api.typesafe.ai/v1/systemone"


def reply(fit=0.91, choice=None, conf=0.88, tone="warm", gap=0.4, model="jev-1.13.0"):
    def handler(req):
        body = FakeApi.body(req)
        options = [k for k in body["questions"]["template"]["criteria"] if k != "none"]
        pick = choice or options[0]
        probs = {k: (conf if k == pick else round((1 - conf) / max(1, len(options)), 3)) for k in [*options, "none"]}
        return httpx.Response(200, json={"model": model, "usage": {"input_tokens": 540, "output_tokens": 0}, "answers": {
            "good_fit": {"type": "noul", "noul": fit},
            "template": {"type": "choice", "choice": pick, "confidence": conf, "probabilities": probs},
            "tone": {"type": "choice", "choice": tone, "confidence": 0.84, "probabilities": {tone: 0.84}},
            "seniority_gap": {"type": "score", "score": gap, "confidence": 0.8, "legend": {"0": "Near-peer"}, "probabilities": {"0": 0.6}},
        }})
    return handler


@pytest.fixture
def jev_cfg(cfg, monkeypatch):
    cfg.data["jev"] = {"provider": "typesafe", "model": "jev-latest", "min_confidence": 0.6, "timeout": 10}
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-secret")
    return cfg


def test_dry_run_with_live_jev(jev_cfg, monkeypatch):
    api = FakeApi().add("POST", TS, reply()).install(monkeypatch)
    text, ctx = run_campaign(jev_cfg, CID, dry_run=True, live_jev=True)
    assert len(api.calls) == 13 and "selector: jev" in text
    req = api.calls[0]
    assert req.headers["Authorization"] == "Bearer ts-secret"
    body = FakeApi.body(req)
    assert body["model"] == "jev-latest" and set(body["questions"]) == {"good_fit", "template", "tone", "seniority_gap"}
    assert body["questions"]["seniority_gap"]["criteria"] == ["Near-peer", "Some gap", "Very senior"]
    # only templates whose requires this person meets are offered, plus none
    for c in api.calls:
        offered = set(FakeApi.body(c)["questions"]["template"]["criteria"])
        assert "referral_ask" not in offered or FakeApi.body(c)["state"]["shared_context"]["prior_contact"]
        assert "saw_your_work" not in offered and "none" in offered
    d = ctx.store.drafts(CID)[0]
    assert d["audit"]["selector"] == "jev"
    assert ctx.ledger.run_total("llm_calls") == 13 and ctx.ledger.run_total("tokens") == 13 * 540
    assert "model calls 13 of 60" in text


def test_dry_run_without_flag_never_calls_jev(jev_cfg):
    text, _ = run_campaign(jev_cfg, CID, dry_run=True)   # the autouse fixture fails any request
    assert "selector: rules" in text


def test_policy_routes_skip_and_review(jev_cfg, monkeypatch):
    FakeApi().add("POST", TS, reply(fit=0.2), reply(conf=0.5), reply()).install(monkeypatch)
    text, ctx = run_campaign(jev_cfg, CID, dry_run=True, live_jev=True)
    assert "1 skipped, 1 to review" in text
    assert "1 template picks to review" in text
    held = [r for r in ctx.store.in_state(CID, "review") if r["reason"] == "jev_low_confidence"]
    assert len(held) == 1
    assert any("0.50" in line for line in text.splitlines())   # top two options shown for review


def test_decisions_are_cached_by_question_set(jev_cfg, monkeypatch):
    api = FakeApi().add("POST", TS, reply()).install(monkeypatch)
    run_campaign(jev_cfg, CID, dry_run=True, live_jev=True)
    n = len(api.calls)
    # wipe campaign state so everyone is re-qualified: decisions come from the cache
    from autonet.pipeline import open_store

    st = open_store(jev_cfg, True)
    st.db.execute("DELETE FROM campaign_prospects"); st.db.execute("DELETE FROM drafts"); st.db.commit()
    run_campaign(jev_cfg, CID, dry_run=True, live_jev=True)
    assert len(api.calls) == n


def test_http_errors_retry_twice_then_rules_fallback(jev_cfg, monkeypatch):
    api = FakeApi().add("POST", TS, (503, {"detail": "busy"})).install(monkeypatch)
    text, ctx = run_campaign(jev_cfg, CID, dry_run=True, live_jev=True)
    assert len(api.calls) == 13 * 3                 # 1 try + 2 retries each
    assert "selector: rules" in text and "Jev unavailable for 13" in text
    assert len(ctx.store.drafts(CID)) == 10          # the run still finishes
    # fallback decisions don't poison the cache: next run asks Jev again
    from autonet.pipeline import open_store

    st = open_store(jev_cfg, True)
    st.db.execute("DELETE FROM campaign_prospects"); st.db.execute("DELETE FROM drafts"); st.db.commit()
    FakeApi().add("POST", TS, reply()).install(monkeypatch)
    text2, _ = run_campaign(jev_cfg, CID, dry_run=True, live_jev=True)
    assert "selector: jev" in text2


def test_refusal_and_bad_choice_fail_closed(jev_cfg, monkeypatch):
    def refuse(req):
        r = reply()(req)
        data = r.json()
        data["answers"]["template"] = {"type": "refusal", "refusal": "no"}
        return httpx.Response(200, json=data)

    FakeApi().add("POST", TS, refuse).install(monkeypatch)
    text, ctx = run_campaign(jev_cfg, CID, dry_run=True, live_jev=True)
    assert "selector: rules" in text
    dec = ctx.store.latest_decision(CID, ctx.store.drafts(CID)[0]["prospect_id"])
    assert "refused" in dec["reason"]["jev_unavailable"]


def test_bad_key_switches_to_rules_for_the_run(jev_cfg, monkeypatch):
    api = FakeApi().add("POST", TS, (401, {"detail": "bad key"})).install(monkeypatch)
    text, _ = run_campaign(jev_cfg, CID, dry_run=True, live_jev=True)
    assert len(api.calls) == 1 and "Jev key rejected" in text


def test_missing_key_uses_rules_with_a_note(cfg, monkeypatch):
    cfg.data["jev"] = {"provider": "typesafe"}
    text, _ = run_campaign(cfg, CID, dry_run=True, live_jev=True)
    assert "TYPESAFE_API_KEY" in text and "selector: rules" in text


def test_llm_budget_stops_stage_2(jev_cfg, campaign_file, monkeypatch):
    FakeApi().add("POST", TS, reply()).install(monkeypatch)
    text, ctx = run_campaign(jev_cfg, campaign_file(limits__llm_call_budget=3), dry_run=True, live_jev=True)
    assert ctx.ledger.run_total("llm_calls") == 3
    assert "model-call budget reached" in text
    assert len(ctx.store.in_state(CID, "qualified")) == 10   # left where they were


def test_cloudflare_and_openrouter_routes(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "cf")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or")
    cf_url = "https://api.cloudflare.com/client/v4/accounts/acct1/ai/run"

    def cf(req):
        inner = reply()(req).json()
        return httpx.Response(200, json={"result": inner, "success": True})

    def cf_wrap(req):  # Cloudflare nests state/questions under "input"
        body = FakeApi.body(req)
        assert body["model"] == "typesafe/jev" and "state" in body["input"]
        req2 = httpx.Request("POST", cf_url, json=body["input"])
        return cf(req2)

    api = FakeApi().add("POST", cf_url, cf_wrap).add("POST", "https://openrouter.ai/api/alpha/decisions", reply(model="typesafe/jev-1.14"))
    api.install(monkeypatch)
    q = {"good_fit": {"type": "noul"}, "template": {"type": "choice", "criteria": {"a": "x", "none": "y"}},
         "tone": {"type": "choice"}, "seniority_gap": {"type": "score"}}
    r = JevClient("cloudflare").ask({"s": 1}, q)
    assert r.answers["template"]["choice"] == "a" and r.model == "jev-1.13.0"
    r2 = JevClient("openrouter").ask({"s": 1}, q)
    assert FakeApi.body(api.calls[-1])["model"] == "typesafe/jev-1.14" and r2.model == "typesafe/jev-1.14"


def test_eval_reports_agreement_and_calibration(jev_cfg, monkeypatch, tmp_path):
    from autonet.compose import load_templates
    from autonet.jev import JevSelector
    from autonet.campaign import load_campaign
    from tests.conftest import CAMPAIGN

    FakeApi().add("POST", TS, reply(conf=0.85)).install(monkeypatch)
    cases = load_cases(REPO / "jev/eval/cases.example.jsonl")
    camp = load_campaign(CAMPAIGN)
    templates = load_templates(REPO / "templates")
    allowed = [templates[t] for t in camp["templates"]["allowed"]]
    out = eval_jev(JevSelector(JevClient("typesafe"), camp), cases, allowed, ["Your University"], [])
    assert out["cases"] == len(cases) and out["answered_by_jev"] == len(cases)
    assert 0 <= out["agreement"] <= 1 and "0.8-0.9" in out["calibration"]
