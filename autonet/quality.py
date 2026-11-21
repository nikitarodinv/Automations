"""The acceptance gate from section 13: of the first 20 real drafts you review, at least 17 are
sendable as written or after light edits. You record each outcome with
`mark-sent <id> --edits none|light|heavy` or `mark-skip <id> --not-sendable`."""

from __future__ import annotations

from typing import Any

from .store import Store

FIRST_N = 20
NEEDED = 17
OUTCOMES = {"none": "sent as written", "light": "sent after light edits", "heavy": "sent after heavy edits",
            "not_sendable": "not sendable"}


def quality_report(store: Store, campaign_id: str | None = None, first: int = FIRST_N) -> dict[str, Any]:
    q = "SELECT draft_id, campaign_id, template_id, review_outcome, audit FROM drafts WHERE review_outcome IN ('none','light','heavy','not_sendable')"
    args: list[Any] = []
    if campaign_id:
        q += " AND campaign_id=?"
        args.append(campaign_id)
    rows = store.db.execute(q + " ORDER BY reviewed_at, draft_id LIMIT ?", (*args, first)).fetchall()
    counts = {k: 0 for k in OUTCOMES}
    by_template: dict[str, list[int]] = {}
    for r in rows:
        counts[r["review_outcome"]] += 1
        ok = r["review_outcome"] in ("none", "light")
        t = by_template.setdefault(r["template_id"], [0, 0])
        t[0] += ok
        t[1] += 1
    reviewed = len(rows)
    sendable = counts["none"] + counts["light"]
    needed = NEEDED * first // FIRST_N
    return {
        "reviewed": reviewed, "first": first, "sendable": sendable, "counts": counts,
        "by_template": by_template,
        "status": "passed" if reviewed >= first and sendable >= needed
        else "failed" if reviewed - sendable > first - needed else "in progress",
        "needed": needed,
    }
