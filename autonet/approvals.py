"""Approval gates (section 12): a new campaign, a changed campaign file, a changed phrase bank or
template, or `review_required: true` holds drafts at `drafted` until you approve in chat
(`autonet approve <campaign>`). The approval is a fingerprint of exactly the files that shape a draft."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .campaign import Campaign
from .config import Config
from .store import Store


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16] if path.is_file() else "missing"


def fingerprint_parts(cfg: Config, campaign: Campaign) -> dict[str, str]:
    parts = {}
    if campaign.path:
        parts["campaign"] = _digest(campaign.path)
    parts["phrase_bank"] = _digest(cfg.resolve(campaign["phrase_bank"]))
    tdir = cfg.path("templates")
    for t in sorted(campaign["templates"]["allowed"]):
        parts[f"template:{t}"] = _digest(tdir / f"{t}.md.j2")
    parts["signatures"] = _digest(cfg.path("signatures"))
    return parts


def fingerprint(parts: dict[str, str]) -> str:
    return hashlib.sha256(repr(sorted(parts.items())).encode()).hexdigest()[:16]


def approval_status(cfg: Config, store: Store, campaign: Campaign) -> tuple[bool, list[str]]:
    """(approved, what changed since the last approval)."""
    parts = fingerprint_parts(cfg, campaign)
    prev = store.get_approval(campaign.id)
    if prev is None:
        return False, ["new campaign: never approved"]
    if prev["fingerprint"] == fingerprint(parts):
        return True, []
    changed = [k for k in sorted(set(parts) | set(prev["parts"])) if parts.get(k) != prev["parts"].get(k)]
    return False, [f"{k} changed" for k in changed]


def approve(cfg: Config, store: Store, campaign: Campaign) -> dict[str, Any]:
    parts = fingerprint_parts(cfg, campaign)
    store.approve(campaign.id, fingerprint(parts), parts)
    return parts
