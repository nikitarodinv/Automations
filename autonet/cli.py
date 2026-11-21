"""`autonet` command line. The OpenClaw skill calls these; you can too."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import typer

from .campaign import load_campaign, resolve_campaign_path
from .compose import load_phrase_bank, load_templates, lint_phrases
from .config import Config
from .errors import AutonetError, ConfigError, DataError
from .pipeline import (
    STAGES, Ctx, ack_deliveries, add_note, check_campaign, finish, make_ctx, mark, open_store, pick_template,
    run_campaign, run_stages, set_email, stage_deliver,
)
from .delivery.markdown import render_draft_md
from .schemas import ValidationFailed
from .store import Store

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Draft personalized networking emails. Never sends.")
lint_app = typer.Typer(no_args_is_help=True, help="Quality checks.")
app.add_typer(lint_app, name="lint")

DRY = typer.Option(False, "--dry-run", help="Use fixture prospects and the separate dry-run database. Nothing reaches a mailbox.")
DB = typer.Option(None, "--db", help="Override the database path.")
DEBUG = typer.Option(False, "--debug", help="Don't mask email addresses in logs.")


def _cfg() -> Config:
    return Config.load()


def _fail(e: Exception) -> None:
    kind = getattr(e, "kind", "error")
    typer.secho(f"{kind} error: {e}" if kind != "error" else str(e), fg=typer.colors.RED, err=True)
    raise typer.Exit(2 if kind in ("config", "auth") else 1)


def _guard(fn):  # type: ignore[no-untyped-def]
    import functools

    @functools.wraps(fn)
    def wrapper(*a, **kw):  # type: ignore[no-untyped-def]
        try:
            return fn(*a, **kw)
        except (AutonetError, ValidationFailed) as e:
            _fail(e)

    return wrapper


def _ctx_for(cfg: Config, campaign_id: str, dry_run: bool, db: Optional[Path], store: Store | None = None) -> Ctx:
    campaign = load_campaign(resolve_campaign_path(cfg.root, campaign_id))
    store = store or open_store(cfg, dry_run, db)
    return make_ctx(cfg, campaign, store, dry_run=dry_run)


# --------------------------------------------------------------------------- campaigns
@app.command()
@_guard
def validate(file: str = typer.Argument(..., help="Campaign id or path to campaigns/<id>.yaml"), dry_run: bool = DRY) -> None:
    """Check a campaign file, its templates, phrase bank and signature. Spends nothing."""
    cfg = _cfg()
    path = resolve_campaign_path(cfg.root, file)
    campaign = load_campaign(path)
    templates, bank, warnings = check_campaign(cfg, campaign, dry_run)
    errors, pw = lint_phrases(bank, {t: templates[t] for t in campaign["templates"]["allowed"]},
                              {"sender_school": campaign.sender["school"], "topic": campaign.sender["topic"],
                               "school": (campaign.targeting.get("schools") or [campaign.sender["school"]])[0]})
    for w in warnings + pw:
        typer.secho(f"warning: {w}", fg=typer.colors.YELLOW)
    if errors:
        raise ConfigError("phrase bank:\n  - " + "\n  - ".join(errors))
    typer.secho(f"ok: {path.name} · {len(campaign['templates']['allowed'])} templates · phrase bank v{bank.version}", fg=typer.colors.GREEN)


@app.command()
@_guard
def run(
    campaign: str,
    limit: Optional[int] = typer.Option(None, "--limit", help="At most this many drafts this run."),
    dry_run: bool = DRY,
    fresh: bool = typer.Option(False, "--fresh", help="With --dry-run: wipe the dry-run database first."),
    db: Optional[Path] = DB,
    debug: bool = DEBUG,
) -> None:
    """Run every stage in order and print the run report."""
    text, ctx = run_campaign(_cfg(), campaign, dry_run=dry_run, limit=limit, db=db, fresh=fresh, debug=debug)
    typer.echo(text)
    if (ctx.out_dir / "review.md").is_file():
        typer.echo(f"Drafts: {ctx.out_dir / 'review.md'}")


def _stage_command(name: str):  # type: ignore[no-untyped-def]
    @_guard
    def cmd(campaign: str, dry_run: bool = DRY, db: Optional[Path] = DB) -> None:
        ctx = _ctx_for(_cfg(), campaign, dry_run, db)
        typer.echo(run_stages(ctx, [name], deliver=False))

    cmd.__doc__ = f"Run only the {name} stage."
    return cmd


for _name in STAGES:
    app.command(name=_name)(_stage_command(_name))


@app.command()
@_guard
def deliver(
    campaign: Optional[str] = typer.Argument(None, help="Campaign id; default: every campaign with drafts waiting."),
    ack: Optional[Path] = typer.Option(None, "--ack", help="acks.jsonl from the skill: record mailbox draft IDs."),
    dry_run: bool = DRY,
    db: Optional[Path] = DB,
) -> None:
    """Save drafts that are waiting (state drafted) to the campaign's delivery target."""
    cfg = _cfg()
    store = open_store(cfg, dry_run, db)
    if ack:
        n = ack_deliveries(store, ack)
        typer.echo(f"recorded {n} mailbox draft(s)")
        return
    ids = [campaign] if campaign else sorted({d["campaign_id"] for d in store.drafts(states=["drafted"])})
    if not ids:
        typer.echo("nothing waiting to deliver")
    for cid in ids:
        ctx = _ctx_for(cfg, cid, dry_run, db, store)
        delivered = stage_deliver(ctx)
        typer.echo(finish(ctx) if delivered else f"{cid}: nothing waiting to deliver")


# --------------------------------------------------------------------------- review
@app.command()
@_guard
def review(
    campaign: Optional[str] = typer.Option(None, "--campaign"),
    full: bool = typer.Option(False, "--full", help="Print each draft's full text."),
    dry_run: bool = DRY,
    db: Optional[Path] = DB,
) -> None:
    """List what needs you: drafts waiting, blocked drafts, and prospects held for review."""
    store = open_store(_cfg(), dry_run, db)
    drafts = store.drafts(campaign, ["drafted", "blocked"])
    q = "SELECT * FROM campaign_prospects WHERE state='review'" + (" AND campaign_id=?" if campaign else "")
    held = store.db.execute(q, (campaign,) if campaign else ()).fetchall()
    if not drafts and not held:
        typer.echo("Nothing needs you.")
        return
    for d in drafts:
        if full:
            typer.echo(render_draft_md(d))
            continue
        a = d.get("audit") or {}
        flag = " [BLOCKED: " + "; ".join(d["lint"]["blocks"]) + "]" if d["state"] == "blocked" else ""
        warn = f" (warn: {'; '.join(d['lint']['warnings'])})" if d["lint"].get("warnings") else ""
        typer.echo(f"{d['draft_id']}  {a.get('name')} · {a.get('company')} · {d['template_id']} · {d['subject']!r}{flag}{warn}")
        if a.get("linkedin_url"):
            typer.echo(f"           {a['linkedin_url']}")
    reviewable = [h for h in held if not (h["reason"] or "").startswith("lint:")]
    if reviewable:
        typer.echo("\nHeld for review:")
        for h in reviewable:
            p = store.get_prospect(h["prospect_id"])
            name = f"{p.full_name} · {p.company.name}" if p else ""
            typer.echo(f"{h['prospect_id']}  {name} · {h['campaign_id']} · {h['reason']}")
        typer.echo("\nFix with: set-email <campaign> <prospect_id> <email> · pick-template <campaign> <prospect_id> <template> · mark-skip <prospect_id>")


@app.command()
@_guard
def note(draft_id: str, sentence: str, dry_run: bool = DRY, db: Optional[Path] = DB) -> None:
    """Add your own opening sentence (up to 40 words) to a draft and rebuild it."""
    cfg = _cfg()
    store = open_store(cfg, dry_run, db)
    d = store.get_draft(draft_id)
    if not d:
        raise DataError(f"no draft {draft_id}")
    ctx = _ctx_for(cfg, d["campaign_id"], dry_run, db, store)
    rec = add_note(ctx, draft_id, sentence)
    typer.echo(render_draft_md(rec))


@app.command("mark-sent")
@_guard
def mark_sent(
    ident: Optional[str] = typer.Argument(None, help="Draft id (d_…) or prospect id (p_…)"),
    all_delivered: bool = typer.Option(False, "--all-delivered"),
    dry_run: bool = DRY,
    db: Optional[Path] = DB,
) -> None:
    """Record that you sent a draft; starts that person's re-contact cooldown."""
    store = open_store(_cfg(), dry_run, db)
    if all_delivered:
        ids = [d["draft_id"] for d in store.drafts(states=["delivered"])]
    elif ident:
        ids = [ident]
    else:
        raise DataError("give a draft id or --all-delivered")
    changed = [x for i in ids for x in mark(store, i, "sent")]
    typer.echo(f"marked sent: {', '.join(changed) or 'none'}")


@app.command("mark-skip")
@_guard
def mark_skip(ident: str, dry_run: bool = DRY, db: Optional[Path] = DB) -> None:
    """Drop a draft or prospect from this campaign."""
    store = open_store(_cfg(), dry_run, db)
    typer.echo(f"skipped: {', '.join(mark(store, ident, 'skipped'))}")


@app.command()
@_guard
def suppress(key: str, reason: str = typer.Option("manual", help="opt_out | manual"), dry_run: bool = DRY, db: Optional[Path] = DB) -> None:
    """Never draft to this email address or prospect id again."""
    store = open_store(_cfg(), dry_run, db)
    kind = "prospect" if key.startswith("p_") else "email"
    if kind == "email" and "@" not in key:
        raise DataError(f"'{key}' is neither an email address nor a prospect id")
    store.suppress(key, kind, "opt_out" if reason == "opt_out" else reason)
    if kind == "email":
        # Any draft not yet sent to this address is dropped.
        for d in store.drafts(states=["drafted", "blocked", "delivered"]):
            if (d["to_email"] or "").lower() == key.lower():
                store.set_draft_state(d["draft_id"], "skipped")
                store.set_state(d["campaign_id"], d["prospect_id"], "suppressed", reason=reason)
    store.db.commit()
    typer.echo(f"suppressed {key}")


@app.command("set-email")
@_guard
def set_email_cmd(campaign: str, prospect_id: str, email: str, dry_run: bool = DRY, db: Optional[Path] = DB) -> None:
    """Supply an address for someone held as 'email unconfirmed'. The next run drafts them."""
    ctx = _ctx_for(_cfg(), campaign, dry_run, db)
    set_email(ctx, prospect_id, email)
    typer.echo(f"{prospect_id}: email set; run `autonet run {campaign}` to draft")


@app.command("pick-template")
@_guard
def pick_template_cmd(campaign: str, prospect_id: str, template: str, dry_run: bool = DRY, db: Optional[Path] = DB) -> None:
    """Choose the template yourself for a prospect held for review."""
    ctx = _ctx_for(_cfg(), campaign, dry_run, db)
    pick_template(ctx, prospect_id, template)
    typer.echo(f"{prospect_id}: template {template}; run `autonet run {campaign}` to draft")


# --------------------------------------------------------------------------- status
@app.command()
@_guard
def status(campaign: Optional[str] = typer.Argument(None), dry_run: bool = DRY, db: Optional[Path] = DB) -> None:
    """Counts by state for each campaign."""
    from .store import STATES

    store = open_store(_cfg(), dry_run, db)
    ids = [campaign] if campaign else [r[0] for r in store.db.execute("SELECT DISTINCT campaign_id FROM campaign_prospects")]
    if not ids:
        typer.echo("no campaigns have run yet")
    for cid in ids:
        counts = store.state_counts(cid)
        parts = [f"{s} {counts[s]}" for s in STATES if counts.get(s)]
        typer.echo(f"{cid}: " + (" · ".join(parts) or "empty"))


@app.command()
@_guard
def ledger(month: bool = typer.Option(True, "--month/--all"), dry_run: bool = DRY, db: Optional[Path] = DB) -> None:
    """Spend by provider."""
    from .util import iso, now_utc

    store = open_store(_cfg(), dry_run, db)
    q = "SELECT provider, SUM(credits) c, SUM(searches) s, SUM(llm_calls) l, COUNT(*) n FROM ledger"
    args: tuple = ()
    if month:
        q += " WHERE at>=?"
        args = (iso(now_utc().replace(day=1, hour=0, minute=0, second=0)),)
    rows = store.db.execute(q + " GROUP BY provider ORDER BY provider", args).fetchall()
    if not rows:
        typer.echo("no spend recorded")
    for r in rows:
        typer.echo(f"{r['provider']}: {r['c']:g} credits · {r['s']} searches · {r['l']} model calls · {r['n']} calls")


@lint_app.command("phrases")
@_guard
def lint_phrases_cmd(bank: Path = typer.Option(Path("phrases/networking.yaml"), "--bank")) -> None:
    """Check the phrase bank against every template."""
    cfg = _cfg()
    pb = load_phrase_bank(cfg.resolve(bank))
    templates = load_templates(cfg.path("templates"))
    errors, warnings = lint_phrases(pb, templates)
    for w in warnings:
        typer.secho(f"warning: {w}", fg=typer.colors.YELLOW)
    for e in errors:
        typer.secho(f"error: {e}", fg=typer.colors.RED)
    n = sum(len(v) for v in pb.categories.values())
    if errors:
        raise typer.Exit(1)
    typer.secho(f"ok: {n} phrases in {len(pb.categories)} categories · {len(templates)} templates", fg=typer.colors.GREEN)


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
