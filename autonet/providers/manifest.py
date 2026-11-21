"""Provider manifests (providers/*.yaml): auth, free plan, rate limits, and for simple REST
APIs the whole request/response mapping. Adapters only translate; the pool owns the rest."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..errors import ConfigError
from ..http import ApiClient, RateLimiter
from ..schemas import ValidationFailed, schema_errors

MANIFEST_DIR = Path(__file__).resolve().parents[2] / "providers"


@dataclass
class Manifest:
    name: str
    capabilities: list[str]
    auth: dict[str, Any]
    free_plan: dict[str, Any] = field(default_factory=dict)
    rate_limit: dict[str, Any] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    error_codes: dict[str, str] = field(default_factory=dict)
    finder: dict[str, Any] | None = None
    source: dict[str, Any] | None = None
    enrich: dict[str, Any] | None = None
    docs: str = ""
    terms_checked: bool = False
    path: Path | None = None

    @property
    def key_env(self) -> str:
        return self.auth["env"]

    def key(self) -> str | None:
        return os.environ.get(self.key_env) or None

    def client(self, key: str | None = None, service: str | None = None) -> ApiClient:
        key = key or self.key()
        if not key:
            raise ConfigError(f"{self.name}: set {self.key_env} in the environment")
        headers: dict[str, str] = {}
        query: dict[str, str] = {}
        t = self.auth["type"]
        if t == "header":
            headers[self.auth.get("name", "X-API-Key")] = key
        elif t == "bearer":
            headers["Authorization"] = f"Bearer {key}"
        else:
            query[self.auth.get("name", "api_key")] = key
        rl = self.rate_limit or {}
        return ApiClient(
            service=service or self.name, headers=headers, query_auth=query, errors=dict(self.errors),
            limiter=RateLimiter(rl.get("per_second"), rl.get("per_minute")), secrets=[key],
        )


def parse_manifest(path: Path) -> Manifest:
    raw = yaml.safe_load(path.read_text()) or {}
    errs = schema_errors("provider", raw)
    if errs:
        raise ValidationFailed(f"provider manifest {path.name}", errs)
    if raw["name"] != path.stem:
        raise ValidationFailed(f"provider manifest {path.name}", [f"name '{raw['name']}' must match the file name"])
    keys = {f for f in Manifest.__dataclass_fields__ if f != "path"}
    return Manifest(path=path, **{k: v for k, v in raw.items() if k in keys})


def load_manifests(folder: Path | None = None) -> dict[str, Manifest]:
    folder = folder or MANIFEST_DIR
    out = {}
    if folder.is_dir():
        for p in sorted(folder.glob("*.yaml")):
            m = parse_manifest(p)
            out[m.name] = m
    return out
