"""Stage 3 decision rules (section 6). The finder adapters map results onto EmailResult;
this module decides what to do with them."""

from __future__ import annotations

from typing import Any

from .models import EmailResult

ACCEPTED = {"accepted", "accepted_catch_all", "confirmed", "manual"}


def decide(r: EmailResult, rules: dict[str, Any]) -> str:
    """Return one of: accepted, accepted_catch_all, unconfirmed, not_found, invalid, claimed."""
    if r.status == "claimed":
        return "claimed"
    if r.status == "not_found" or not r.email:
        return "not_found"
    if r.status == "invalid":
        return "invalid"
    score = r.score or 0
    accept = set(rules.get("accept_statuses", ["valid"]))
    if r.status == "valid" and "valid" in accept and score >= rules.get("min_score", 80):
        return "accepted"
    # Catch-all domains are accepted only above the stricter min_score_accept_all.
    if r.status == "accept_all" and score >= rules.get("min_score_accept_all", 92):
        return "accepted_catch_all"
    return "unconfirmed"
