"""gmail_draft: drafts.create through the Gmail API. Gmail has no drafts-only scope; gmail.compose
could also send a draft, but this code never calls drafts.send or messages.send."""

from __future__ import annotations

import base64
from email.message import EmailMessage
from email.policy import SMTP
from typing import Any

from ..errors import AuthError, ConfigError
from ..http import ApiClient, RateLimiter
from .base import Delivered, Delivery

API = "https://gmail.googleapis.com/gmail/v1/users/me/drafts"


def raw_message(d: dict[str, Any]) -> str:
    msg = EmailMessage(policy=SMTP)
    msg["To"] = d["to_email"]
    msg["Subject"] = d["subject"]
    msg.set_content(d["body"])
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


class GmailDelivery(Delivery):
    name = "gmail_draft"

    def __init__(self, out_dir: Any, auth: Any):
        super().__init__(out_dir)
        self.auth = auth

    def deliver_one(self, d: dict[str, Any]) -> Delivered:
        token = self.auth.access_token()
        client = ApiClient(service="gmail", headers={"Authorization": f"Bearer {token}"},
                           errors={"401": "auth", "403": "auth", "429": "transient", "5xx": "transient", "400": "data"},
                           limiter=RateLimiter(per_second=4), secrets=[token])
        try:
            r = client.request("POST", API, json={"message": {"raw": raw_message(d)}})
        except AuthError as e:
            raise ConfigError(f"Gmail refused the draft ({e}); run `autonet auth google`") from e
        did = (r.body or {}).get("id") if isinstance(r.body, dict) else None
        if not did:
            raise ConfigError("Gmail accepted the draft but returned no id")
        return Delivered(d["draft_id"], did, "delivered")

    def deliver(self, drafts: list[dict[str, Any]]) -> list[Delivered]:
        return [self.deliver_one(d) for d in drafts]
