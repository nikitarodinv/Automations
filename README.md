# Autonetworker

Turns a campaign file (keywords, companies, filters) into personalized networking email
drafts waiting for your review. Backend for the `autonetworker` OpenClaw skill. **It never sends
email.** Full design: the Autonetworker technical spec (Nov 21, 2026).

## Status: milestones 1–4 done

| Area | Works now | Later |
| --- | --- | --- |
| Campaign files | Schema validation, defaults, `autonet validate` (also lists which providers lack keys) | |
| Stage 1, discovery | `pool` (PDL, then Apollo), `pdl`, `apollo`, `hunter_domain`, `csv`, `urls`; local filters, scoring, per-company caps, suppression, cooldown, EU/UK excluded by default; paging continues across runs | |
| Stage 2, template | Jev via TypeSafe, OpenRouter or Cloudflare; decision cache; rules fallback on errors, refusals or a missing key; `autonet eval jev` | |
| Stage 3, email | Finder waterfall (Hunter → Prospeo → Apollo), Hunter verifier with 202 polling and 222 retry next run, two-finder confirmation, 451 suppression, allowances with even pacing, run and monthly budgets | |
| Stage 4, research | Hunter's source pages + up to 3 web searches (Brave); robots.txt, 1 req/s per site, no LinkedIn, no login pages; name + company/school check, then Jev's identity check (≥0.8); one Claude extraction call; every hook verified in code (quote word for word, fields inside the quote, date, age limits, work topics only); Jev re-checks the template when a detail turns up; your notes (tier 0) | |
| Stage 5, compose | Templates, phrase bank, deterministic phrase picking, colleague dedupe, `autonet lint phrases` | |
| Stage 6, draft | All lint checks, signature, opt-out line; optional polish (one Claude call, rejected if it adds a name or number, changes >25%, or touches the greeting) | |
| Delivery | `eml`, `markdown`, `outlook_draft` (jsonl + ack), `graph_draft` (device-code sign-in, Mail.ReadWrite only), `gmail_draft` (browser sign-in) | |
| Providers | YAML manifests, `@register` classes, entry-point plugins; `autonet provider list / new / record / test / smoke` | |
| Rollout | Approval gates, scheduled runs through OpenClaw, Jev-eval gate, run lock, quality gate (17 of 20), retention, `import-phrases`, `campaign new`, `skill install`, `doctor` | Follow-ups, reply detection (v2) |

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
| `TYPESAFE_API_KEY` | Jev (TypeSafe) | pay per token, ~$0.001 a run | Template choice and page identity checks (or `OPENROUTER_API_KEY`, or `CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID`) |
| `BRAVE_API_KEY` | Brave Search | check your plan | Stage 4 web searches (without it, only Hunter's source pages are read) |
| `ANTHROPIC_API_KEY` | Claude (`claude-opus-5-5`) | pay per token | Stage 4 extraction and the optional polish (or `ant auth login`) |

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

## Research and polish

Set `research.enabled: true` in the campaign (and `BRAVE_API_KEY`). Each person you can email gets
up to 3 searches plus the pages Hunter found their address on; at most 10 pages are read, politely.
A detail only reaches a draft if its supporting quote is found word for word on the page, its fields
appear in that quote, its date parses and is recent enough (talks and writing 24 months; deals,
promotions and company news 180 days), and it's about work. `review.md` shows the source link and
quote for every detail, so you can check it before you send. When nothing qualifies, the email uses
the shared-school or shared-employer opener; research never blocks a draft. Cost: about 3 searches,
up to 10 Jev identity checks (fractions of a cent) and one Claude call per person.

`draft.polish: true` in `config.yaml` adds one Claude call per draft to smooth grammar. The result is
thrown away (and the original kept, with the reason in the audit) if it adds any name or number,
changes more than 25% of the text, runs over the length limits, or changes the greeting.

## Mailbox drafts

- **Outlook (school account):** register an app in Microsoft Entra (public client, delegated
  `Mail.ReadWrite` only, no `Mail.Send`), put its ID in `mailbox.microsoft.client_id`, set
  `delivery: graph_draft`, and run `uv run autonet auth microsoft` once. If your school shows
  "Approval required", request it there, or use `delivery: eml` (no permissions needed). A token
  that carries `Mail.Send` is refused.
- **Gmail:** create a "Desktop app" OAuth client in Google Cloud, save its JSON to
  `~/.config/autonetworker/google_client_secret.json`, set `delivery: gmail_draft`, and run
  `uv run autonet auth google`. Gmail has no drafts-only scope: `gmail.compose` could send a draft,
  but this code never calls a send endpoint (a test checks that).
- Tokens are cached in `~/.config/autonetworker/` with mode 600. `autonet auth status` shows them.
- Each mailbox draft ID is saved before the next draft is created, so a crash mid-delivery never
  creates the same draft twice.

## Going live (milestone 4)

1. **Install the skill:** `uv run autonet skill install` writes
   `~/.openclaw/workspace/skills/autonetworker/SKILL.md` with this project's path and prints the
   `openclaw.json` block for your keys.
2. **Check everything:** `uv run autonet doctor` (offline: config, campaigns, keys, Jev eval, mailbox
   token permissions, skill, schedule gates, quality gate).
3. **Approve:** a new campaign, or any change to its campaign file, phrase bank, templates or
   signature, holds drafts at `drafted`. Preview with `--dry-run --fresh`, then
   `uv run autonet approve <campaign>`; held drafts go out with `autonet deliver <campaign>`.
   `review_required: true` in a campaign holds every run for your approval.
4. **Measure quality:** for the first 20 real drafts, record each outcome with
   `mark-sent <id> --edits none|light|heavy` or `mark-skip <id> --not-sendable`. `autonet quality`
   shows progress against the gate (17 of 20 sendable as written or with light edits).
5. **Measure Jev:** `autonet eval jev` on 30+ labeled cases. Results are stored.
6. **Schedule:** add `schedule: "30 8 * * 1-5"` to the campaign, then
   `uv run autonet schedule install <campaign> --channel slack --to channel:C123 --tz America/Chicago`.
   It prints the `openclaw automations create …` command (command jobs need `operator.admin` on the
   Gateway); `--apply` runs it. Each run posts its report to your channel, posts nothing when there's
   nothing new (`NO_REPLY`), and skips itself, saying why, until the campaign is approved and Jev's
   last eval shows 85%+ agreement on 30+ cases (`scheduling.require_jev_eval: false` turns that gate off).
   `outlook_draft` campaigns get an agent-turn job instead, since only the agent can call the mail tool.

Only one run happens at a time (a lock next to the database). Retention runs at the start of every
real run, and on demand with `autonet prune [--dry-run]`: prospects never drafted go after 90 days,
everything else after `retention_days`; suppression entries are never removed.

Already keep phrases in XML? `uv run autonet import-phrases phrases.xml --out phrases/mine.yaml`.

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
autonet/          cli, pipeline (stages), campaign, matching, selector, jev, emails, research, search, fetch,
                  llm, compose, draft, store (SQLite), ledger, pool, http, estimate, approvals,
                  schedule, lock, retention, quality, importer, setup_tools,
                  delivery/ (eml, markdown, outlook_mcp, graph, gmail, auth)
autonet/providers/ base, registry, manifest, generic_http, contract, tools,
                  hunter, pdl, apollo, csv_file, urls, fixture
providers/        hunter, pdl, apollo, prospeo (.yaml manifests)
schemas/          campaign, phrase_bank, template, hook, provider (.schema.json)
jev/eval/         cases.example.jsonl
research/         extract_prompt.md
campaigns/        ib-alumni-fall26.yaml
templates/        alumni_coffee_chat, industry_question, referral_ask, saw_your_work (.md.j2)
phrases/          networking.yaml
fixtures/dryrun/  fake prospects and finder results for --dry-run
skill/SKILL.md    copy to ~/.openclaw/workspace/skills/autonetworker/
tests/            unit/, test_pipeline.py (acceptance checks), test_pool_providers.py, test_jev.py,
                  test_research.py, test_polish_mailbox.py, test_rollout.py,
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

Milestone 3:

- **The text model is Claude (`claude-opus-5-5`)** through the official SDK, with structured outputs
  (JSON that must fit a schema) and server-side refusal fallbacks on. Extraction runs at effort
  `medium`, polish at `low`.
- **Only excerpts around the person's name** (up to 6,000 characters a page) go to the extraction
  call, to keep cost down; the word-for-word quote check still runs on the full fetched text.
- **A hook needs a date.** If neither the model nor the page gives one that parses, the hook is
  dropped, since its age can't be checked. Future-dated events are dropped too.
- **The rules fallback uses research:** shared school → `alumni_coffee_chat`; otherwise a verified
  talk, article, podcast, deal or promotion → `saw_your_work`.
- **Robots.txt that can't be fetched (5xx or no connection) means no fetch** for that site, per RFC 9309.
- **Polish runs only on real runs**, and is reused when a draft is rebuilt with unchanged text.

Milestone 4 (the spec's build-order diagram didn't come through, so this is the rollout work its
other sections call for):

- **Approval is a fingerprint** of the campaign file, its phrase bank, its templates and the
  signature file. Any change holds drafts at `drafted` (dry runs excepted) and `deliver` refuses until
  `autonet approve`. The skill tells the agent to approve only on your explicit word in chat.
- **Scheduling uses OpenClaw's own scheduler** (a command job per campaign) rather than a scheduler of
  our own; the CLI only builds the job and enforces the gates at run time.
- **The Jev gate reads the latest stored eval**, so a newer, smaller eval re-closes it.
- **The quality gate counts only drafts you rated.** `mark-sent` without `--edits` isn't counted, so
  an unrated send can't inflate the rate; it fails as soon as 4 of the first 20 are unsendable.
- **Retention keeps contact history** for at least the re-contact cooldown, even past `retention_days`.
