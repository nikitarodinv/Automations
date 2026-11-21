"""One .eml file per draft with X-Unsent: 1, so Outlook opens it as an editable draft."""

from __future__ import annotations

from email.message import EmailMessage
from email.policy import SMTP
from typing import Any

from .base import Delivered, Delivery


def build_eml(d: dict[str, Any]) -> bytes:
    msg = EmailMessage(policy=SMTP)
    msg["To"] = d["to_email"]
    msg["Subject"] = d["subject"]
    msg["X-Unsent"] = "1"
    msg["X-Autonet-Draft-Id"] = d["draft_id"]
    msg.set_content(d["body"])
    return msg.as_bytes()


class EmlDelivery(Delivery):
    name = "eml"

    def deliver(self, drafts: list[dict[str, Any]]) -> list[Delivered]:
        folder = self.out_dir / "eml"
        folder.mkdir(parents=True, exist_ok=True)
        out = []
        for d in drafts:
            path = folder / f"{d['draft_id']}.eml"
            path.write_bytes(build_eml(d))
            out.append(Delivered(d["draft_id"], str(path), "delivered"))
        return out
