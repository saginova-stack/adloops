---
name: adloops
description: "Twice-weekly audit and guardrailed auto-tweak loop for paid ads on Google Ads, Meta Ads, and LinkedIn Ads. Pulls last 7d performance, diffs against the prior run, joins Google Ads with GA4 to flag consent gaps and attribution discrepancies, proposes mutations within a ±20% budget cap (pause zombies, scale winners, slow CPA spikes), and posts a structured report to Telegram. LinkedIn reads work today; LinkedIn mutations land in Phase 3 once Marketing Developer Platform approval comes through. Use when the user wants to know how their ads are performing this week, schedule a recurring ads audit, or apply auto-pilot tweaks within hard guardrails."
metadata:
  {
    "openclaw": {
      "emoji": "📊",
      "requires": {
        "env": [
          "TELEGRAM_BOT_TOKEN"
        ],
        "optionalEnv": [
          "ADLOOPS_TELEGRAM_CHAT_ID",
          "TELEGRAM_CHAT_ID",
          "GOOGLE_ADS_DEVELOPER_TOKEN",
          "GOOGLE_ADS_CLIENT_ID",
          "GOOGLE_ADS_CLIENT_SECRET",
          "GOOGLE_ADS_REFRESH_TOKEN",
          "GOOGLE_ADS_LOGIN_CUSTOMER_ID",
          "GOOGLE_ADS_CUSTOMER_ID",
          "GA4_PROPERTY_ID",
          "GOOGLE_APPLICATION_CREDENTIALS",
          "META_ACCESS_TOKEN",
          "META_AD_ACCOUNT_ID",
          "LINKEDIN_ACCESS_TOKEN",
          "LINKEDIN_AD_ACCOUNT_URN",
          "OPENROUTER_API_KEY",
          "ANTHROPIC_API_KEY",
          "ADLOOPS_BRAND_DIR"
        ]
      },
      "primaryEnv": "TELEGRAM_BOT_TOKEN"
    }
  }
---

# AdLoops Skill

Audits paid ad accounts (Google, Meta, LinkedIn) on a 2x/week cadence, diffs against the prior run, and (Phase 2) auto-applies budget tweaks within hard guardrails — every mutation gated on ±20% per-run budget change, mandatory PAUSED status on new campaigns, optional cross-platform daily-spend ceiling, and an append-only audit log. Posts the report to Telegram via the existing OpenClaw bot.

Phase 2 (current) wires guardrailed mutations on Google Ads and Meta. LinkedIn stays read-only (Phase 3, blocked on Marketing Developer Platform approval). The audit also enriches Google Ads campaigns with GA4 paid-traffic data to surface consent gaps, attribution discrepancies, and real (GA4-counted) CPA.

Before running:

> First-time setup? The full guided path (Manager-account dev token, headless
> OAuth, conversion-tracking prerequisite, run order) is in ONBOARDING.md:
> <https://github.com/saginova-stack/adloops/blob/main/ONBOARDING.md>

1. **Run `./install.sh`** at the repo root once. Installs uv, syncs the adloop MCP, builds the LinkedIn MCP, sets up the Python venv.
2. **Wire credentials** for at least one platform (see `setup.md` for the application steps).
3. **Fill in the brand config** (`brand.json`). The skill refuses to run if `icp.personas` is empty. Run `cd skill && ../.venv/bin/python -m scripts.run --scaffold` to create the directory + a copy of the example file. Default location is `~/.adloops/brand/brand.json`; override with `ADLOOPS_BRAND_DIR`.
4. **Confirm the OC Telegram bot is reachable** — the skill reads `TELEGRAM_BOT_TOKEN` from the OpenClaw process env. Set `ADLOOPS_TELEGRAM_CHAT_ID` to the chat that should receive the report.

## Run it

Run from the `skill/` directory, using a Python that has `requirements.txt`
installed. From the repo that's the venv `install.sh` created:

```bash
cd skill && ../.venv/bin/python -m scripts.run --check
```

(If you installed this as a standalone skill rather than from the repo, run from
the skill directory with that environment's Python, e.g.
`./.venv/bin/python -m scripts.run --check`.) The commands below are shown as
`python -m scripts.run` for brevity — substitute the Python above.

```bash
# One-time scaffold of the brand directory:
python -m scripts.run --scaffold

# Dry run — no Telegram send, executors run in preview mode (no real changes):
python -m scripts.run --dry-run

# Audit + report only, no mutations (Phase 1 behaviour, useful during ops trust-building):
python -m scripts.run --no-mutate

# Real run (audit, dispatch AUTO mutations, queue APPROVAL, send to Telegram):
python -m scripts.run

# Approve a queued mutation from a prior run:
python -m scripts.run --approve <run_id>:<index>
```

The cron operator should run this from inside `skill/` with the venv activated. See `setup.md` for the exact systemd / cron entry.

## What happens on each run

1. Load and validate `brand.json` (fail-loud on missing or empty personas — sends a Telegram heads-up and exits nonzero).
2. For each enabled platform in `brand.json.guardrails.platforms`:
   - Pull the last 7 days of campaign-level performance.
   - Skip the platform with a clear error if its env vars are missing — other platforms still run.
3. Diff against the most recent snapshot in `<brand-dir>/campaigns/.archive/` (default `~/.adloops/brand`, or `ADLOOPS_BRAND_DIR`).
4. Compute top movers: 3 best by conversion lift, 3 worst by spend-with-zero-conversions.
5. Propose mutations via `scripts/mutations.py` (pause zombies, ±20% on CPA spikes/drops). Each runs through `scripts/guardrails.py`: AUTO → dispatched via `scripts/executors/` (Google goes through the adloop MCP's `preview → confirm_and_apply`; Meta goes direct to Marketing Graph). APPROVAL → queued to the report. REJECTED → audit log only.
6. Upgrade recommendations to the LLM chain (`recommender.py`): OpenRouter Nemotron → Anthropic Haiku → rule-based fallback.
7. Write a fresh JSON snapshot to `.archive/<run_id>.json`.
8. Render the Telegram message in 7 sections (header, headline metrics, top movers, tracking & consent, actions taken, pending approvals, recommendations) and split if any single message exceeds 3000 chars.

## Brand config

The brand file is the only source of truth for ICP, voice, and guardrails. See `references/brand.schema.json` for the schema and `references/brand.example.json` for a filled-in sample. Required keys: `company`, `icp` (with at least one persona), `valueProps`, `brandVoice`, `guardrails`.

## Guardrails

Implemented in `scripts/guardrails.py`, fully unit-tested, wired in front of every mutation in `scripts/run.py`:

| Rule | Decision |
|------|----------|
| Budget change within ±N% (default 20) | AUTO |
| Budget change beyond ±N% | APPROVAL |
| Pause poor performer | AUTO |
| Re-enable a paused campaign | APPROVAL |
| Create new campaign, status=PAUSED, approval not required | AUTO |
| Create new campaign, status≠PAUSED | REJECTED |
| Create new campaign, approval required | APPROVAL |
| Increase that pushes total daily spend over `neverIncreaseBudgetAbove` | REJECTED |
| Unrecognized mutation kind | REJECTED |

Every mutation — proposed, applied, or rejected — writes one JSONL line to `<brand-dir>/campaigns/.audit.jsonl` (default `~/.adloops/brand`, or `ADLOOPS_BRAND_DIR`).

## Vendored MCP servers

`mcp-servers/adloop` is `kLOsk/adloop@v0.7.0` (MIT) — a *combined* Google Ads + GA4 MCP server with cross-reference tools (`analyze_campaign_conversions`, `landing_page_analysis`, `attribution_check`). `mcp-servers/linkedin-ads` is `danielpopamd/linkedin-ads-mcp@05a2761` (MIT).

Reads use direct Python SDKs (Google Ads, GA4 Data, Meta Marketing Graph, LinkedIn REST) — deterministic, no subprocess, cron-friendly. The audit computes the GA4 cross-reference join itself in Python; the report surfaces consent gap and attribution discrepancy signals.

Writes go through the vendored MCPs. Google Ads mutations use the kLOsk/adloop MCP's `preview → confirm_and_apply` two-step over stdio (matches our guardrail flow exactly). Meta mutations go direct to the Marketing Graph (no public MCP shim exists). LinkedIn writes are wired but locked behind Phase 3.

Meta has no public MCP shim (the official Meta Ads CLI announced April 2026 is CLI-only); we hit the Marketing Graph API directly via the same auth Meta's CLI uses.

Run `./install.sh` from the repo root for the one-time setup that prepares both vendored MCPs (installs uv, runs `uv sync` on adloop, `npm install && npm run build` on the LinkedIn MCP).

## What's NOT in this build

- LinkedIn mutations (Phase 3, after Marketing Developer Platform approval)
- Creative generation (Phase 4, separate spec)
- TikTok / YouTube / Microsoft Ads (out of spec)

## Files

- `scripts/run.py` — entrypoint (`python -m scripts.run`)
- `scripts/brand_loader.py` — loads & validates brand.json
- `scripts/guardrails.py` — mutation guardrails + audit log
- `scripts/mcp_clients.py` — Google/Meta/LinkedIn read clients + GA4 enrichment
- `scripts/audit.py` — fetch-all → diff → top movers → GA4 cross-reference → snapshot
- `scripts/mutations.py` — proposes Mutations from an AuditReport (pause zombies, CPA spike/drop)
- `scripts/mcp_runner.py` — minimal synchronous MCP stdio client
- `scripts/executors/google.py` — Google Ads mutations via the adloop MCP (preview/confirm)
- `scripts/executors/meta.py` — Meta mutations via the Marketing Graph API
- `scripts/recommender.py` — LLM-driven recommendations (OpenRouter → Anthropic → rule-based)
- `scripts/telegram_report.py` — message rendering + Telegram send
- `references/brand.schema.json` — JSON Schema for `brand.json`
- `references/brand.example.json` — filled-in template
- `mcp-servers/` — vendored MCP submodules used by the executors
