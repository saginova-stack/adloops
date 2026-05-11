---
name: adloops
description: "Twice-weekly audit and tweak loop for paid ads on Google Ads, Meta Ads, and LinkedIn Ads. Pulls last 7d performance, diffs against the prior run, and posts a structured report to Telegram. Phase 2+ adds guardrailed mutations (±20% budget cap, new campaigns paused). Use when the user wants to know how their ads are performing this week, schedule a recurring ads audit, or apply auto-pilot tweaks within hard guardrails."
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
          "ADLOOPS_BRAND_DIR"
        ]
      },
      "primaryEnv": "TELEGRAM_BOT_TOKEN"
    }
  }
---

# AdLoops Skill

Audits paid ad accounts (Google, Meta, LinkedIn) on a 2x/week cadence, diffs against the prior run, and posts a Telegram report through the existing OpenClaw bot. Mutation support (Phase 2+) sits behind hard guardrails enforced in code: ±20% per-run budget change, mandatory PAUSED status on new campaigns, optional cross-platform daily-spend ceiling, and an append-only audit log.

This is the Phase 1 build — read-only audit and report. Before running:

1. **Wire credentials** for at least one platform (see `setup.md` at the repo root for the application steps).
2. **Fill in `~/Syncthing/adloops-brand/brand.json`.** The skill refuses to run if `icp.personas` is empty. Run `python -m scripts.run --scaffold` to create the directory + a copy of the example file.
3. **Confirm the OC Telegram bot is reachable** — the skill reads `TELEGRAM_BOT_TOKEN` from the OpenClaw process env. Set `ADLOOPS_TELEGRAM_CHAT_ID` to the chat that should receive the report.

## Run it

```bash
# One-time scaffold of the brand directory:
python -m scripts.run --scaffold

# Dry run (prints the report to stdout, never hits Telegram):
python -m scripts.run --dry-run

# Real run (will send to Telegram):
python -m scripts.run
```

The cron operator should run this from inside `skill/` with the venv activated. See `setup.md` for the exact systemd / cron entry.

## What happens on each run

1. Load and validate `brand.json` (fail-loud on missing or empty personas — sends a Telegram heads-up and exits nonzero).
2. For each enabled platform in `brand.json.guardrails.platforms`:
   - Pull the last 7 days of campaign-level performance.
   - Skip the platform with a clear error if its env vars are missing — other platforms still run.
3. Diff against the most recent snapshot in `~/Syncthing/adloops-brand/campaigns/.archive/`.
4. Compute top movers: 3 best by conversion lift, 3 worst by spend-with-zero-conversions.
5. (Phase 2+) Decide auto vs approval-required mutations using `scripts/guardrails.py`. Phase 1 returns empty action lists.
6. Write a fresh JSON snapshot to `.archive/<run_id>.json`.
7. Render the Telegram message in 6 sections (header, headline metrics, top movers, actions taken, pending approvals, recommendations) and split if any single message exceeds 3000 chars.

## Brand config

The brand file is the only source of truth for ICP, voice, and guardrails. See `references/brand.schema.json` for the schema and `references/brand.example.json` for a filled-in sample. Required keys: `company`, `icp` (with at least one persona), `valueProps`, `brandVoice`, `guardrails`.

## Guardrails (phase 2 wiring)

Implemented in `scripts/guardrails.py`, fully unit-tested:

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

Every mutation — proposed, applied, or rejected — writes one JSONL line to `~/Syncthing/adloops-brand/campaigns/.audit.jsonl`.

## Vendored MCP servers

`mcp-servers/adloop` is `kLOsk/adloop@v0.7.0` (MIT) — a *combined* Google Ads + GA4 MCP server with cross-reference tools (`analyze_campaign_conversions`, `landing_page_analysis`, `attribution_check`). `mcp-servers/linkedin-ads` is `danielpopamd/linkedin-ads-mcp@05a2761` (MIT).

Phase 1 calls Google Ads and GA4 via their Python SDKs directly (deterministic, no subprocess) and computes the cross-reference join itself — the report surfaces consent gap and attribution discrepancy signals. The vendored MCP servers become the canonical write path in Phase 2 because the kLOsk preview/confirm pattern matches our guardrail flow exactly.

Meta has no public MCP shim (the official Meta Ads CLI announced April 2026 is CLI-only); we hit the Marketing Graph API directly via the same auth Meta's CLI uses.

Run `./install.sh` from the repo root for the one-time setup that prepares both vendored MCPs (installs uv, runs `uv sync` on adloop, `npm install && npm run build` on the LinkedIn MCP).

## What's NOT in Phase 1

- Mutations of any kind (read-only)
- LLM-driven recommendations (the rule-based recommender flags zero-conversion spend and scaled-down winners; the LLM step lands in Phase 2)
- LinkedIn anything other than reads (Phase 3, after Marketing Developer Platform approval)
- Creative generation (Phase 4, separate spec)

## Files

- `scripts/run.py` — entrypoint (`python -m scripts.run`)
- `scripts/brand_loader.py` — loads & validates brand.json
- `scripts/guardrails.py` — mutation guardrails + audit log (used in Phase 2)
- `scripts/mcp_clients.py` — Google/Meta/LinkedIn read clients
- `scripts/audit.py` — fetch-all → diff → top movers → snapshot
- `scripts/telegram_report.py` — message rendering + Telegram send
- `references/brand.schema.json` — JSON Schema for `brand.json`
- `references/brand.example.json` — filled-in template
- `mcp-servers/` — vendored MCP submodules (Phase 2 mutation path)
