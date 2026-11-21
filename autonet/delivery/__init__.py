"""Delivery adapters. None of them can send mail: they write drafts for you to review."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..errors import ConfigError
from .eml import EmlDelivery
from .markdown import MarkdownDelivery, write_review
from .outlook_mcp import OutlookMcpDelivery

ADAPTERS = {"eml": EmlDelivery, "markdown": MarkdownDelivery, "outlook_draft": OutlookMcpDelivery}


def get_delivery(name: str, out_dir: Path, cfg: Any = None) -> Any:
    if name == "graph_draft":
        from .auth import microsoft_from_config
        from .graph import GraphDelivery

        return GraphDelivery(out_dir, microsoft_from_config(cfg))
    if name == "gmail_draft":
        from .auth import google_from_config
        from .gmail import GmailDelivery

        return GmailDelivery(out_dir, google_from_config(cfg))
    if name not in ADAPTERS:
        raise ConfigError(f"unknown delivery '{name}'")
    return ADAPTERS[name](out_dir)


__all__ = ["get_delivery", "write_review", "ADAPTERS"]
