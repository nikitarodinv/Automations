# Autonetworker

Turns a campaign file (keywords, companies, filters) into personalized networking email
drafts waiting for your review. Backend for the `autonetworker` OpenClaw skill. **It never sends
email.** Full design: the Autonetworker technical spec (Nov 21, 2026).

## Status: milestone 1, the offline core

Everything that runs without spending credits works now:

| Area | Milestone 1 | Later |
| --- | --- | --- |
| Campaign files | Schema validation, defaults, `autonet validate` | |
| Stage 1, discovery | `csv` source (your own list, or LinkedIn's Connections.csv export), local filters, qualification scoring, per-company caps, suppression, re-contact cooldown, EU/UK excluded by default | `pool`, `pdl`, `apollo`, `hunter_domain`, `urls` (M2) |
| Stage 2, template | Rules selector (Jev's fallback rule), `requires` checks in code, decision cache; Jev's decision thresholds implemented as `apply_jev_policy` | Jev client (M2) |
| Stage 3, email | Accept rules, 90-day lookup reuse, addresses from your CSV, budget guard and ledger, lookups capped to draft slots | Hunter + finder waterfall (M2) |
| Stage 4, research | Your notes (tier-0 hooks) | Web research, Jev re-check (M3) |
| Stage 5, compose | Templates, phrase bank, deterministic phrase picking, colleague dedupe, `autonet lint phrases` | |
| Stage 6, draft | All lint checks, signature, opt-out line | Optional polish (M3) |
| Delivery | `eml` (opens in Outlook as a draft), `markdown`, `outlook_draft` (jsonl + ack) | `graph_draft`, `gmail_draft` (M3) |

`--dry-run` uses fake prospects and fake finder results from `fixtures/dryrun/`, with a separate
database (`data/dryrun.db`). It makes no network calls.

## Quick start

```bash
uv sync
uv run autonet lint phrases
uv run autonet validate ib-alumni-fall26
uv run autonet run ib-alumni-fall26 --dry-run --fresh     # 10 drafts → out/dry_*/review.md
uv run pytest -q
```

Tune `phrases/networking.yaml` and `templates/*.md.j2`, then rerun the dry run with `--fresh` to see
the new drafts. Golden snapshots in `tests/golden/` show every change as a diff; accept an intended
change with `UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py`.

## A real run (milestone 1)

1. Put people in `lists/contacts.csv` (gitignored). Columns are matched by name; see
   `lists/example_contacts.csv`. LinkedIn's Connections.csv works as is.
2. Without a finder yet, only rows with an `Email Address` get drafted. Everyone else waits in
   `pending_email` until milestone 2 adds Hunter.
3. `uv run autonet run ib-alumni-fall26`. Drafts land in `out/<run_id>/eml/`, plus a `review.md` with
   each draft's audit trail.
4. After you send, `uv run autonet mark-sent <draft_id>` starts that person's 180-day cooldown.

## Everyday commands

```text
autonet review [--full]                       what needs you
autonet note <draft_id> "<sentence>"          add your own opener, rebuild the draft (before delivery)
autonet deliver [campaign]                    deliver drafts waiting at "drafted"
autonet set-email <campaign> <prospect> <email>
autonet pick-template <campaign> <prospect> <template>
autonet mark-sent <id> | --all-delivered      autonet mark-skip <id>
autonet suppress <email|prospect_id>          never draft to them again
autonet status · autonet ledger
```

Add `--dry-run` to any of them to work against the dry-run database.

## Layout

```text
autonet/          cli, pipeline (stages), campaign, matching, selector, emails, compose, draft,
                  store (SQLite), ledger, providers/ (csv, fixture), delivery/ (eml, markdown, outlook_mcp)
schemas/          campaign, phrase_bank, template, hook (.schema.json)
campaigns/        ib-alumni-fall26.yaml
templates/        alumni_coffee_chat, industry_question, referral_ask, saw_your_work (.md.j2)
phrases/          networking.yaml
fixtures/dryrun/  fake prospects and finder results for --dry-run
skill/SKILL.md    copy to ~/.openclaw/workspace/skills/autonetworker/
tests/            unit/, test_pipeline.py (acceptance checks), test_golden.py, golden/
```

## Decisions made in milestone 1 where the spec left room

- **Prospect states.** Main path: `discovered → qualified → selected → email_found → researched →
  drafted → delivered → sent`. Waiting: `pending_email`. Exits: `review` (returns once you act),
  `skipped`, `suppressed` (permanent). People who fail filters stay `discovered` with the reason, so
  editing the campaign re-evaluates them next run.
- **Seniority** is a scoring penalty (−5) locally, as in section 4, not a hard filter.
- **Phrase variables.** A phrase is skipped if any variable it uses is empty, even one missing from
  its `requires`; `lint phrases` warns about the missing declaration.
- **Subjects with `max_chars`** that would overflow for a given person are skipped for that person,
  so lint only warns about them.
- **A required hook slot** (as in `saw_your_work`) fails the draft when there's no hook, rather than
  rendering an opener that refers to nothing.
- **Fixtures live in `fixtures/`**, not `tests/fixtures/`, because `--dry-run` is a product feature.
- **The sample campaign** uses `source: csv` and `delivery: eml` until milestones 2 and 3 land.
