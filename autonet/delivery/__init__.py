"""Delivery adapters. None of them can send mail: they write drafts for you to review."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..errors import ConfigError
from .eml import EmlDelivery
from .markdown import MarkdownDelivery, write_review
from .outlook_mcp import OutlookMcpDelivery

ADAPTERS = {"eml": EmlDelivery, "markdown": MarkdownDelivery, "outlook_draft": OutlookMcpDelivery}
LATER = {
    "graph_draft": "Microsoft Graph drafts arrive in milestone 3; use delivery: eml for now",
    "gmail_draft": "Gmail drafts arrive in milestone 3; use delivery: eml for now",
}


def get_delivery(name: str, out_dir: Path) -> Any:
    if name in LATER:
        raise ConfigError(LATER[name])
    if name not in ADAPTERS:
        raise ConfigError(f"unknown delivery '{name}'")
    return ADAPTERS[name](out_dir)


__all__ = ["get_delivery", "write_review", "ADAPTERS"]
