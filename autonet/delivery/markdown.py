"""Markdown review queue: each draft with its audit details."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..util import mask_email
from .base import Delivered, Delivery


def render_draft_md(d: dict[str, Any], mask: bool = False) -> str:
    a = d.get("audit") or {}
    lint = d.get("lint") or {}
    to = mask_email(d.get("to_email")) if mask else d.get("to_email")
    lines = [
        f"## {d['draft_id']} · {a.get('name', '')} · {a.get('company', '')}",
        "",
        f"- **To:** {to or '(none)'}  (score {d.get('email_score')}, {a.get('email_decision', '')}, via {a.get('email_provider', '')})",
        f"- **Template:** {d['template_id']} v{d['template_version']} · selector `{a.get('selector')}` · {a.get('selector_reason', '')}",
        f"- **Phrases:** {', '.join(f'{k}={v}' for k, v in (d.get('phrase_ids') or {}).items())} (bank v{d['phrase_bank_version']})",
        f"- **Hook:** {a.get('hook_summary') or 'none (profile-based opener)'}",
        *([f"- **Source quote:** \"{a['hook_quote']}\""] if a.get("hook_quote") else []),
        *([f"- **Polish:** {'applied' if a['polish'].get('applied') else 'kept original: ' + (a['polish'].get('reason') or '')}"] if a.get("polish") else []),
        f"- **LinkedIn:** {a.get('linkedin_url') or 'n/a'}",
        f"- **State:** {d['state']}",
    ]
    if lint.get("blocks"):
        lines.append(f"- **Blocked:** {'; '.join(lint['blocks'])}")
    if lint.get("warnings"):
        lines.append(f"- **Warnings:** {'; '.join(lint['warnings'])}")
    lines += ["", f"**Subject:** {d['subject']}", "", "```text", d["body"] or "", "```", ""]
    return "\n".join(lines)


def write_review(path: Path, title: str, drafts: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    parts = [f"# {title}", "", f"{len(drafts)} draft(s)", ""] + [render_draft_md(d) for d in drafts]
    path.write_text("\n".join(parts))
    return path


class MarkdownDelivery(Delivery):
    name = "markdown"

    def deliver(self, drafts: list[dict[str, Any]]) -> list[Delivered]:
        path = write_review(self.out_dir / "review.md", f"Drafts · {self.out_dir.name}", drafts)
        return [Delivered(d["draft_id"], f"{path}#{d['draft_id']}", "delivered") for d in drafts]
