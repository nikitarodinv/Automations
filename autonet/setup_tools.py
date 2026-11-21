"""`autonet skill install`, `autonet campaign new`, `autonet doctor`."""

from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .config import Config
from .errors import AutonetError, ConfigError

DEFAULT_SKILL_DIR = Path("~/.openclaw/workspace/skills/autonetworker").expanduser()
PROJECT_PLACEHOLDER = "~/Autonetworker/Automations"
KEY_ENVS = ["HUNTER_API_KEY", "TYPESAFE_API_KEY", "PDL_API_KEY", "APOLLO_API_KEY", "PROSPEO_API_KEY", "BRAVE_API_KEY",
            "ANTHROPIC_API_KEY"]


def _tilde(p: Path) -> str:
    home = Path.home()
    try:
        return "~/" + str(p.resolve().relative_to(home))
    except ValueError:
        return str(p)


def render_skill(cfg: Config) -> str:
    src = (cfg.root / "skill" / "SKILL.md").read_text()
    return src.replace(PROJECT_PLACEHOLDER, _tilde(cfg.root))


def install_skill(cfg: Config, target_dir: Path | None = None, force: bool = False) -> tuple[Path, bool]:
    target = (target_dir or DEFAULT_SKILL_DIR) / "SKILL.md"
    text = render_skill(cfg)
    if target.exists():
        if target.read_text() == text:
            return target, False
        if not force:
            raise ConfigError(f"{target} exists and differs; pass --force to replace it")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    return target, True


def openclaw_env_snippet() -> str:
    env = {k: f"<your {k}>" for k in KEY_ENVS}
    return yaml.safe_dump({"skills": {"entries": {"autonetworker": {"enabled": True, "env": env}}}}, sort_keys=False)


def new_campaign(cfg: Config, campaign_id: str, base: str = "ib-alumni-fall26") -> Path:
    import re

    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", campaign_id):
        raise ConfigError("campaign id: lowercase letters, digits, - and _ only")
    dst = cfg.root / "campaigns" / f"{campaign_id}.yaml"
    if dst.exists():
        raise ConfigError(f"{dst} already exists")
    src = cfg.root / "campaigns" / f"{base}.yaml"
    text = src.read_text()
    text = re.sub(r"^id: .*$", f"id: {campaign_id}", text, count=1, flags=re.M)
    text = re.sub(r"^description: .*$", "description: TODO describe who this campaign reaches", text, count=1, flags=re.M)
    text = re.sub(r"^schedule: .*$", "# schedule: \"30 8 * * 1-5\"", text, flags=re.M)
    dst.write_text(text)
    return dst


@dataclass
class Check:
    level: str   # ok | warn | fail
    what: str
    detail: str = ""


def doctor(cfg: Config) -> list[Check]:
    """Offline health check: nothing here calls a paid API."""
    from .campaign import load_campaign
    from .delivery.auth import google_from_config, microsoft_from_config
    from .pipeline import check_campaign, open_store
    from .providers import manifests
    from .quality import quality_report
    from .schedule import scheduled_gates, validate_cron

    out: list[Check] = []
    store = open_store(cfg, False)
    out.append(Check("ok", "database", str(cfg.path("db"))))
    for path in sorted((cfg.root / "campaigns").glob("*.yaml")):
        try:
            camp = load_campaign(path)
            _, _, warns = check_campaign(cfg, camp)
            out.append(Check("warn" if warns else "ok", f"campaign {camp.id}", "; ".join(warns)))
            if camp.get("schedule"):
                errs = validate_cron(camp["schedule"])
                gate = scheduled_gates(cfg, store, camp)
                out.append(Check("fail" if errs else ("ok" if gate.ok else "warn"), f"schedule {camp.id}",
                                 "; ".join(errs + gate.reasons) or f"\"{camp['schedule']}\" ready to run unattended"))
        except AutonetError as e:
            out.append(Check("fail", f"campaign {path.stem}", str(e)))
        except Exception as e:  # ValidationFailed and friends
            out.append(Check("fail", f"campaign {path.stem}", str(e).splitlines()[0]))
    for name, m in manifests().items():
        out.append(Check("ok" if m.key() else "warn", f"provider {name}", "key set" if m.key() else f"{m.key_env} not set"))
    jev = (cfg.data.get("jev") or {}).get("provider", "off")
    if jev not in (None, False, "off"):
        from .jev import ROUTES

        env = ROUTES.get(jev, {}).get("key_env", "")
        out.append(Check("ok" if os.environ.get(env) else "warn", f"jev ({jev})", "key set" if os.environ.get(env) else f"{env} not set"))
        ev = store.latest_eval("jev")
        out.append(Check("ok" if ev and ev["cases"] >= 30 and (ev["agreement"] or 0) >= 0.85 else "warn", "jev eval",
                         f"{ev['agreement']:.0%} on {ev['cases']} cases" if ev and ev["agreement"] is not None else "not run yet"))
    for label, build in (("microsoft", microsoft_from_config), ("google", google_from_config)):
        try:
            a = build(cfg)
            tok_path = a.cache.path if a.cache else None
            if tok_path and tok_path.exists():
                mode = stat.S_IMODE(tok_path.stat().st_mode)
                out.append(Check("ok" if mode == 0o600 else "fail", f"mailbox {label}",
                                 f"signed in; token file mode {oct(mode)}" + ("" if mode == 0o600 else " (should be 0o600)")))
        except ConfigError:
            pass
    skill = DEFAULT_SKILL_DIR / "SKILL.md"
    if skill.exists():
        out.append(Check("ok" if skill.read_text() == render_skill(cfg) else "warn", "openclaw skill",
                         str(skill) if skill.read_text() == render_skill(cfg) else "installed copy differs; run `autonet skill install --force`"))
    else:
        out.append(Check("warn", "openclaw skill", "not installed; run `autonet skill install`"))
    out.append(Check("ok" if shutil.which("openclaw") else "warn", "openclaw CLI",
                     "found" if shutil.which("openclaw") else "not on PATH (needed for `autonet schedule install --apply`)"))
    q = quality_report(store)
    out.append(Check({"passed": "ok", "failed": "fail"}.get(q["status"], "warn"), "draft quality gate",
                     f"{q['sendable']} of {q['reviewed']} reviewed drafts sendable (need {q['needed']} of {q['first']})"))
    return out
