"""Campaign files (campaigns/<id>.yaml): load, apply defaults, validate."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .schemas import ValidationFailed, schema_errors

DEFAULTS: dict[str, Any] = {
    "description": "",
    "targeting": {
        "keywords": {"any": [], "all": [], "exclude": []},
        "titles": {"include": [], "exclude": []},
        "seniority": [],
        "companies": {"include": [], "exclude": []},
        "locations": [],
        "schools": [],
        "allow_eu_uk": False,
    },
    "limits": {
        "max_prospects_per_run": 40,
        "max_drafts_per_run": 15,
        "max_per_company_per_run": 3,
        "hunter_credit_budget": 20,
        "llm_call_budget": 60,
        "search_budget": 45,
        "recontact_cooldown_days": 180,
    },
    "email": {"min_score": 80, "accept_statuses": ["valid"], "min_score_accept_all": 92},
    "research": {
        "enabled": False,
        "search_provider": "brave",
        "max_searches_per_prospect": 3,
        "max_pages": 10,
        "reselect": True,
        "blocked_domains": ["linkedin.com"],
    },
    "source": "pool",
    "source_options": {},
    "delivery": "eml",
    "review_before_delivery": False,
    "review_required": False,
}

# Implemented in milestone 1. Others validate but fail at run time with a clear message.
IMPLEMENTED_SOURCES = {"csv"}
IMPLEMENTED_DELIVERY = {"eml", "markdown", "outlook_draft"}


def _with_defaults(base: Any, raw: Any) -> Any:
    if isinstance(base, dict) and isinstance(raw, dict):
        out = copy.deepcopy(base)
        for k, v in raw.items():
            out[k] = _with_defaults(base.get(k), v) if k in base else v
        return out
    return copy.deepcopy(raw if raw is not None else base)


@dataclass
class Campaign:
    data: dict[str, Any]
    path: Path | None = None

    @property
    def id(self) -> str:
        return self.data["id"]

    @property
    def sender(self) -> dict[str, Any]:
        return self.data["sender"]

    @property
    def targeting(self) -> dict[str, Any]:
        return self.data["targeting"]

    @property
    def limits(self) -> dict[str, Any]:
        return self.data["limits"]

    @property
    def email(self) -> dict[str, Any]:
        return self.data["email"]

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


def parse_campaign(raw: dict[str, Any], path: Path | None = None) -> Campaign:
    what = f"campaign {path}" if path else "campaign"
    if not isinstance(raw, dict):
        raise ValidationFailed(what, ["file must contain a YAML mapping"])
    errs = schema_errors("campaign", raw)
    if errs:
        raise ValidationFailed(what, errs)
    data = _with_defaults(DEFAULTS, raw)
    extra = []
    allowed = data["templates"]["allowed"]
    if data["templates"]["fallback"] not in allowed:
        extra.append(f"templates.fallback: '{data['templates']['fallback']}' is not in templates.allowed")
    if data["source"] == "csv" and not data["source_options"].get("path"):
        extra.append("source_options.path: required when source is csv")
    if data["email"]["min_score_accept_all"] < data["email"]["min_score"]:
        extra.append("email.min_score_accept_all: should not be below email.min_score")
    if extra:
        raise ValidationFailed(what, extra)
    return Campaign(data=data, path=path)


def load_campaign(path: Path) -> Campaign:
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise ValidationFailed(f"campaign {path}", [f"YAML error: {e}"]) from e
    return parse_campaign(raw, path)


def resolve_campaign_path(root: Path, ref: str) -> Path:
    """Accept a campaign id (campaigns/<id>.yaml) or a path."""
    p = Path(ref)
    if p.suffix in (".yaml", ".yml"):
        return p if p.is_absolute() or p.exists() else root / p
    return root / "campaigns" / f"{ref}.yaml"
