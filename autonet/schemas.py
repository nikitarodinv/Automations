"""JSON Schema loading and validation with readable error messages."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"


class ValidationFailed(Exception):
    def __init__(self, what: str, errors: list[str]):
        self.what = what
        self.errors = errors
        super().__init__(f"{what} failed validation:\n  - " + "\n  - ".join(errors))


@lru_cache(maxsize=None)
def _validator(name: str) -> Draft202012Validator:
    schema = json.loads((SCHEMA_DIR / f"{name}.schema.json").read_text())
    return Draft202012Validator(schema)


def schema_errors(name: str, data: object) -> list[str]:
    errs = sorted(_validator(name).iter_errors(data), key=lambda e: list(e.absolute_path))
    out = []
    for e in errs:
        loc = ".".join(str(p) for p in e.absolute_path) or "(top level)"
        out.append(f"{loc}: {e.message}")
    return out


def validate(name: str, data: object, what: str | None = None) -> None:
    errs = schema_errors(name, data)
    if errs:
        raise ValidationFailed(what or name, errs)
