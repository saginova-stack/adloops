# AdLoops — Resume Point

Read this first when picking the project back up. It captures the exact state
of the repo, what's blocking forward progress, and the concrete next actions.

## State as of 2026-05-20

- **Phases 1, 2, and 3 (LinkedIn executor) code-complete.** Phase 3 is tested against MCP mocks; going live still needs LinkedIn Marketing Developer Platform approval on the app plus a one-time `node dist/auth-cli.js` to seed the token store.
- **178 tests passing.**
- **Skill installed live** at `~/.openclaw/workspace/skills/adloops` (symlink → `~/adloops/skill/`).
- **No remote configured.** `git remote -v` shows nothing — push when you decide where this lives (likely a private GitHub repo since `brand.json` will reference real ICP details).

Phase 2 commits (most recent first):
- *(latest)* — LLM recommender chain (OpenRouter → Anthropic → rule-based) + Phase 2 doc rewrite
- `9a44ad7` — wired mutations into run.py + `--approve` mode + report renderer for action rows
- `9288418` — Google + Meta executors (adloop MCP preview/confirm for Google, Marketing Graph for Meta)
- `d308e85` — minimal synchronous MCP stdio client
- `7f43f7f` — mutation proposer (pause zombies, ±20% on CPA spike/drop)

Phase 1 commits:
- `8ab5e99` — GA4 cross-reference enrichment (consent gap, attribution gap, real CPA)
- `8a57867` — submodule rename `mcp-servers/google-ads` → `adloop` + `install.sh`
- `e8d3a6a` — initial read-only audit + Telegram delivery

## What Phase 2 now delivers

Per-audit-run mutation pipeline:

1. **Propose** via `mutations.propose(report, brand)`. Three rules:
   - Pause campaigns with ≥$50 spend + 0 conversions
   - Decrease budget −cap% on CPA spike ≥50% week-over-week
   - Increase budget +cap% on CPA drop ≥25% with conversion lift
2. **Check** via `guardrails.check_mutation()`. Tristate result:
   - AUTO → dispatches via platform executor (Google batches on one MCP session)
   - APPROVAL → queues to report's "Pending approvals" with a copy-paste `--approve <run_id>:<index>` command
   - REJECTED → audit log only
3. **Dispatch**:
   - Google → `pause_entity` / `update_campaign` → `confirm_and_apply` via the kLOsk/adloop MCP over stdio
   - Meta → direct Marketing Graph API (`ads_management` scope required)
   - LinkedIn → `update_campaign` via the vendored linkedin-ads MCP over stdio (requires MDP approval + `node dist/auth-cli.js` at runtime; mocked in tests)
4. **Recommendations**: `recommender.recommendations(report, brand)` returns LLM-driven advice if `OPENROUTER_API_KEY` or `ANTHROPIC_API_KEY` is set, else falls back to the rule-based section.

Every AUTO/APPROVAL/REJECTED decision writes one JSONL line to `.audit.jsonl`. The `--approve` CLI mode reads those rows and replays a single APPROVAL after re-running `check_mutation` against current brand config.

CLI flags:
- `python -m scripts.run` — full pipeline, sends to Telegram
- `python -m scripts.run --dry-run` — no Telegram, executors run in preview mode (no real changes)
- `python -m scripts.run --no-mutate` — audit + report only (Phase 1 behaviour)
- `python -m scripts.run --approve <run_id>:<index>` — replay one APPROVAL row

## What I can do right now without anything else from Michiel

- Read tests, refactor, clean up.
- Tune mutation thresholds for a specific brand via `brand.json`'s `guardrails.proposer` block (pauseZombieMinSpend, decreaseCpaSpikePct, increaseCpaDropPct, maxProposalsPerRun). Defaults match the historical module constants.
- Wire metric persistence so week-over-week consent-gap and attribution-gap trends are reported.
- Batch LinkedIn `AUTO` mutations on one MCP session in `run.py` (currently per-call spawn, mirroring the Google batching pattern would save ~1s × N spawns when there are multiple LinkedIn AUTOs).

## What Michiel needs to provide before Phase 2 is operationally green

Cumulative list — mirror of `setup.md` §7:

- [ ] **Run `./install.sh`** at the repo root — installs uv, syncs the adloop MCP, builds the LinkedIn MCP, creates the skill's `.venv`. Idempotent.
- [ ] LinkedIn Marketing Developer Platform application submitted on the LinkedIn app (1–5 day approval — **start this first** so the Phase 3 executor can run against live accounts)
- [ ] LinkedIn OAuth seeded once via `cd skill/mcp-servers/linkedin-ads && node dist/auth-cli.js`
- [ ] Google Ads developer token applied for
- [ ] Combined OAuth wizard run: `cd skill/mcp-servers/adloop && uv run adloop init`
- [ ] GA4 service-account JSON created and `GOOGLE_APPLICATION_CREDENTIALS` env set
- [ ] GA4 property granted Viewer access to that service account
- [ ] `GA4_PROPERTY_ID` set in env (numeric)
- [ ] Meta Marketing API system-user access token — **must include `ads_management` scope for Phase 2 mutations** (not just `ads_read`)
- [ ] `ADLOOPS_TELEGRAM_CHAT_ID` set in OC env
- [ ] ICP filled into `~/Syncthing/adloops-brand/brand.json`
- [ ] Confirm the actual Syncthing share name on the OC server (currently `~/Syncthing/` does not exist; override with `ADLOOPS_BRAND_DIR=...`)
- [ ] (optional) `OPENROUTER_API_KEY` or `ANTHROPIC_API_KEY` for LLM-driven recommendations

## First-run operational checklist

After the install + credentials, run for two weeks with `--no-mutate` to validate the audit and observe what the proposer *would* do (proposer output isn't surfaced when `--no-mutate` is set, so for transparency consider `--dry-run` instead, which calls executors with `dry_run=True` and shows the previews in the report). Then flip to full mode:

```bash
# Week 1-2: audit only, no mutations
0 9 * * 2,5  cd /home/ubuntu/adloops && .venv/bin/python -m scripts.run --no-mutate

# Or: preview mutations without applying
0 9 * * 2,5  cd /home/ubuntu/adloops && .venv/bin/python -m scripts.run --dry-run

# Week 3+: full pipeline
0 9 * * 2,5  cd /home/ubuntu/adloops && .venv/bin/python -m scripts.run
```

After every run in the first month, read `~/Syncthing/adloops-brand/campaigns/.audit.jsonl` and confirm the AUTO decisions look sane. If they don't, tune the thresholds in `mutations.py` (`PAUSE_ZOMBIE_MIN_SPEND`, `DECREASE_CPA_SPIKE_PCT`, `INCREASE_CPA_DROP_PCT`) or temporarily switch to `--no-mutate`.

## How to resume

```bash
cd /home/ubuntu/adloops
.venv/bin/pytest tests/ -q          # confirm 178 pass
git log --oneline -10               # confirm latest commit is HEAD
ls /home/ubuntu/.openclaw/workspace/skills/adloops/SKILL.md   # confirm symlink intact
ls skill/mcp-servers/adloop/pyproject.toml                    # confirm submodule present
ls skill/mcp-servers/linkedin-ads/package.json                # confirm submodule present
```

If any of those fail, see "Recovery" below.

## Phase 3 (LinkedIn mutations — code shipped, awaiting auth)

What's in place:

- `skill/scripts/executors/linkedin.py` — spawns the vendored
  `danielpopamd/linkedin-ads-mcp` via `mcp_runner` (Node command) and
  maps `Mutation.kind` onto a single MCP tool call: `update_campaign`
  with `status=PAUSED`/`ACTIVE` for pause/enable, or
  `dailyBudgetAmount` for budget_change.
- `executors/__init__.py:dispatch` routes `linkedin` through the new
  executor (the "Phase 3 blocked" error is gone).
- `tests/test_executors.py` has 10 LinkedIn-specific cases mirroring
  the Google/Meta patterns: URN→numeric-id stripping, currency
  passthrough, dry-run shape, missing env, unsupported kind, inactive
  session.

What still needs to happen before live:

1. **MDP approval** on the LinkedIn app at
   https://www.linkedin.com/developers/apps (1–5 business days).
2. **One-time OAuth seed**: `cd skill/mcp-servers/linkedin-ads && node dist/auth-cli.js`.
   This writes a token to the MCP's on-disk store; the executor
   doesn't manage tokens itself.
3. Export `LINKEDIN_AD_ACCOUNT_URN`
   (e.g. `urn:li:sponsoredAccount:1234567890`) — the executor strips
   the trailing numeric segment to get the MCP's `accountId`.
4. (Optional) Set `currency` in proposer/mutation payloads if the
   account is non-USD — the LinkedIn MCP defaults `dailyBudgetCurrency`
   to USD when omitted.

Known follow-up: `run.py` currently dispatches Meta and LinkedIn AUTOs
per-call (spawn-per-mutation). For LinkedIn that's a Node MCP spawn per
mutation. If real-world runs produce multiple LinkedIn AUTOs, mirror
the Google batching path (`_dispatch_google_batch`) — `LinkedInExecutor`
is already a context manager so the call-site change is small.

## Recovery

If the repo state looks wrong on resume:

| Symptom | Fix |
|---------|-----|
| Nothing installed | `./install.sh` (idempotent — installs uv, syncs adloop, builds linkedin-ads, creates `.venv`, runs tests) |
| `.venv` missing | `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt` |
| Submodules empty (`skill/mcp-servers/adloop/` is an empty dir) | `git submodule update --init` |
| `uv` not on PATH after install.sh | `export PATH="$HOME/.local/bin:$PATH"` |
| GA4 enrichment shows "skipped: PERMISSION_DENIED" in reports | Service account email isn't granted Viewer on the GA4 property — GA4 Admin → Property Access Management |
| Meta mutations return "ads_management permission" error | Token only has `ads_read` — regenerate with `ads_management` |
| Tests import errors | `cd skill && PYTHONPATH=. ../.venv/bin/pytest ../tests/` — but `tests/conftest.py` already injects skill/ into sys.path, so this shouldn't happen |
| OC doesn't see the skill | `ls -la ~/.openclaw/workspace/skills/adloops` should be a symlink to `/home/ubuntu/adloops/skill`; if missing, recreate with `ln -s /home/ubuntu/adloops/skill ~/.openclaw/workspace/skills/adloops` |
| Brand dir missing | Either set `ADLOOPS_BRAND_DIR` to the real Syncthing share, or run `--scaffold` against a stub path for testing |
| `--approve` reports "still APPROVAL" | The mutation still exceeds the cap even after re-check. Either tune `brand.json:guardrails.maxDailyBudgetChangePct`, or replay the audit later when the delta has narrowed. |

If you're really stuck, `development.md` has the full build log including which decisions were made and why. Read that before you start un-doing things.

## Things explicitly NOT in this repo (don't add them without asking)

- Image/creative generation (Phase 4, separate spec)
- TikTok / YouTube / Microsoft Ads platforms
- Notion or Drive integration for brand assets — assets live in Syncthing only
- A web dashboard — Telegram is the surface

These are spec'd as out-of-scope. If a future request seems to want them, push back and ask whether the scope changed before building.
