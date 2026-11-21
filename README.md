# Autonetworker

Turns a campaign file (keywords, companies, filters) into personalized networking email
drafts waiting for your review. Backend for the `autonetworker` OpenClaw skill. **It never sends
email.** Full design: the Autonetworker technical spec (Nov 21, 2026).

## Status: milestones 1 and 2 done

| Area | Works now | Later |
| --- | --- | --- |
| Campaign files | Schema validation, defaults, `autonet validate` (also lists which providers lack keys) | |
| Stage 1, discovery | `pool` (PDL, then Apollo), `pdl`, `apollo`, `hunter_domain`, `csv`, `urls`; local filters, scoring, per-company caps, suppression, cooldown, EU/UK excluded by default; paging continues across runs | |
| Stage 2, template | Jev via TypeSafe, OpenRouter or Cloudflare; decision cache; rules fallback on errors, refusals or a missing key; `autonet eval jev` | |
| Stage 3, email | Finder waterfall (Hunter → Prospeo → Apollo), Hunter verifier with 202 polling and 222 retry next run, two-finder confirmation, 451 suppression, allowances with even pacing, run and monthly budgets | |
| Stage 4, research | Your notes (tier-0 hooks) | Web research, Jev identity check and re-check (M3) |
| Stage 5, compose | Templates, phrase bank, deterministic phrase picking, colleague dedupe, `autonet lint phrases` | |
| Stage 6, draft | All lint checks, signature, opt-out line | Optional polish (M3) |
| Delivery | `eml` (opens in Outlook as a draft), `markdown`, `outlook_draft` (jsonl + ack) | `graph_draft`, `gmail_draft` (M3) |
| Providers | YAML manifests, `@register` classes, entry-point plugins; `autonet provider list / new / record / test / smoke` | |

`--dry-run` uses fake prospects and fake finder results from `fixtures/dryrun/`, with a separate
database (`data/dryrun.db`). It makes no network calls unless you add `--live-jev`.

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

## Setting up providers

Put keys in the environment (for the OpenClaw skill: `~/.openclaw/openclaw.json` under
`skills.entries.autonetworker.env`). Any provider without a key is skipped and named in the report.

| Env var | Provider | Free plan | Used for |
| --- | --- | --- | --- |
| `HUNTER_API_KEY` | Hunter | 50 credits/month | Finding and verifying emails; `hunter_domain` source |
| `PDL_API_KEY` | People Data Labs | 100 records/month | Finding people (`pdl`), filling in `urls` |
| `APOLLO_API_KEY` | Apollo | 75 credits/month | Search is free; 1 credit per person enriched; also a finder |
| `PROSPEO_API_KEY` | Prospeo | 75 credits/month | Backup finder |
| `TYPESAFE_API_KEY` | Jev (TypeSafe) | pay per token, ~$0.001 a run | Template choice (or `OPENROUTER_API_KEY`, or `CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID`) |

Then:

```bash
uv run autonet provider list                    # keys found, credits left
uv run autonet provider smoke                   # live check with Hunter's free test key
uv run autonet validate ib-alumni-fall26
uv run autonet run ib-alumni-fall26 --estimate  # worst-case spend; spends nothing
uv run autonet run ib-alumni-fall26             # set source: pool in the campaign to use PDL/Apollo
```

The pool spends each free allowance evenly over the days left in the month (`providers.pace`),
keeps `providers.reserve` credits back for manual lookups, and trusts Hunter's own balance over the
ledger. Paid email lookups stop once the run has enough addresses for its draft slots.

## A run from your own list

1. Put people in `lists/contacts.csv` (gitignored). Columns are matched by name; see
   `lists/example_contacts.csv`. LinkedIn's Connections.csv works as is.
2. Rows with an `Email Address` are used as is; everyone else goes through the finder waterfall.
3. `uv run autonet run ib-alumni-fall26`. Drafts land in `out/<run_id>/eml/`, plus a `review.md` with
   each draft's audit trail.
4. After you send, `uv run autonet mark-sent <draft_id>` starts that person's 180-day cooldown.

## Adding a provider

```bash
uv run autonet provider new acme_finder --capability finder   # manifest, fixtures folder, contract test
# fill in providers/acme_finder.yaml from the provider's docs (providers/prospeo.yaml is a complete example)
uv run autonet provider record acme_finder --first Jordan --last Lee --domain example.com   # ≤3 credits
uv run autonet provider test acme_finder                      # shared contract suite, offline
```

Then add it to `providers.finders` (or `sources`) in `config.yaml`, in the position you want it tried.
APIs the manifest can't describe get a short class with `@register("name")`; see `autonet/providers/hunter.py`.

## Measuring Jev

Copy `jev/eval/cases.example.jsonl` to `jev/eval/cases.jsonl`, add 30+ prospects labeled with the
template you would pick, and run `uv run autonet eval jev`. It reports agreement and calibration;
turn on scheduled runs once agreement reaches 85%, and pin `jev.pin_version` once picks look right.

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
autonet/          cli, pipeline (stages), campaign, matching, selector, jev, emails, compose, draft,
                  store (SQLite), ledger, pool, http, estimate, delivery/ (eml, markdown, outlook_mcp)
autonet/providers/ base, registry, manifest, generic_http, contract, tools,
                  hunter, pdl, apollo, csv_file, urls, fixture
providers/        hunter, pdl, apollo, prospeo (.yaml manifests)
schemas/          campaign, phrase_bank, template, hook, provider (.schema.json)
jev/eval/         cases.example.jsonl
campaigns/        ib-alumni-fall26.yaml
templates/        alumni_coffee_chat, industry_question, referral_ask, saw_your_work (.md.j2)
phrases/          networking.yaml
fixtures/dryrun/  fake prospects and finder results for --dry-run
skill/SKILL.md    copy to ~/.openclaw/workspace/skills/autonetworker/
tests/            unit/, test_pipeline.py (acceptance checks), test_pool_providers.py, test_jev.py,
                  test_golden.py, golden/, fixtures/providers/ (recorded replies for the contract suite)
```

## Decisions where the spec left room

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
- **The sample campaign** uses `source: csv` and `delivery: eml`; switch to `source: pool` once keys are set.

Milestone 2, checked against each provider's docs on 2026-11-21:

- **Apollo search hides last names**, LinkedIn URLs and domains, so the Apollo source pays 1 credit to
  enrich each person. It checks the free search result against your title and keyword filters first.
- **Prospeo has no email-finder endpoint**, only Enrich Person; its manifest uses that. Only the
  `VERIFIED` status is documented, so anything else goes to review.
- **No score from Apollo or Prospeo:** `verified` counts as 85, which clears `min_score` (80) but not
  the catch-all bar (92).
- **Hunter's no-result reply isn't documented**; both a 200 with `email: null` and a 404 count as a free miss.
- **`hunter_credit_budget` caps all paid email lookups** in a run (any finder), not only Hunter's.
- **Dry runs stay offline** (fixture finder) rather than calling Hunter's test key for every prospect,
  since the test key returns the same dummy address each time. `autonet provider smoke` does the
  live test-key check instead.
- **A missing or rejected Jev key** falls back to the rules selector for the run, with a note, instead
  of aborting: section 5's fallback rule takes precedence over the general auth-error rule.
- **A bare LinkedIn URL** (`source: urls`) that no enricher recognizes gets its name and company from
  Hunter's `linkedin_handle` lookup, and its template is picked again once that's known.
