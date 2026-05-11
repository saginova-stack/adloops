# AdLoops — Resume Point

Read this first when picking the project back up. It captures the exact state
of the repo, what's blocking forward progress, and the concrete next actions.

## State as of 2026-05-11

- **Phase 1 + Phase 2 complete and committed.** LinkedIn mutations (Phase 3) blocked on Marketing Developer Platform approval.
- **148 tests passing.**
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
   - LinkedIn → raises "Phase 3 — blocked on MDP approval"
4. **Recommendations**: `recommender.recommendations(report, brand)` returns LLM-driven advice if `OPENROUTER_API_KEY` or `ANTHROPIC_API_KEY` is set, else falls back to the rule-based section.

Every AUTO/APPROVAL/REJECTED decision writes one JSONL line to `.audit.jsonl`. The `--approve` CLI mode reads those rows and replays a single APPROVAL after re-running `check_mutation` against current brand config.

CLI flags:
- `python -m scripts.run` — full pipeline, sends to Telegram
- `python -m scripts.run --dry-run` — no Telegram, executors run in preview mode (no real changes)
- `python -m scripts.run --no-mutate` — audit + report only (Phase 1 behaviour)
- `python -m scripts.run --approve <run_id>:<index>` — replay one APPROVAL row

## What I can do right now without anything else from Michiel

- Read tests, refactor, clean up.
- Add Phase 3 LinkedIn executor against mocks (real wiring needs MDP approval).
- Tune the mutation thresholds in `mutations.py` (currently constants — make them brand-config?).
- Add landing-page level cross-reference (paid traffic + zero conversions per page) — would mean a second GA4 query joining `pagePath × sessionGoogleAdsCampaignId`.
- Wire metric persistence so week-over-week consent-gap and attribution-gap trends are reported.

## What Michiel needs to provide before Phase 2 is operationally green

Cumulative list — mirror of `setup.md` §7:

- [ ] **Run `./install.sh`** at the repo root — installs uv, syncs the adloop MCP, builds the LinkedIn MCP, creates the skill's `.venv`. Idempotent.
- [ ] LinkedIn Marketing Developer Platform application submitted (1–5 day approval — **start this first**, it's the long pole for Phase 3)
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
.venv/bin/pytest tests/ -q          # confirm 148 pass
git log --oneline -10               # confirm latest commit is HEAD
ls /home/ubuntu/.openclaw/workspace/skills/adloops/SKILL.md   # confirm symlink intact
ls skill/mcp-servers/adloop/pyproject.toml                    # confirm submodule present
ls skill/mcp-servers/linkedin-ads/package.json                # confirm submodule present
```

If any of those fail, see "Recovery" below.

## Phase 3 plan (LinkedIn mutations — blocked)

Trigger when LinkedIn MDP approval lands. Concretely:

1. Add `skill/scripts/executors/linkedin.py` that spawns the vendored
   `danielpopamd/linkedin-ads-mcp` via `mcp_runner` (same pattern as the
   Google executor — Node command instead of uv). Tool surface from the
   MCP: `pause_campaign`, `enable_campaign`, `update_campaign_budget`,
   etc. Map our `Mutation` shape onto the LinkedIn-MCP tool args.
2. Update `executors/__init__.py:dispatch` to route LinkedIn through it
   instead of raising the "Phase 3" error.
3. Verify the proposer doesn't need changes — the rules are
   platform-agnostic (they read `CampaignPerf.platform`).
4. Add `tests/test_executors_linkedin.py` mirroring the Google tests.
5. Update `setup.md` to remove the Phase 3 disclaimer.

The MCP itself is already installed by `install.sh` and `node dist/auth-cli.js` documented in `setup.md` §3.

Most of the work is auth (the LinkedIn MCP has a 3-legged OAuth flow that needs an explicit one-time `node dist/auth-cli.js` run) and mapping our `Mutation.kind` values to LinkedIn's API.

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
