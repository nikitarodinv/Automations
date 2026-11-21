"""Global config (config.yaml) and project-root discovery."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "paths": {
        "db": "data/autonet.db",
        "dry_run_db": "data/dryrun.db",
        "out": "out",
        "logs": "logs",
        "templates": "templates",
        "signatures": "signatures.yaml",
    },
    "jev": {"provider": "off", "min_confidence": 0.6},
    "limits": {"daily_draft_cap": 25},
    "draft": {
        "polish": False,
        "include_opt_out_line": True,
        "opt_out_line": "If you'd rather I not follow up, just let me know.",
    },
    "budgets": {"hunter_monthly": 50, "provider_monthly": {}, "search_monthly": 1000},
    "retention_days": 365,
    "dry_run": {
        "prospects": "fixtures/dryrun/prospects.jsonl",
        "emails": "fixtures/dryrun/emails.yaml",
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def find_root(start: Path | None = None) -> Path:
    """AUTONET_HOME, else the nearest folder with config.yaml, else the repo this package lives in."""
    env = os.environ.get("AUTONET_HOME")
    if env:
        return Path(env).expanduser().resolve()
    here = (start or Path.cwd()).resolve()
    for d in [here, *here.parents]:
        if (d / "config.yaml").is_file() and (d / "autonet").is_dir():
            return d
    return Path(__file__).resolve().parents[1]


@dataclass
class Config:
    root: Path
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, root: Path | None = None, path: Path | None = None) -> "Config":
        root = (root or find_root()).resolve()
        cfg_path = path or root / "config.yaml"
        raw = {}
        if cfg_path.is_file():
            raw = yaml.safe_load(cfg_path.read_text()) or {}
        return cls(root=root, data=_merge(DEFAULTS, raw))

    def path(self, key: str) -> Path:
        p = Path(self.data["paths"][key])
        return p if p.is_absolute() else self.root / p

    def resolve(self, rel: str | Path) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else self.root / p

    def __getitem__(self, key: str) -> Any:
        return self.data[key]
