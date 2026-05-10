# AdLoops — Resume Point

Read this first when picking the project back up. It captures the exact state
of the repo, what's blocking forward progress, and the concrete next actions.

## State as of 2026-05-10

- **Phase 1 complete and committed.** Commit: `e8d3a6a` on branch `main`.
- **56 tests passing.**
- **Skill installed live** at `~/.openclaw/workspace/skills/adloops` (symlink → `~/adloops/skill/`).
- **No remote configured.** `git remote -v` shows nothing — push when you decide where this lives (likely a private GitHub repo since `brand.json` will reference real ICP details).

## What I can do right now without anything else from Michiel

- Read tests, refactor, clean up.
- Build Phase 2 mutation paths against mocks (real wiring needs creds).
- Improve the rule-based recommender or wire OpenRouter/Nemotron for the LLM-driven version.

## What Michiel needs to provide before Phase 1 is operationally green

Mirror of `setup.md` §7, kept here so it's the first thing seen on resume:

- [ ] LinkedIn Marketing Developer Platform application submitted (1–5 day approval — **start this first**, it's the long pole)
- [ ] Google Ads developer token applied for + OAuth refresh token captured (use `cd skill/mcp-servers/google-ads && uv run adloop init`)
- [ ] Meta Marketing API system-user access token captured (`ads_read` for Phase 1, add `ads_management` for Phase 2)
- [ ] `ADLOOPS_TELEGRAM_CHAT_ID` set in OC env — pick the chat that should receive the report
- [ ] ICP filled into `~/Syncthing/adloops-brand/brand.json` (run `python -m scripts.run --scaffold` first to seed it from the example file)
- [ ] Confirm the actual Syncthing share name on the OC server. Currently `~/Syncthing/` does not exist on this box; only `~/.local/state/syncthing/`. Override with `ADLOOPS_BRAND_DIR=/path/to/share/adloops-brand` if the share lives elsewhere.

## How to resume

```bash
cd /home/ubuntu/adloops
.venv/bin/pytest tests/ -q          # confirm 56 pass
git log --oneline -5                # confirm e8d3a6a is HEAD
ls /home/ubuntu/.openclaw/workspace/skills/adloops/SKILL.md   # confirm symlink intact
```

If any of those fail, see "Recovery" below.

## Phase 2 plan (next phase — start here)

Goal: enable guardrailed mutations on Meta + Google. LinkedIn stays read-only
(blocked on MDP approval).

### Files to add

- `skill/scripts/mutations.py` — proposes mutations from an `AuditReport`. Pure function: takes report → returns `list[Mutation]` (defined in `guardrails.py`). Phase 2 starts with three rules:
  1. Pause campaigns with $X+ spend and zero conversions over the 7d window (kill obvious zombies).
  2. Decrease budget by 20% (the cap) on campaigns whose CPA grew >50% week-over-week.
  3. Increase budget by 20% on campaigns whose CPA dropped >25% with conversion volume up.
  Cap to N proposals per run (config in `brand.json`?) so a runaway never sends 50 mutations through.
- `skill/scripts/executors/google.py` — executes a `Mutation` via the kLOsk/adloop MCP server (preview + confirm pattern). Spawns the MCP as a subprocess over stdio.
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
whole snapshot; pass only the deltas + brand voice + 7 metric totals.

## Phase 3 plan (LinkedIn mutations)

Trigger when LinkedIn MDP approval lands. Add `skill/scripts/executors/linkedin.py` mirroring the Meta executor — same guardrail flow, different API. Most of the work is auth (`node dist/auth-cli.js` in the vendored MCP) and matching the LinkedIn Campaigns/Creatives structure to our `Mutation` shape.

## Recovery

If the repo state looks wrong on resume:

| Symptom | Fix |
|---------|-----|
| `.venv` missing | `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt` |
| Submodules empty (`skill/mcp-servers/google-ads/` is an empty dir) | `git submodule update --init` |
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
