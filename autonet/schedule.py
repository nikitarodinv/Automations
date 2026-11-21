"""Scheduled runs through OpenClaw automations (docs checked 2026-11-21).

A command job runs `autonet run <campaign> --scheduled` on the Gateway host with no model call and
posts its stdout to your chat channel; `NO_REPLY` posts nothing. `delivery: outlook_draft` needs the
agent to call the mail tool, so it gets an agent-turn job instead."""

from __future__ import annotations

import re
import shlex
import shutil
from dataclasses import dataclass
from typing import Any

from .approvals import approval_status
from .campaign import Campaign
from .config import Config
from .errors import ConfigError
from .store import Store

JEV_GATE = 0.85
JEV_MIN_CASES = 30
JOB_PREFIX = "autonetworker:"
_FIELD = re.compile(r"^(\*|\d+(-\d+)?)(/\d+)?(,(\*|\d+(-\d+)?)(/\d+)?)*$")
_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]


def validate_cron(expr: str) -> list[str]:
    """Five-field cron (minute hour day month weekday), numbers, ranges, lists and steps."""
    fields = expr.split()
    if len(fields) != 5:
        return [f"schedule '{expr}': needs 5 fields (minute hour day month weekday)"]
    errs = []
    for f, (lo, hi), name in zip(fields, _RANGES, ["minute", "hour", "day", "month", "weekday"]):
        if not _FIELD.match(f):
            errs.append(f"schedule {name} '{f}' isn't a cron field")
            continue
        for n in re.findall(r"\d+", re.sub(r"/\d+", "", f)):
            if not lo <= int(n) <= hi:
                errs.append(f"schedule {name} {n} is outside {lo}-{hi}")
    return errs


@dataclass
class GateResult:
    ok: bool
    reasons: list[str]


def scheduled_gates(cfg: Config, store: Store, campaign: Campaign) -> GateResult:
    """What must be true before a campaign runs unattended."""
    reasons = []
    sc = cfg.data.get("scheduling") or {}
    jev_on = (cfg.data.get("jev") or {}).get("provider", "off") not in (None, False, "off")
    if jev_on and sc.get("require_jev_eval", True):
        ev = store.latest_eval("jev")
        if not ev:
            reasons.append(f"run `autonet eval jev` on {JEV_MIN_CASES}+ labeled cases first (agreement must reach {JEV_GATE:.0%})")
        elif ev["cases"] < JEV_MIN_CASES:
            reasons.append(f"the last Jev eval had {ev['cases']} cases; {JEV_MIN_CASES}+ are needed")
        elif (ev["agreement"] or 0) < JEV_GATE:
            reasons.append(f"Jev agreement is {ev['agreement']:.0%} (needs {JEV_GATE:.0%}); keep reviewing picks by hand")
    approved, changes = approval_status(cfg, store, campaign)
    if not approved:
        reasons.append("not approved since: " + "; ".join(changes) + f" (preview with --dry-run, then `autonet approve {campaign.id}`)")
    return GateResult(not reasons, reasons)


def openclaw_command(cfg: Config, campaign: Campaign, *, channel: str | None, to: str | None, tz: str | None,
                     agent: str | None = None) -> list[str]:
    schedule = campaign.get("schedule")
    if not schedule:
        raise ConfigError(f"campaign {campaign.id} has no `schedule:` (cron syntax, e.g. \"30 8 * * 1-5\")")
    errs = validate_cron(schedule)
    if errs:
        raise ConfigError("; ".join(errs))
    uv = shutil.which("uv") or "uv"
    standard = cfg.root / "campaigns" / f"{campaign.id}.yaml"
    ref = campaign.id if not campaign.path or campaign.path.resolve() == standard.resolve() else str(campaign.path.resolve())
    run = [uv, "run", "--project", str(cfg.root), "autonet", "run", ref, "--scheduled"]
    argv = ["openclaw", "automations", "create", schedule, "--name", f"{JOB_PREFIX} {campaign.id}"]
    if campaign["delivery"] == "outlook_draft":
        # The mail connection's create-draft tool is only reachable from an agent turn.
        argv += ["--message", f"/autonetworker run {ref} --scheduled", "--session", "isolated"]
        if agent:
            argv += ["--agent", agent]
    else:
        argv += ["--command-argv", __import__("json").dumps(run), "--command-cwd", str(cfg.root)]
    if tz:
        argv += ["--tz", tz]
    if channel or to:
        argv += ["--announce"] + (["--channel", channel] if channel else []) + (["--to", to] if to else [])
    return argv


def shell_line(argv: list[str]) -> str:
    return " ".join(shlex.quote(a) for a in argv)


def nothing_to_report(report: Any) -> bool:
    c = report.counts
    return not (c["drafted"] or c["delivered"] or report.needs_you or report.errors or c["blocked"])
