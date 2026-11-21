"""Small helpers shared across stages: hashing, normalization, time, masking."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

_LEGAL_SUFFIXES = re.compile(
    r"[,\s]+(inc|incorporated|llc|l\.l\.c|ltd|limited|plc|corp|corporation|co|company|lp|llp|"
    r"gmbh|ag|sa|nv|bv|holdings|group)\.?$",
    re.IGNORECASE,
)


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime | None = None) -> str:
    return (dt or now_utc()).isoformat().replace("+00:00", "Z")


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def short_hash(*parts: object, length: int = 8) -> str:
    h = hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()
    return h[:length]


def stable_json(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def normalize_linkedin_url(url: str) -> str:
    """Lowercase, drop scheme differences, query string, fragment and trailing slash."""
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url.lower())
    host = parts.netloc
    if host.endswith("linkedin.com"):
        host = "www.linkedin.com"
    path = parts.path.rstrip("/")
    return f"https://{host}{path}"


def normalize_company(name: str) -> str:
    """Lowercase, strip punctuation noise and legal suffixes ("Inc.", "LLC", ...)."""
    n = name.strip().lower()
    prev = None
    while prev != n:
        prev = n
        n = _LEGAL_SUFFIXES.sub("", n).strip(" ,.")
    n = n.replace("&", " and ")
    n = re.sub(r"[^\w\s]", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def normalize_text(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def mask_email(email: str | None) -> str | None:
    if not email or "@" not in email:
        return email
    local, domain = email.split("@", 1)
    return f"{local[:1]}***@{domain}"


def word_count(text: str) -> int:
    return len(re.findall(r"\b[\w'’&-]+\b", text))
