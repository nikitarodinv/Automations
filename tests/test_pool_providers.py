"""Milestone 2: provider pool, finder waterfall, real adapters (against a fake API), budgets."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from autonet.errors import AuthError, ConfigError
from autonet.ledger import Ledger, RunBudget
from autonet.pipeline import run_campaign
from autonet.pool import Allowance, Pool, _next_reset, _period_start
from autonet.providers.contract import run_contract
from autonet.store import Store
from tests.fakeapi import FakeApi

CID = "ib-alumni-fall26"
HUNTER = "https://api.hunter.io/v2"
PDL = "https://api.peopledatalabs.com/v5"
APOLLO = "https://api.apollo.io/api/v1"
PROSPEO = "https://api.prospeo.io/enrich-person"


# --------------------------------------------------------------------------- helpers
def account(remaining=50, reset="2026-12-01"):
    return (200, {"data": {"plan_name": "Free", "reset_date": reset, "requests": {
        "credits": {"used": 50 - remaining, "available": 50, "remaining": remaining},
        "searches": {"used": 0, "available": 50, "remaining": remaining}}}})


def hunter_found(email, score=94, status="valid", first="Jordan", last="Lee"):
    return (200, {"data": {"first_name": first, "last_name": last, "email": email, "score": score, "accept_all": status == "accept_all",
                           "sources": [], "verification": {"date": "2026-11-01", "status": status}}}, {"X-Credits-Used": "1"})


HUNTER_MISS = (200, {"data": {"email": None, "score": None, "verification": {"status": None}}})


def pdl_record(first, last, company, domain, title="Associate", school="Your University", loc="New York, New York, United States"):
    return {"id": f"{first}{last}", "first_name": first, "last_name": last, "linkedin_url": f"linkedin.com/in/{first.lower()}-{last.lower()}",
            "job_title": title, "job_company_name": company, "job_company_website": domain, "location_name": loc,
            "headline": f"Investment Banking {title} at {company}", "education": [{"school": {"name": school}, "end_date": "2021"}],
            "experience": []}


@pytest.fixture
def pool_cfg(cfg):
    cfg.data["jev"]["provider"] = "off"
    cfg.data["providers"] = {
        "pace": "burst", "reserve": 0,
        "sources": [{"name": "pdl", "monthly": 100}],
        "finders": [{"name": "hunter", "monthly": 50}, {"name": "prospeo", "monthly": 75}],
    }
    return cfg


@pytest.fixture
def keys(monkeypatch):
    for k in ("HUNTER_API_KEY", "PDL_API_KEY", "PROSPEO_API_KEY"):
        monkeypatch.setenv(k, f"secret-{k.lower()}")


def pool_campaign(campaign_file, **kw):
    return campaign_file(source="pool", **kw)


# --------------------------------------------------------------------------- contract suite
@pytest.mark.parametrize("provider", ["hunter", "prospeo", "apollo", "pdl"])
def test_contract_suite(provider):
    failed = [c for c in run_contract(provider) if not c.ok]
    assert not failed, [(c.case, c.name, c.detail) for c in failed]


# --------------------------------------------------------------------------- allowances & pacing
def test_even_pacing_spreads_hunter_50_over_the_month():
    a = Allowance("hunter", monthly=50, reserve=5, today=date(2026, 11, 21))
    assert a.days_left == 10
    assert a.today_cap() == pytest.approx(4.5)
    for _ in range(4):
        assert a.can_spend(1)
        a.charge(1)
    assert not a.can_spend(1)  # 4 + 1 > 4.5


def test_first_call_of_day_allowed_even_when_cap_below_one():
    a = Allowance("hunter", monthly=50, reserve=5, today=date(2026, 11, 1))
    a.used_period = 30
    assert a.today_cap() < 1
    assert a.can_spend(1)
    a.charge(1)
    assert not a.can_spend(1)


def test_reserve_is_held_back_and_balance_wins():
    a = Allowance("hunter", monthly=50, reserve=5, pace="burst")
    a.balance = 6
    assert a.can_spend(1) and not a.can_spend(2)
    a.balance = None
    a.used_period = 45
    assert not a.can_spend(1)


def test_reset_dates():
    assert _next_reset(date(2026, 11, 21), "calendar_month", None) == date(2026, 12, 1)
    assert _next_reset(date(2026, 12, 21), "calendar_month", None) == date(2027, 1, 1)
    assert _next_reset(date(2026, 11, 21), "signup_day", 25) == date(2026, 11, 25)
    assert _period_start(date(2026, 11, 21), "signup_day", 25) == date(2026, 10, 25)


def test_monthly_budget_caps_allowance_and_ledger_counts(pool_cfg, tmp_path):
    pool_cfg.data["budgets"]["provider_monthly"] = {"hunter": 10}
    store = Store(tmp_path / "x.db")
    store.start_run("r", "c", False)
    led = Ledger(store, "r", RunBudget(100, 0, 0))
    led.record("hunter", "find", credits=4)
    a = Pool(pool_cfg, store, led).allowance("hunter")
    assert a.monthly == 10 and a.remaining == 6


def test_missing_keys_are_skipped_and_named(pool_cfg, campaign_file, monkeypatch):
    monkeypatch.setenv("PDL_API_KEY", "k")
    api = FakeApi().add("POST", f"{PDL}/person/search", (200, {"data": [], "total": 0})).install(monkeypatch)
    text, ctx = run_campaign(pool_cfg, pool_campaign(campaign_file))
    assert "skipped hunter: HUNTER_API_KEY is not set" in text
    assert "skipped prospeo: PROSPEO_API_KEY is not set" in text
    assert len(api.calls) == 1


# --------------------------------------------------------------------------- end to end with real adapters
def test_pool_run_pdl_then_hunter(pool_cfg, campaign_file, keys, monkeypatch):
    api = (FakeApi()
           .add("GET", f"{HUNTER}/account", account(31))
           .add("POST", f"{PDL}/person/search", (200, {"status": 200, "total": 2, "scroll_token": "tok1", "data": [
               pdl_record("Jordan", "Lee", "Evercore", "evercore.com"),
               pdl_record("Priya", "Shah", "Goldman Sachs", "gs.com", title="Analyst")]}))
           .add("GET", f"{HUNTER}/email-finder",
                lambda req: __import__("httpx").Response(200, json=hunter_found(
                    f"{req.url.params['first_name'].lower()}@{req.url.params['domain']}")[1], headers={"X-Credits-Used": "1"})))
    api.install(monkeypatch)
    text, ctx = run_campaign(pool_cfg, pool_campaign(campaign_file))
    drafts = ctx.store.drafts(CID)
    assert len(drafts) == 2 and all(d["state"] == "delivered" for d in drafts)
    assert {d["to_email"] for d in drafts} == {"jordan@evercore.com", "priya@gs.com"}
    # keys travel in headers, never URLs
    for c in api.calls:
        assert "secret" not in str(c.url)
    assert api.to(f"{HUNTER}/email-finder")[0].headers["X-API-KEY"] == "secret-hunter_api_key"
    assert api.to(f"{PDL}/person/search")[0].headers["X-Api-Key"] == "secret-pdl_api_key"
    # ledger: 2 PDL records, 2 Hunter credits; report shows provider lines
    assert ctx.ledger.total_since("pdl", "2000") == 2 and ctx.ledger.total_since("hunter", "2000") == 2
    assert "hunter 29/50 left · resets Dec 1" in text
    assert "Finder credits 2 of 20" in text
    # PDL's scroll token is kept, so the next run gets new people instead of paying for the same ones
    assert ctx.store.get_cursor(CID, "pdl", "search") == "tok1"
    q = FakeApi.body(api.to(f"{PDL}/person/search")[0])
    assert q["size"] <= 40 and "query" in q


def test_rerun_with_pool_spends_nothing_new(pool_cfg, campaign_file, keys, monkeypatch):
    api = (FakeApi().add("GET", f"{HUNTER}/account", account(31))
           .add("POST", f"{PDL}/person/search", (200, {"data": [pdl_record("Jordan", "Lee", "Evercore", "evercore.com")], "scroll_token": "t"}),
                (200, {"data": [pdl_record("Jordan", "Lee", "Evercore", "evercore.com")], "scroll_token": "t2"}))
           .add("GET", f"{HUNTER}/email-finder", hunter_found("jordan.lee@evercore.com")))
    api.install(monkeypatch)
    path = pool_campaign(campaign_file)
    run_campaign(pool_cfg, path)
    text, ctx = run_campaign(pool_cfg, path)
    assert len(ctx.store.drafts(CID)) == 1
    assert len(api.to(f"{HUNTER}/email-finder")) == 1   # second run: Jordan already delivered, no lookup


def _one_prospect_csv(tmp_path, first="Jordan", last="Lee", company="Evercore", email=""):
    csv = tmp_path / f"{first}.csv"
    csv.write_text("First Name,Last Name,URL,Email Address,Company,Position,Headline,Location,School\n"
                   f"{first},{last},https://www.linkedin.com/in/{first.lower()},{email},{company},Associate,Investment Banking Associate,\"New York, NY\",Your University\n")
    return str(csv)


def test_waterfall_first_passing_finder_wins(pool_cfg, campaign_file, keys, tmp_path, monkeypatch):
    api = (FakeApi().add("GET", f"{HUNTER}/account", account())
           .add("GET", f"{HUNTER}/email-finder", HUNTER_MISS)
           .add("POST", PROSPEO, (200, {"error": False, "person": {"email": {"status": "VERIFIED", "revealed": True, "email": "jl@evercore.com"}}})))
    api.install(monkeypatch)
    _, ctx = run_campaign(pool_cfg, campaign_file(source_options={"path": _one_prospect_csv(tmp_path)}))
    d = ctx.store.drafts(CID)[0]
    assert d["to_email"] == "jl@evercore.com" and d["audit"]["email_provider"] == "prospeo"
    assert len(api.to(PROSPEO)) == 1


def test_waterfall_stops_at_first_pass(pool_cfg, campaign_file, keys, tmp_path, monkeypatch):
    api = (FakeApi().add("GET", f"{HUNTER}/account", account())
           .add("GET", f"{HUNTER}/email-finder", hunter_found("jordan.lee@evercore.com")))
    api.install(monkeypatch)
    _, ctx = run_campaign(pool_cfg, campaign_file(source_options={"path": _one_prospect_csv(tmp_path)}))
    assert ctx.store.drafts(CID)[0]["to_email"] == "jordan.lee@evercore.com"
    assert not api.to(PROSPEO)


def test_two_finders_agreeing_confirm_a_low_score(pool_cfg, campaign_file, keys, tmp_path, monkeypatch):
    pool_cfg.data["budgets"]["hunter_monthly"] = 50
    api = (FakeApi().add("GET", f"{HUNTER}/account", account())
           .add("GET", f"{HUNTER}/email-finder", hunter_found("jordan.lee@evercore.com", score=70, status="accept_all"))
           .add("POST", PROSPEO, (200, {"error": False, "person": {"email": {"status": "SOMETHING_ELSE", "revealed": True, "email": "Jordan.Lee@evercore.com"}}})))
    api.install(monkeypatch)
    _, ctx = run_campaign(pool_cfg, campaign_file(source_options={"path": _one_prospect_csv(tmp_path)}))
    lk = ctx.store.accepted_email(ctx.store.get_prospect(ctx.store.drafts(CID)[0]["prospect_id"]))
    assert lk["decision"] == "confirmed" and lk["provider"] == "hunter+prospeo"


def test_unconfirmed_goes_to_review(pool_cfg, campaign_file, keys, tmp_path, monkeypatch):
    api = (FakeApi().add("GET", f"{HUNTER}/account", account())
           .add("GET", f"{HUNTER}/email-finder", hunter_found("jordan.lee@evercore.com", score=70, status="accept_all"))
           .add("POST", PROSPEO, (400, {"error": True, "error_code": "NO_MATCH"})))
    api.install(monkeypatch)
    text, ctx = run_campaign(pool_cfg, campaign_file(source_options={"path": _one_prospect_csv(tmp_path)}))
    assert "1 unconfirmed emails" in text
    assert ctx.store.in_state(CID, "review")[0]["reason"] == "email_unconfirmed"


def test_hunter_verifier_poll_and_222_retry_next_run(pool_cfg, campaign_file, keys, tmp_path, monkeypatch):
    pool_cfg.data["providers"]["finders"] = [{"name": "hunter", "monthly": 50}]
    api = (FakeApi().add("GET", f"{HUNTER}/account", account())
           .add("GET", f"{HUNTER}/email-finder", hunter_found("jordan.lee@evercore.com", score=75, status="unknown"))
           .add("GET", f"{HUNTER}/email-verifier", (202, {"data": {}}), (222, {"data": {}}),
                (200, {"data": {"status": "valid", "score": 93}})))
    api.install(monkeypatch)
    path = campaign_file(source_options={"path": _one_prospect_csv(tmp_path)})
    text, ctx = run_campaign(pool_cfg, path)
    assert ctx.store.state_counts(CID) == {"pending_email": 1}
    assert "1 waiting" in text
    text2, ctx2 = run_campaign(pool_cfg, path)
    assert len(api.to(f"{HUNTER}/email-finder")) == 1       # no second find: only the check is retried
    d = ctx2.store.drafts(CID)[0]
    assert d["to_email"] == "jordan.lee@evercore.com" and d["email_score"] == 93


def test_hunter_451_suppresses_for_good(pool_cfg, campaign_file, keys, tmp_path, monkeypatch):
    api = (FakeApi().add("GET", f"{HUNTER}/account", account())
           .add("GET", f"{HUNTER}/email-finder", (451, {"errors": [{"id": "claimed_email", "code": 451}]})))
    api.install(monkeypatch)
    path = campaign_file(source_options={"path": _one_prospect_csv(tmp_path)})
    _, ctx = run_campaign(pool_cfg, path)
    assert ctx.store.state_counts(CID) == {"suppressed": 1}
    assert not api.to(PROSPEO)
    run_campaign(pool_cfg, path)
    assert len(api.to(f"{HUNTER}/email-finder")) == 1


def test_hunter_429_moves_to_next_finder_and_stops_hunter(pool_cfg, campaign_file, keys, tmp_path, monkeypatch):
    csv = tmp_path / "two.csv"
    csv.write_text("First Name,Last Name,Company,Position,Headline,Location,School\n"
                   "Jordan,Lee,Evercore,Associate,Investment Banking Associate,\"New York, NY\",Your University\n"
                   "Priya,Shah,Lazard,Analyst,Investment Banking Analyst,\"New York, NY\",Your University\n")
    api = (FakeApi().add("GET", f"{HUNTER}/account", account())
           .add("GET", f"{HUNTER}/email-finder", (429, {"errors": [{"id": "usage_limit", "code": 429}]}))
           .add("POST", PROSPEO, lambda req: __import__("httpx").Response(200, json={"error": False, "person": {"email": {
               "status": "VERIFIED", "revealed": True, "email": FakeApi.body(req)["data"]["first_name"].lower() + "@x.com"}}})))
    api.install(monkeypatch)
    text, ctx = run_campaign(pool_cfg, campaign_file(source_options={"path": str(csv)}))
    assert len(ctx.store.drafts(CID)) == 2
    assert len(api.to(f"{HUNTER}/email-finder")) == 1      # not asked again after the quota error
    assert "skipping it for the rest of this run" in text


def test_hunter_403_backs_off_then_gives_up_for_this_run(pool_cfg, campaign_file, keys, tmp_path, monkeypatch):
    from autonet import http

    waits = []
    monkeypatch.setattr(http, "SLEEP", waits.append)
    pool_cfg.data["providers"]["finders"] = [{"name": "hunter", "monthly": 50}]
    api = (FakeApi().add("GET", f"{HUNTER}/account", account())
           .add("GET", f"{HUNTER}/email-finder", (403, {"errors": [{"id": "rate_limit", "code": 403}]})))
    api.install(monkeypatch)
    _, ctx = run_campaign(pool_cfg, campaign_file(source_options={"path": _one_prospect_csv(tmp_path)}))
    assert len(api.to(f"{HUNTER}/email-finder")) == 6            # 1 try + 5 retries
    backoff = [w for w in waits if w >= 1]
    assert len(backoff) == 5 and all(b <= w <= b * 1.25 for w, b in zip(backoff, [1, 2, 4, 8, 16]))
    assert ctx.store.state_counts(CID) == {"pending_email": 1}   # kept for the next run


def test_retry_after_is_honored(monkeypatch):
    from autonet import http

    waits = []
    monkeypatch.setattr(http, "SLEEP", waits.append)
    FakeApi().add("GET", "https://x.test/a", (503, {}, {"Retry-After": "7"}), (200, {"ok": 1})).install(monkeypatch)
    r = http.ApiClient("x").request("GET", "https://x.test/a")
    assert r.ok and 7.0 in waits


def test_hunter_401_aborts_before_spending(pool_cfg, campaign_file, keys, tmp_path, monkeypatch):
    api = FakeApi().add("GET", f"{HUNTER}/account", (401, {"errors": [{"id": "authentication_failed", "code": 401}]}))
    api.install(monkeypatch)
    with pytest.raises(AuthError, match="hunter"):
        run_campaign(pool_cfg, campaign_file(source_options={"path": _one_prospect_csv(tmp_path)}))
    assert not api.to(f"{HUNTER}/email-finder")


def test_hunter_400_marks_data_error(pool_cfg, campaign_file, keys, tmp_path, monkeypatch):
    pool_cfg.data["providers"]["finders"] = [{"name": "hunter", "monthly": 50}]
    api = (FakeApi().add("GET", f"{HUNTER}/account", account())
           .add("GET", f"{HUNTER}/email-finder", (400, {"errors": [{"id": "invalid_domain", "code": 400}]})))
    api.install(monkeypatch)
    _, ctx = run_campaign(pool_cfg, campaign_file(source_options={"path": _one_prospect_csv(tmp_path)}))
    row = ctx.store.campaign_prospects(CID)[0]
    assert row["state"] == "skipped" and "invalid_domain" in row["reason"]


def test_credit_budget_with_real_finder(pool_cfg, campaign_file, keys, tmp_path, monkeypatch):
    rows = "\n".join(f"P{i},Lee,Evercore,Associate,Investment Banking Associate,\"New York, NY\",Your University" for i in range(6))
    csv = tmp_path / "six.csv"
    csv.write_text("First Name,Last Name,Company,Position,Headline,Location,School\n" + rows + "\n")
    pool_cfg.data["providers"]["finders"] = [{"name": "hunter", "monthly": 50}]
    api = (FakeApi().add("GET", f"{HUNTER}/account", account())
           .add("GET", f"{HUNTER}/email-finder", lambda req: __import__("httpx").Response(
               200, json=hunter_found(req.url.params["first_name"].lower() + "@evercore.com")[1], headers={"X-Credits-Used": "1"})))
    api.install(monkeypatch)
    text, ctx = run_campaign(pool_cfg, campaign_file(source_options={"path": str(csv)}, limits__hunter_credit_budget=3,
                                                     limits__max_per_company_per_run=10))
    assert ctx.ledger.total_since("hunter", "2000") == 3
    assert "run credit budget reached" in text
    assert ctx.store.state_counts(CID)["pending_email"] == 3


def test_even_pacing_leaves_people_pending(pool_cfg, campaign_file, keys, tmp_path, monkeypatch):
    pool_cfg.data["providers"]["pace"] = "even"
    pool_cfg.data["providers"]["finders"] = [{"name": "hunter", "monthly": 50}]
    rows = "\n".join(f"P{i},Lee,Evercore,Associate,Investment Banking Associate,\"New York, NY\",Your University" for i in range(5))
    csv = tmp_path / "five.csv"
    csv.write_text("First Name,Last Name,Company,Position,Headline,Location,School\n" + rows + "\n")
    # 8 left, 7 days to reset, reserve 5 -> under 1 a day: only the first call of the day is allowed
    reset = date.fromordinal(date.today().toordinal() + 7).isoformat()
    api = (FakeApi().add("GET", f"{HUNTER}/account", account(8, reset))
           .add("GET", f"{HUNTER}/email-finder", hunter_found("x@evercore.com")))
    api.install(monkeypatch)
    pool_cfg.data["providers"]["reserve"] = 5
    _, ctx = run_campaign(pool_cfg, campaign_file(source_options={"path": str(csv)}, limits__max_per_company_per_run=10))
    assert len(api.to(f"{HUNTER}/email-finder")) == 1
    assert ctx.store.state_counts(CID)["pending_email"] == 4


# --------------------------------------------------------------------------- other sources
def test_hunter_domain_source_carries_email(pool_cfg, campaign_file, keys, monkeypatch):
    emails = [{"value": "jordan.lee@evercore.com", "type": "personal", "confidence": 95, "first_name": "Jordan", "last_name": "Lee",
               "position": "Investment Banking Associate", "seniority": "junior", "linkedin": "https://www.linkedin.com/in/jordan-lee",
               "verification": {"status": "valid"}}]
    api = (FakeApi().add("GET", f"{HUNTER}/account", account())
           .add("GET", f"{HUNTER}/domain-search", lambda req: __import__("httpx").Response(200, json={
               "data": {"organization": "Evercore", "emails": emails if req.url.params["domain"] == "evercore.com" else []},
               "meta": {"results": 1 if req.url.params["domain"] == "evercore.com" else 0}})))
    api.install(monkeypatch)
    text, ctx = run_campaign(pool_cfg, campaign_file(source="hunter_domain", targeting__locations=[]))
    d = ctx.store.drafts(CID)
    assert len(d) == 1 and d[0]["to_email"] == "jordan.lee@evercore.com"
    assert not api.to(f"{HUNTER}/email-finder")                # already had it: no finder credit
    assert ctx.ledger.total_since("hunter", "2000") == 1          # one domain search with results
    assert ctx.store.get_cursor(CID, "hunter_domain", "evercore.com") == "done"


def test_apollo_source_prefilters_before_paying(pool_cfg, campaign_file, monkeypatch):
    monkeypatch.setenv("APOLLO_API_KEY", "ak")
    pool_cfg.data["providers"] = {"pace": "burst", "reserve": 0, "sources": [{"name": "apollo", "monthly": 75}], "finders": []}
    from tests.fixtures_apollo import MATCH, SEARCH

    api = (FakeApi().add("POST", f"{APOLLO}/mixed_people/api_search", (200, SEARCH))
           .add("POST", f"{APOLLO}/people/match", (200, MATCH)))
    api.install(monkeypatch)
    text, ctx = run_campaign(pool_cfg, pool_campaign(campaign_file))
    assert len(api.to(f"{APOLLO}/people/match")) == 1             # the recruiter wasn't enriched
    d = ctx.store.drafts(CID)
    assert len(d) == 1 and d[0]["to_email"] == "jordan.lee@evercore.com"
    assert ctx.ledger.total_since("apollo", "2000") == 1
    body = FakeApi.body(api.to(f"{APOLLO}/mixed_people/api_search")[0])
    assert body["q_organization_domains_list"][:2] == ["gs.com", "evercore.com"] and "entry" in body["person_seniorities"]


def test_urls_source_enriches_by_linkedin_url(pool_cfg, campaign_file, keys, monkeypatch):
    rec = pdl_record("Jordan", "Lee", "Evercore", "evercore.com")
    api = (FakeApi().add("GET", f"{HUNTER}/account", account())
           .add("GET", f"{PDL}/person/enrich", (200, {"status": 200, "likelihood": 9, "data": rec}))
           .add("GET", f"{HUNTER}/email-finder", hunter_found("jordan.lee@evercore.com")))
    api.install(monkeypatch)
    _, ctx = run_campaign(pool_cfg, campaign_file(source="urls", source_options={"urls": ["https://www.linkedin.com/in/jordan-lee"]}))
    d = ctx.store.drafts(CID)
    assert len(d) == 1 and d[0]["body"].startswith(("Hi Jordan,", "Hello Jordan,"))
    assert api.to(f"{PDL}/person/enrich")[0].url.params["profile"] == "https://www.linkedin.com/in/jordan-lee"


def test_urls_source_falls_back_to_hunter_linkedin_handle(pool_cfg, campaign_file, keys, monkeypatch):
    api = (FakeApi().add("GET", f"{HUNTER}/account", account())
           .add("GET", f"{PDL}/person/enrich", (404, {"status": 404, "error": {"type": "not_found"}}))
           .add("GET", f"{HUNTER}/email-finder", (200, {"data": {
               "first_name": "Jordan", "last_name": "Lee", "email": "jordan.lee@evercore.com", "score": 95, "company": "Evercore",
               "domain": "evercore.com", "position": "Associate", "verification": {"status": "valid"}}})))
    api.install(monkeypatch)
    _, ctx = run_campaign(pool_cfg, campaign_file(source="urls", source_options={"urls": ["https://www.linkedin.com/in/jordan-lee"]}))
    assert api.to(f"{HUNTER}/email-finder")[0].url.params["linkedin_handle"] == "jordan-lee"
    d = ctx.store.drafts(CID)
    assert len(d) == 1 and d[0]["state"] == "delivered" and d[0]["audit"]["company"] == "Evercore"
    p = ctx.store.get_prospect(d[0]["prospect_id"])
    assert (p.first_name, p.company.domain) == ("Jordan", "evercore.com")


def test_unknown_source_name_fails_validation(pool_cfg, campaign_file):
    from autonet.providers import get_source

    with pytest.raises(ConfigError, match="Known:"):
        get_source("nope")
