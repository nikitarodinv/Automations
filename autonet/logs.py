"""Structured JSON-lines logging. Keys are never logged; emails are masked unless --debug."""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

from .util import iso, mask_email

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
_SECRET_KEYS = {"api_key", "key", "token", "authorization", "x-api-key", "password", "secret"}


class JsonLog:
    def __init__(self, folder: Path, debug: bool = False):
        self.folder = folder
        self.debug = debug

    def _scrub(self, v: Any) -> Any:
        if isinstance(v, dict):
            return {k: ("***" if k.lower() in _SECRET_KEYS else self._scrub(x)) for k, x in v.items()}
        if isinstance(v, list):
            return [self._scrub(x) for x in v]
        if isinstance(v, str) and not self.debug:
            return _EMAIL.sub(lambda m: mask_email(m.group(0)) or "", v)
        return v

    def write(self, **fields: Any) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        rec = {"at": iso(), **self._scrub(fields)}
        with open(self.folder / f"autonet-{date.today().isoformat()}.jsonl", "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
