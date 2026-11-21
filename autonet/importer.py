"""`autonet import-phrases phrases.xml`: convert an XML phrase list to the YAML phrase bank, once.

Accepted shapes (attributes optional):
  <phrases version="3">
    <category name="opener.shared_school">
      <phrase id="os1" weight="3" requires="school, company" tone="warm" seniority="entry junior"
              max_chars="60">I'm a {{sender_year}} at {{school}} ...</phrase>
    </category>
  </phrases>
A category may also be an element named after it (<greeting><phrase>…</phrase></greeting>), and tags
may be child elements (<tags><tone>warm</tone></tags>). Missing ids are generated."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError
from .schemas import ValidationFailed, schema_errors

RESERVED = {"id", "weight", "requires", "max_chars", "text", "name"}


def _list(v: str | None) -> list[str]:
    return [x for x in re.split(r"[,\s]+", v or "") if x]


def parse_xml(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    if re.search(r"<!DOCTYPE|<!ENTITY", raw, re.I):
        raise ConfigError(f"{path.name}: DOCTYPE/ENTITY declarations aren't accepted")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise ConfigError(f"{path.name}: not valid XML ({e})") from e
    cats: dict[str, list[dict[str, Any]]] = {}
    counters: dict[str, int] = {}
    for cat_el in root:
        name = cat_el.get("name") or cat_el.tag
        for ph in cat_el.iter("phrase"):
            text = re.sub(r"\s+", " ", "".join(ph.itertext() if ph.find("tags") is None else (ph.text or ""))).strip()
            prefix = re.sub(r"[^a-z]", "", name.split(".")[-1])[:3] or "p"
            counters[prefix] = counters.get(prefix, 0) + 1
            item: dict[str, Any] = {"id": ph.get("id") or f"{prefix}{counters[prefix]}", "text": text}
            if ph.get("requires"):
                item["requires"] = _list(ph.get("requires"))
            tags = {k: _list(v) for k, v in ph.attrib.items() if k not in RESERVED}
            tags_el = ph.find("tags")
            if tags_el is not None:
                for t in tags_el:
                    tags.setdefault(t.tag, []).extend(_list(t.text))
            if tags:
                item["tags"] = tags
            if ph.get("weight"):
                item["weight"] = float(ph.get("weight"))  # type: ignore[arg-type]
            if ph.get("max_chars"):
                item["max_chars"] = int(ph.get("max_chars"))  # type: ignore[arg-type]
            cats.setdefault(name, []).append(item)
    if not cats:
        raise ConfigError(f"{path.name}: no <phrase> elements found")
    variables = sorted({v for items in cats.values() for it in items for v in re.findall(r"\{\{\s*([\w.]+)\s*\}\}", it["text"])
                        if not v.startswith("hook.")} | {"hook.*"})
    return {"version": int(root.get("version", 1)), "variables": variables, "categories": cats}


def import_phrases(src: Path, dst: Path, force: bool = False) -> dict[str, Any]:
    if dst.exists() and not force:
        raise ConfigError(f"{dst} exists; pass --force to overwrite, or choose another --out")
    bank = parse_xml(src)
    errs = schema_errors("phrase_bank", bank)
    if errs:
        raise ValidationFailed(f"imported phrase bank from {src.name}", errs)
    header = f"# Imported from {src.name} by `autonet import-phrases`. Edit freely; run `autonet lint phrases`.\n"
    dst.write_text(header + yaml.safe_dump(bank, sort_keys=False, allow_unicode=True, width=110))
    return bank
