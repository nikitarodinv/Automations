"""outlook_draft: the CLI writes drafts.jsonl; the OpenClaw skill creates each draft through
the mail connection's create-draft tool, then acks the returned IDs with `autonet deliver --ack`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .base import Delivered, Delivery


class OutlookMcpDelivery(Delivery):
    name = "outlook_draft"

    def deliver(self, drafts: list[dict[str, Any]]) -> list[Delivered]:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path = self.out_dir / "drafts.jsonl"
        with open(path, "w") as f:
            for d in drafts:
                f.write(json.dumps({"draft_id": d["draft_id"], "to": d["to_email"], "subject": d["subject"], "body": d["body"]}, ensure_ascii=False) + "\n")
        return [Delivered(d["draft_id"], None, "drafted") for d in drafts]


def read_acks(path: Path) -> list[tuple[str, str]]:
    """Each line: {"draft_id": "...", "mailbox_draft_id": "..."}."""
    out = []
    for line in path.read_text().splitlines():
        if line.strip():
            rec = json.loads(line)
            out.append((rec["draft_id"], rec.get("mailbox_draft_id") or rec.get("id")))
    return out
