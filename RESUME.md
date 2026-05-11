# AdLoops — Resume Point

Read this first when picking the project back up. It captures the exact state
of the repo, what's blocking forward progress, and the concrete next actions.

## State as of 2026-05-11

- **Phase 1 complete and committed**, including GA4 cross-reference enrichment.
- **81 tests passing.**
- **Skill installed live** at `~/.openclaw/workspace/skills/adloops` (symlink → `~/adloops/skill/`).
- **No remote configured.** `git remote -v` shows nothing — push when you decide where this lives (likely a private GitHub repo since `brand.json` will reference real ICP details).

Key commits:
- `e8d3a6a` — initial Phase 1 (read-only audit, Telegram, guardrails unwired)
- `8a57867` — rename `mcp-servers/google-ads` → `mcp-servers/adloop`, add `install.sh`, GA4 env vars documented
- *(latest)* — GA4 cross-reference enrichment: consent gap, attribution gap, real CPA; new Tracking & consent report section

## What Phase 1 now delivers

Per-audit run, Google Ads campaigns are enriched with GA4 paid-Google session
and conversion data joined on `sessionGoogleAdsCampaignId`. The report flags:

- **Consent gap ≥30%** — Ads-reported clicks vs GA4 sessions. Typical signal
  for GDPR cookie rejection or broken tracking.
- **Attribution gap ≥25%** — Ads-reported conversions vs GA4 conversions.
  Typical signal for conversion-window mismatch or broken event wiring.
- **Real CPA drift ≥25%** — CPA computed against GA4 conversions vs the
  Ads-reported number. Flags when the two sources disagree on what
  converted.

GA4 enrichment is best-effort: missing creds, API errors, or zero matches
leave the Google Ads numbers intact and surface the reason in the report
under "Tracking & consent: GA4 enrichment skipped: …".

Thresholds live as module constants in `telegram_report.py`
(`CONSENT_GAP_FLAG_PCT`, `ATTRIBUTION_GAP_FLAG_PCT`, `REAL_CPA_DRIFT_FLAG_PCT`)
— tune them from data once we have real traffic.

## What I can do right now without anything else from Michiel

- Read tests, refactor, clean up.
- Build Phase 2 mutation paths against mocks (real wiring needs creds).
- Improve the rule-based recommender or wire OpenRouter/Nemotron for the LLM-driven version.
- Add landing-page level cross-reference (paid traffic + zero conversions per page) — would mean a second GA4 query on `pagePath × sessionGoogleAdsCampaignId`.

## What Michiel needs to provide before Phase 1 is operationally green

Mirror of `setup.md` §7, kept here so it's the first thing seen on resume:

- [ ] **Run `./install.sh`** at the repo root — installs uv, syncs the adloop MCP, builds the LinkedIn MCP, creates the skill's `.venv`. Idempotent.
- [ ] LinkedIn Marketing Developer Platform application submitted (1–5 day approval — **start this first**, it's the long pole)
- [ ] Google Ads developer token applied for
- [ ] Combined OAuth wizard run: `cd skill/mcp-servers/adloop && uv run adloop init` (sets up Google Ads + GA4 auth in one flow)
- [ ] GA4 service-account JSON created and `GOOGLE_APPLICATION_CREDENTIALS` env set (preferred over OAuth for cron stability)
- [ ] GA4 property granted Viewer access to that service account
- [ ] `GA4_PROPERTY_ID` set in env (numeric)
- [ ] Meta Marketing API system-user access token captured (`ads_read` for Phase 1, add `ads_management` for Phase 2)
- [ ] `ADLOOPS_TELEGRAM_CHAT_ID` set in OC env — pick the chat that should receive the report
- [ ] ICP filled into `~/Syncthing/adloops-brand/brand.json` (run `python -m scripts.run --scaffold` first to seed it from the example file)
- [ ] Confirm the actual Syncthing share name on the OC server. Currently `~/Syncthing/` does not exist on this box; only `~/.local/state/syncthing/`. Override with `ADLOOPS_BRAND_DIR=/path/to/share/adloops-brand` if the share lives elsewhere.

## How to resume

```bash
cd /home/ubuntu/adloops
.venv/bin/pytest tests/ -q          # confirm 81 pass
git log --oneline -5                # confirm latest commit is HEAD
ls /home/ubuntu/.openclaw/workspace/skills/adloops/SKILL.md   # confirm symlink intact
ls skill/mcp-servers/adloop/pyproject.toml                    # confirm submodule present
ls skill/mcp-servers/linkedin-ads/package.json                # confirm submodule present
```

If any of those fail, see "Recovery" below.

## Phase 2 plan (next phase — start here)

Goal: enable guardrailed mutations on Meta + Google. LinkedIn stays read-only
(blocked on MDP approval).

**Architecture shift for Phase 2:** mutations go through the vendored MCP
servers via stdio. The kLOsk/adloop MCP has `preview → confirm_and_apply`
built in — exactly matches our `Mutation → check_mutation → execute` flow.
Phase 2 takes a `mcp` Python client dep and spawns the MCP per audit run.

### Files to add

- `skill/scripts/mutations.py` — proposes mutations from an `AuditReport`. Pure function: takes report → returns `list[Mutation]` (defined in `guardrails.py`). Phase 2 starts with three rules:
  1. Pause campaigns with $X+ spend and zero conversions over the 7d window (kill obvious zombies).
  2. Decrease budget by 20% (the cap) on campaigns whose CPA grew >50% week-over-week.
  3. Increase budget by 20% on campaigns whose CPA dropped >25% with conversion volume up.
  Cap to N proposals per run (config in `brand.json`?) so a runaway never sends 50 mutations through.
- `skill/scripts/mcp_runner.py` — thin wrapper around the Python `mcp` client. Spawns an MCP server as a subprocess over stdio, calls a named tool with JSON args, returns the result. Reusable for both adloop and linkedin-ads MCPs.
- `skill/scripts/executors/google.py` — executes a `Mutation` via the kLOsk/adloop MCP server's `pause_entity` / `update_campaign` / `draft_campaign` + `confirm_and_apply` flow.
- `skill/scripts/executors/meta.py` — executes via the Marketing Graph API. Implements its own preview/confirm dance (compute the diff client-side, log it, then POST).

### `run.py` flow change

After `audit.run_audit(...)` and before report rendering:

```python
proposals = mutations.propose(report, brand)
cfg = guardrails.config_from_brand(brand)

decisions = []
for m in proposals:
    projected = mutations.projected_total_spend(report, [m])
    decisions.append(guardrails.check_mutation(m, cfg, projected_active_daily_spend=projected))

# Apply only AUTO; queue APPROVAL; log REJECTED
for d in decisions:
    if d.decision is Decision.AUTO:
        try:
            executors.dispatch(d.mutation)
            guardrails.log_decision(d, applied=True)
            report.actions_taken.append(guardrails.serialize_result(d))
        except Exception as e:
            guardrails.log_decision(d, applied=False)
            # surface in report
    elif d.decision is Decision.APPROVAL:
        guardrails.log_decision(d, applied=False)
        report.pending_approvals.append(guardrails.serialize_result(d))
    else:  # REJECTED
        guardrails.log_decision(d, applied=False)
```

### `--approve` flag

```
python -m scripts.run --approve <run_id>:<index>
```

Reads the audit log, finds the matching APPROVAL row, re-runs `check_mutation`
to confirm the rule is still in approval territory (a 30% change on Tuesday
might be only 18% on Friday), and executes if AUTO.

### Tests to write

- `tests/test_mutations.py` — proposal rules (pause-zombie, decrease-on-cpa-spike, increase-on-cpa-drop). Edge cases: zero-conversion + zero spend (don't pause), tiny budget that ±20% rounds to noise.
- `tests/test_mcp_runner.py` — mocked subprocess (use a fixture that emits canned MCP framing), confirm tool call + result parsing.
- `tests/test_run_phase2.py` — entrypoint with mocked executors; confirm AUTO calls executor, APPROVAL doesn't, REJECTED doesn't. Confirm audit log gets one line per decision.
- Don't write live API mutation tests — those land in a manual smoke test against a sandbox account.

### LLM recommendations

Phase 2 also adds the LLM-driven recommender that the spec called out.
Implementation hint:

```python
# skill/scripts/recommender.py
import os
def llm_recommendations(report: AuditReport, brand: Brand) -> list[str]:
    if os.environ.get("OPENROUTER_API_KEY"):
        return _via_openrouter(report, brand)   # nvidia/nemotron-3-super-120b-a12b:free
    if os.environ.get("ANTHROPIC_API_KEY"):
        return _via_anthropic(report, brand)    # haiku, the cheap one
    return _rule_based_recommendations(report.deltas)  # the existing fallback
```

Keep token use small — one prompt per run, max 1k input tokens. Don't pass the
whole snapshot; pass only the deltas + brand voice + 7 metric totals + the
GA4 cross-reference flags (they're often the most actionable signal).

## Phase 3 plan (LinkedIn mutations)

Trigger when LinkedIn MDP approval lands. Add `skill/scripts/executors/linkedin.py` mirroring the Meta executor — same guardrail flow, different API. Most of the work is auth (`node dist/auth-cli.js` in the vendored MCP) and matching the LinkedIn Campaigns/Creatives structure to our `Mutation` shape.

## Recovery

If the repo state looks wrong on resume:

| Symptom | Fix |
|---------|-----|
| Nothing installed | `./install.sh` (idempotent — installs uv, syncs adloop, builds linkedin-ads, creates `.venv`, runs tests) |
| `.venv` missing | `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt` |
| Submodules empty (`skill/mcp-servers/adloop/` is an empty dir) | `git submodule update --init` |
| `uv` not on PATH after install.sh | `export PATH="$HOME/.local/bin:$PATH"` |
| GA4 enrichment shows "skipped: PERMISSION_DENIED" in reports | Service account email isn't granted Viewer on the GA4 property — GA4 Admin → Property Access Management |
| Tests import errors | `cd skill && PYTHONPATH=. ../.venv/bin/pytest ../tests/` — but `tests/conftest.py` already injects skill/ into sys.path, so this shouldn't happen |
| OC doesn't see the skill | `ls -la ~/.openclaw/workspace/skills/adloops` should be a symlink to `/home/ubuntu/adloops/skill`; if missing, recreate with `ln -s /home/ubuntu/adloops/skill ~/.openclaw/workspace/skills/adloops` |
| Brand dir missing | Either set `ADLOOPS_BRAND_DIR` to the real Syncthing share, or run `--scaffold` against a stub path for testing |

If you're really stuck, `development.md` has the full build log including which decisions were made and why. Read that before you start un-doing things.

## Things explicitly NOT in this repo (don't add them without asking)

- Image/creative generation (Phase 4, separate spec)
- TikTok / YouTube / Microsoft Ads platforms
- Notion or Drive integration for brand assets — assets live in Syncthing only
- A web dashboard — Telegram is the surface

These are spec'd as out-of-scope. If a future request seems to want them, push back and ask whether the scope changed before building.
