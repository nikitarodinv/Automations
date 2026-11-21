"""OAuth for mailbox drafts. Tokens live outside the repo (default ~/.config/autonetworker/),
file mode 600, and are never logged.

Microsoft: device-code flow (works for school tenants; shows a code to enter at microsoft.com/devicelogin).
Scope Mail.ReadWrite only: a token that also carries Mail.Send is refused.
Google: installed-app loopback flow with PKCE, scope gmail.compose."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from .. import http
from ..errors import AuthError, ConfigError

DEFAULT_DIR = Path("~/.config/autonetworker").expanduser()
MS_SCOPES = "https://graph.microsoft.com/Mail.ReadWrite offline_access"
GOOGLE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"


@dataclass
class TokenCache:
    path: Path

    def load(self) -> dict[str, Any] | None:
        if not self.path.is_file():
            return None
        try:
            return json.loads(self.path.read_text())
        except ValueError:
            return None

    def save(self, tok: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(tok, f)
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)


def _post_form(url: str, data: dict[str, str]) -> tuple[int, dict[str, Any]]:
    with httpx.Client(timeout=30, transport=http.TRANSPORT) as c:
        r = c.post(url, data=data)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {"error": r.text[:200]}


def _with_expiry(tok: dict[str, Any], old: dict[str, Any] | None = None) -> dict[str, Any]:
    out = dict(tok)
    out["expires_at"] = time.time() + float(tok.get("expires_in", 3600))
    if old and "refresh_token" not in out and old.get("refresh_token"):
        out["refresh_token"] = old["refresh_token"]
    return out


# ---------------------------------------------------------------------------- Microsoft
def _consent_hint(body: dict[str, Any]) -> str:
    desc = str(body.get("error_description", ""))
    if "AADSTS65001" in desc or "AADSTS90094" in desc or "consent" in desc.lower() or "admin" in desc.lower():
        return ("your school needs to approve this app. Request approval on the sign-in screen, "
                "or set `delivery: eml` (drafts open in Outlook, no permissions needed)")
    return desc.split("\r\n")[0] or body.get("error", "sign-in failed")


def _refuse_send_scope(tok: dict[str, Any]) -> None:
    granted = str(tok.get("scope", "")).lower()
    if "mail.send" in granted:
        raise ConfigError("the app registration grants Mail.Send; remove that permission so drafts can never be "
                          "sent automatically, then sign in again")


@dataclass
class MicrosoftAuth:
    client_id: str
    tenant: str = "organizations"
    cache: TokenCache | None = None

    @property
    def base(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0"

    def device_login(self, echo: Callable[[str], None] = print, max_wait: float = 900) -> dict[str, Any]:
        status, dc = _post_form(f"{self.base}/devicecode", {"client_id": self.client_id, "scope": MS_SCOPES})
        if status != 200:
            raise ConfigError(f"microsoft sign-in: {_consent_hint(dc)}")
        echo(dc.get("message") or f"Go to {dc['verification_uri']} and enter {dc['user_code']}")
        interval = float(dc.get("interval", 5))
        deadline = http.MONOTONIC() + min(max_wait, float(dc.get("expires_in", 900)))
        while http.MONOTONIC() < deadline:
            http.SLEEP(interval)
            status, tok = _post_form(f"{self.base}/token", {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": self.client_id, "device_code": dc["device_code"],
            })
            if status == 200 and tok.get("access_token"):
                _refuse_send_scope(tok)
                tok = _with_expiry(tok)
                if self.cache:
                    self.cache.save(tok)
                return tok
            err = tok.get("error")
            if err == "authorization_pending":
                continue
            if err == "slow_down":
                interval += 5
                continue
            if err in ("expired_token", "code_expired"):
                break
            raise ConfigError(f"microsoft sign-in: {_consent_hint(tok)}")
        raise ConfigError("microsoft sign-in timed out; run `autonet auth microsoft` again")

    def access_token(self) -> str:
        tok = self.cache.load() if self.cache else None
        if not tok:
            raise ConfigError("not signed in to Microsoft; run `autonet auth microsoft` once")
        if tok.get("expires_at", 0) - 60 > time.time():
            return tok["access_token"]
        if not tok.get("refresh_token"):
            raise ConfigError("Microsoft sign-in expired; run `autonet auth microsoft`")
        status, new = _post_form(f"{self.base}/token", {
            "grant_type": "refresh_token", "client_id": self.client_id,
            "refresh_token": tok["refresh_token"], "scope": MS_SCOPES,
        })
        if status != 200 or not new.get("access_token"):
            raise AuthError("microsoft", f"refresh failed ({_consent_hint(new)}); run `autonet auth microsoft`")
        _refuse_send_scope(new)
        new = _with_expiry(new, tok)
        if self.cache:
            self.cache.save(new)
        return new["access_token"]


# ---------------------------------------------------------------------------- Google
def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


@dataclass
class GoogleAuth:
    client_secrets: Path
    cache: TokenCache | None = None

    def _client(self) -> tuple[str, str]:
        if not self.client_secrets.is_file():
            raise ConfigError(f"Google client secrets not found at {self.client_secrets} "
                              "(create a 'Desktop app' OAuth client in Google Cloud and download its JSON)")
        data = json.loads(self.client_secrets.read_text())
        inst = data.get("installed") or data.get("web") or {}
        if not inst.get("client_id"):
            raise ConfigError(f"{self.client_secrets}: no installed.client_id")
        return inst["client_id"], inst.get("client_secret", "")

    def browser_login(self, open_browser: Callable[[str], Any] | None = None, timeout: float = 300,
                      echo: Callable[[str], None] = print) -> dict[str, Any]:
        import webbrowser

        cid, secret = self._client()
        verifier, challenge = _pkce()
        state = secrets.token_urlsafe(16)
        got: dict[str, str] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                q = parse_qs(urlsplit(self.path).query)
                got.update({k: v[0] for k, v in q.items()})
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"Signed in. You can close this tab and return to the terminal.")

            def log_message(self, *a: Any) -> None:
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        server.timeout = timeout
        redirect = f"http://127.0.0.1:{server.server_address[1]}"
        url = GOOGLE_AUTH + "?" + urlencode({
            "client_id": cid, "redirect_uri": redirect, "response_type": "code", "scope": GOOGLE_SCOPE,
            "access_type": "offline", "prompt": "consent", "code_challenge": challenge,
            "code_challenge_method": "S256", "state": state,
        })
        echo(f"Opening your browser to sign in to Google. If it doesn't open, visit:\n{url}")
        (open_browser or webbrowser.open)(url)
        server.handle_request()
        server.server_close()
        if got.get("state") != state or "code" not in got:
            raise ConfigError(f"google sign-in failed: {got.get('error', 'no authorization code received')}")
        status, tok = _post_form(GOOGLE_TOKEN, {
            "code": got["code"], "client_id": cid, "client_secret": secret, "redirect_uri": redirect,
            "grant_type": "authorization_code", "code_verifier": verifier,
        })
        if status != 200 or not tok.get("access_token"):
            raise ConfigError(f"google sign-in failed: {tok.get('error_description') or tok.get('error')}")
        tok = _with_expiry(tok)
        if self.cache:
            self.cache.save(tok)
        return tok

    def access_token(self) -> str:
        tok = self.cache.load() if self.cache else None
        if not tok:
            raise ConfigError("not signed in to Google; run `autonet auth google` once")
        if tok.get("expires_at", 0) - 60 > time.time():
            return tok["access_token"]
        cid, secret = self._client()
        status, new = _post_form(GOOGLE_TOKEN, {
            "grant_type": "refresh_token", "client_id": cid, "client_secret": secret,
            "refresh_token": tok.get("refresh_token", ""),
        })
        if status != 200 or not new.get("access_token"):
            raise AuthError("google", "refresh failed; run `autonet auth google`")
        new = _with_expiry(new, tok)
        if self.cache:
            self.cache.save(new)
        return new["access_token"]


def microsoft_from_config(cfg: Any) -> MicrosoftAuth:
    c = (cfg.data.get("mailbox") or {}).get("microsoft") or {}
    if not c.get("client_id"):
        raise ConfigError("set mailbox.microsoft.client_id in config.yaml (your app registration's ID)")
    path = Path(c.get("token_cache") or DEFAULT_DIR / "microsoft_token.json").expanduser()
    return MicrosoftAuth(c["client_id"], c.get("tenant", "organizations"), TokenCache(path))


def google_from_config(cfg: Any) -> GoogleAuth:
    c = (cfg.data.get("mailbox") or {}).get("google") or {}
    secrets_path = Path(c.get("client_secrets") or DEFAULT_DIR / "google_client_secret.json").expanduser()
    path = Path(c.get("token_cache") or DEFAULT_DIR / "google_token.json").expanduser()
    return GoogleAuth(secrets_path, TokenCache(path))
