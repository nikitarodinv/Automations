"""graph_draft: the CLI creates each draft itself through Microsoft Graph (POST /me/messages),
with no model in the loop. Needs Mail.ReadWrite only; nothing here can send."""

from __future__ import annotations

from typing import Any

from ..errors import AuthError, ConfigError
from ..http import ApiClient, RateLimiter
from .base import Delivered, Delivery

GRAPH = "https://graph.microsoft.com/v1.0"


class GraphDelivery(Delivery):
    name = "graph_draft"

    def __init__(self, out_dir: Any, auth: Any):
        super().__init__(out_dir)
        self.auth = auth

    def _client(self, token: str) -> ApiClient:
        return ApiClient(
            service="microsoft graph", headers={"Authorization": f"Bearer {token}"},
            errors={"401": "auth", "403": "auth", "429": "transient", "5xx": "transient", "400": "data"},
            limiter=RateLimiter(per_second=4), secrets=[token],
        )

    def deliver_one(self, d: dict[str, Any]) -> Delivered:
        msg = {
            "subject": d["subject"],
            "body": {"contentType": "Text", "content": d["body"]},
            "toRecipients": [{"emailAddress": {"address": d["to_email"]}}],
        }
        for attempt in range(2):
            try:
                r = self._client(self.auth.access_token()).request("POST", f"{GRAPH}/me/messages", json=msg)
                break
            except AuthError as e:
                if attempt == 0 and self.auth.cache:
                    tok = self.auth.cache.load() or {}
                    tok["expires_at"] = 0           # force one refresh, then retry
                    self.auth.cache.save(tok)
                    continue
                raise ConfigError(f"Graph refused the draft ({e}). If your school requires approval, request it "
                                  "or set `delivery: eml`") from e
        mid = (r.body or {}).get("id") if isinstance(r.body, dict) else None
        if not mid:
            raise ConfigError("Graph accepted the draft but returned no id")
        return Delivered(d["draft_id"], mid, "delivered")

    def deliver(self, drafts: list[dict[str, Any]]) -> list[Delivered]:
        return [self.deliver_one(d) for d in drafts]
