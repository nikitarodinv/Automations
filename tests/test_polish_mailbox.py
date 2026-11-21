"""Milestone 3: optional polish (guards) and mailbox delivery (Graph, Gmail, sign-in flows)."""

from __future__ import annotations

import base64
import email
import json
import os
import stat
import threading
import time
import urllib.request
from pathlib import Path

import httpx
import pytest

from autonet.delivery.auth import GoogleAuth, MicrosoftAuth, TokenCache
from autonet.delivery.graph import GraphDelivery
from autonet.delivery.gmail import GmailDelivery
from autonet.draft import changed_fraction, new_facts, polish
from autonet.errors import ConfigError
from autonet.pipeline import run_campaign
from tests.fakeapi import FakeApi
from tests.test_research import FakeLLM

CID = "ib-alumni-fall26"
BODY = ("Hi Jordan,\n\nI'm a junior at Your University and saw that your a fellow alum now at Evercore. "
        "I'm trying to learn as much as I can before recruiting picks up.\n\n"
        "Would you be open to a 15-minute call in the next couple of weeks?\n\n"
        "Thanks for considering it, and no worries if the timing doesn't work.")
SUBJECT = "Your University student, quick question about Evercore"


# --------------------------------------------------------------------------- polish guards
def test_good_polish_applies():
    fixed = BODY.replace("your a fellow", "you're a fellow")
    r = polish(SUBJECT, BODY, 150, FakeLLM({"subject": SUBJECT, "body": fixed}))
    assert r.applied and "you're a fellow" in r.body


@pytest.mark.parametrize("change, reason", [
    (lambda b: b.replace("Evercore.", "Evercore and Goldman Sachs."), "new names"),
    (lambda b: b.replace("15-minute", "20-minute"), "new names or numbers"),
    (lambda b: "Hello Jordan,\n\n" + b.split("\n\n", 1)[1], "greeting"),
    (lambda b: b.split("\n\n")[0] + "\n\nCompletely different words that rewrite everything here for no reason at all, yes.", "changed"),
])
def test_polish_guards_reject(change, reason):
    r = polish(SUBJECT, BODY, 150, FakeLLM({"subject": SUBJECT, "body": change(BODY)}))
    assert not r.applied and reason in r.reason and r.body == BODY


def test_fact_and_change_helpers():
    assert new_facts("Hi Jordan, thanks", "Hi Jordan, Thanks") == []          # capitalization isn't a new fact
    assert new_facts("Hi Jordan", "Hi Jordan from Lazard") == ["Lazard"]
    assert changed_fraction("abcdefghij", "abcdefghij") == 0
    assert changed_fraction("abcdefghij", "abcdeXXXXX") == 0.5


def test_pipeline_polish_is_recorded_and_reused(cfg, campaign_file, tmp_path, monkeypatch):
    from autonet import pipeline

    cfg.data["jev"]["provider"] = "off"
    cfg.data["draft"]["polish"] = True
    calls = []

    def fake_polish(subject, body, max_words, llm):
        calls.append(body)
        from autonet.draft import Polish

        return Polish(True, subject, body.replace("Hi Jordan,", "Hi Jordan,"), "")

    monkeypatch.setattr("autonet.draft.polish", fake_polish)
    monkeypatch.setattr("autonet.llm.from_config", lambda cfg, client=None: object())
    csv = tmp_path / "c.csv"
    csv.write_text("First Name,Last Name,Email Address,Company,Position,Headline,Location,School\n"
                   "Jordan,Lee,jordan.lee@evercore.com,Evercore,Associate,Investment Banking Associate,\"New York, NY\",Your University\n")
    path = campaign_file(source_options={"path": str(csv)}, review_before_delivery=True)
    text, ctx = run_campaign(cfg, path)
    d = ctx.store.drafts(CID)[0]
    assert d["audit"]["polish"]["applied"] is True and "Polish: 1 applied" in text
    assert ctx.ledger.run_total("llm_calls") == 1
    # rebuilding with a note changes the input, so polish runs again; rebuilding unchanged reuses it
    pipeline.add_note(ctx, d["draft_id"], "I enjoyed your post on private credit.")
    assert len(calls) == 2
    p = ctx.store.get_prospect(d["prospect_id"])
    pipeline.compose_one(ctx, p)
    assert len(calls) == 2


# --------------------------------------------------------------------------- Microsoft sign-in
MS = "https://login.microsoftonline.com/organizations/oauth2/v2.0"


def ms_auth(tmp_path):
    return MicrosoftAuth("app-123", "organizations", TokenCache(tmp_path / "cfg" / "ms.json"))


def test_device_code_flow_polls_and_caches_token(tmp_path, monkeypatch):
    shown = []
    api = (FakeApi()
           .add("POST", f"{MS}/devicecode", (200, {"device_code": "dc", "user_code": "ABCD-EFGH", "interval": 1, "expires_in": 900,
                                                  "verification_uri": "https://microsoft.com/devicelogin",
                                                  "message": "To sign in, enter ABCD-EFGH at https://microsoft.com/devicelogin"}))
           .add("POST", f"{MS}/token", (400, {"error": "authorization_pending"}), (400, {"error": "slow_down"}),
                (200, {"access_token": "at1", "refresh_token": "rt1", "expires_in": 3600,
                       "scope": "https://graph.microsoft.com/Mail.ReadWrite"})))
    api.install(monkeypatch)
    a = ms_auth(tmp_path)
    tok = a.device_login(echo=shown.append)
    assert tok["access_token"] == "at1" and "ABCD-EFGH" in shown[0]
    assert stat.S_IMODE(os.stat(a.cache.path).st_mode) == 0o600
    body = api.to(f"{MS}/devicecode")[0].content.decode()
    assert "Mail.ReadWrite" in body and "Mail.Send" not in body
    assert a.access_token() == "at1"


def test_token_with_mail_send_is_refused(tmp_path, monkeypatch):
    (FakeApi().add("POST", f"{MS}/devicecode", (200, {"device_code": "dc", "user_code": "X", "interval": 1, "verification_uri": "u"}))
     .add("POST", f"{MS}/token", (200, {"access_token": "at", "expires_in": 3600, "scope": "Mail.ReadWrite Mail.Send"}))).install(monkeypatch)
    with pytest.raises(ConfigError, match="Mail.Send"):
        ms_auth(tmp_path).device_login(echo=lambda m: None)
    assert not (tmp_path / "cfg" / "ms.json").exists()


def test_school_approval_needed_is_explained(tmp_path, monkeypatch):
    FakeApi().add("POST", f"{MS}/devicecode", (400, {"error": "invalid_client",
        "error_description": "AADSTS65001: The user or administrator has not consented to use the application."})).install(monkeypatch)
    with pytest.raises(ConfigError, match="school needs to approve.*delivery: eml"):
        ms_auth(tmp_path).device_login(echo=lambda m: None)


def test_expired_token_refreshes(tmp_path, monkeypatch):
    a = ms_auth(tmp_path)
    a.cache.save({"access_token": "old", "refresh_token": "rt1", "expires_at": time.time() - 10})
    FakeApi().add("POST", f"{MS}/token", (200, {"access_token": "new", "expires_in": 3600, "scope": "Mail.ReadWrite"})).install(monkeypatch)
    assert a.access_token() == "new"
    assert a.cache.load()["refresh_token"] == "rt1"    # kept when the reply omits it


# --------------------------------------------------------------------------- Graph delivery
def _drafts():
    return [{"draft_id": f"d_{i}", "to_email": f"p{i}@evercore.com", "subject": "Hello", "body": "Hi P,\n\nBody"} for i in range(2)]


def test_graph_creates_drafts_only(tmp_path, monkeypatch):
    a = ms_auth(tmp_path)
    a.cache.save({"access_token": "at", "refresh_token": "rt", "expires_at": time.time() + 3600})
    n = iter(range(100))
    api = FakeApi().add("POST", "https://graph.microsoft.com/v1.0/me/messages",
                        lambda r: httpx.Response(201, json={"id": f"AAMk{next(n)}", "isDraft": True}))
    api.install(monkeypatch)
    out = GraphDelivery(tmp_path, a).deliver(_drafts())
    assert [o.ref for o in out] == ["AAMk0", "AAMk1"]
    sent = FakeApi.body(api.calls[0])
    assert sent["toRecipients"][0]["emailAddress"]["address"] == "p0@evercore.com" and sent["body"]["contentType"] == "Text"
    assert api.calls[0].headers["Authorization"] == "Bearer at"
    assert all(str(c.url).endswith("/me/messages") for c in api.calls)    # never /send or /sendMail


def test_graph_401_refreshes_once_then_retries(tmp_path, monkeypatch):
    a = ms_auth(tmp_path)
    a.cache.save({"access_token": "stale", "refresh_token": "rt", "expires_at": time.time() + 3600})
    api = (FakeApi().add("POST", "https://graph.microsoft.com/v1.0/me/messages",
                         lambda r: httpx.Response(401 if r.headers["Authorization"] == "Bearer stale" else 201, json={"id": "AAMk9"}))
           .add("POST", f"{MS}/token", (200, {"access_token": "fresh", "expires_in": 3600, "scope": "Mail.ReadWrite"})))
    api.install(monkeypatch)
    assert GraphDelivery(tmp_path, a).deliver_one(_drafts()[0]).ref == "AAMk9"


def test_graph_pipeline_saves_each_id_before_the_next(cfg, campaign_file, tmp_path, monkeypatch):
    """A crash mid-delivery never creates a second mailbox draft for the same draft_id."""
    cfg.data["jev"]["provider"] = "off"
    cfg.data["mailbox"] = {"microsoft": {"client_id": "app-123", "token_cache": str(tmp_path / "ms.json")}}
    TokenCache(tmp_path / "ms.json").save({"access_token": "at", "refresh_token": "rt", "expires_at": time.time() + 3600})
    csv = tmp_path / "c.csv"
    csv.write_text("First Name,Last Name,Email Address,Company,Position,Headline,Location,School\n"
                   "Jordan,Lee,jordan.lee@evercore.com,Evercore,Associate,Investment Banking Associate,\"New York, NY\",Your University\n"
                   "Priya,Shah,priya@gs.com,Goldman Sachs,Analyst,Investment Banking Analyst,\"New York, NY\",Your University\n")
    calls = {"n": 0}

    outage = {"on": True}

    def graph(r):
        calls["n"] += 1
        if outage["on"] and "priya" in r.content.decode():
            return httpx.Response(500, json={})     # Graph down for the second draft, even after retries
        return httpx.Response(201, json={"id": f"AAMk{calls['n']}"})

    FakeApi().add("POST", "https://graph.microsoft.com/v1.0/me/messages", graph).install(monkeypatch)
    path = campaign_file(delivery="graph_draft", source_options={"path": str(csv)})
    from autonet.errors import TransientError

    with pytest.raises(TransientError):
        run_campaign(cfg, path)
    from autonet.pipeline import open_store

    st = open_store(cfg, False)
    delivered = st.drafts(CID, ["delivered"])
    assert len(delivered) == 1 and delivered[0]["delivery_ref"] == "AAMk1"
    outage["on"] = False
    calls["n"] = 10
    _, ctx = run_campaign(cfg, path)
    refs = sorted(d["delivery_ref"] for d in ctx.store.drafts(CID))
    assert refs == ["AAMk1", "AAMk11"]   # the first draft was not created again


# --------------------------------------------------------------------------- Gmail
def test_gmail_creates_draft_with_raw_mime(tmp_path, monkeypatch):
    secrets = tmp_path / "secret.json"
    secrets.write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "cs"}}))
    a = GoogleAuth(secrets, TokenCache(tmp_path / "g.json"))
    a.cache.save({"access_token": "gt", "refresh_token": "grt", "expires_at": time.time() + 3600})
    api = FakeApi().add("POST", "https://gmail.googleapis.com/gmail/v1/users/me/drafts", (200, {"id": "r-123", "message": {"id": "m1"}}))
    api.install(monkeypatch)
    out = GmailDelivery(tmp_path, a).deliver_one(_drafts()[0])
    assert out.ref == "r-123"
    raw = FakeApi.body(api.calls[0])["message"]["raw"]
    msg = email.message_from_bytes(base64.urlsafe_b64decode(raw))
    assert msg["To"] == "p0@evercore.com" and msg["Subject"] == "Hello"


def test_google_loopback_sign_in_with_pkce(tmp_path, monkeypatch):
    secrets = tmp_path / "secret.json"
    secrets.write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "cs"}}))
    a = GoogleAuth(secrets, TokenCache(tmp_path / "g.json"))
    api = FakeApi().add("POST", "https://oauth2.googleapis.com/token", (200, {"access_token": "gt", "refresh_token": "grt", "expires_in": 3600}))
    api.install(monkeypatch)

    def browser(url):
        from urllib.parse import parse_qs, urlsplit

        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        assert q["scope"].endswith("gmail.compose") and q["code_challenge_method"] == "S256"
        threading.Thread(target=lambda: urllib.request.urlopen(f"{q['redirect_uri']}/?code=c0de&state={q['state']}").read()).start()

    tok = a.browser_login(open_browser=browser, echo=lambda m: None, timeout=5)
    assert tok["access_token"] == "gt" and a.access_token() == "gt"
    form = api.calls[0].content.decode()
    assert "code=c0de" in form and "code_verifier=" in form
