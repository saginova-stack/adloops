# AdLoops

Twice-weekly audit + guardrailed auto-tweak loop for paid ads on Google Ads, Meta Ads, and LinkedIn Ads. Packaged as an OpenClaw skill (also runnable as a Claude Code skill — same shape). Posts a structured report to Telegram via the existing OpenClaw bot.

> LinkedIn campaign reads are live-verified against the current versioned Marketing API. Guardrailed LinkedIn pause, enable, and budget mutations use the same direct REST/OAuth path: every live write GETs the campaign first and GET-verifies it afterward. They require an OAuth token granted `rw_ads`; scheduled mutation remains off until one named live action has been verified.
>
> Meta support goes beyond campaign-level CBO budgets: budget changes are ad-set- and lifetime-budget aware, and a **desired-state reconciler** creates any campaigns you declare in `brand.json` that are missing (always PAUSED, through the guardrails) and reconciles budget drift on ones that exist. Declared campaigns can scaffold a default ad set whose targeting is explicit, **derived from the brand ICP** (geo / interests / company size / age via Meta Targeting Search), or attached from **Custom / Lookalike Audiences** you bring — the real path for firmographic targeting Meta has no native facet for.

**New to AdLoops?** Start with [`ONBOARDING.md`](./ONBOARDING.md) — a guided
zero-to-first-report path with every onboarding gotcha inlined (Manager-account
dev token, headless OAuth, the conversion-tracking prerequisite, run order).

## Layout

```
adloops/
├── skill/                       ← the actual skill — symlinked into OC's skills dir
│   ├── SKILL.md
│   ├── scripts/
│   │   ├── run.py               ← entrypoint (`python -m scripts.run`, run from skill/)
│   │   ├── audit.py             ← fetch-all → diff → top movers → GA4 cross-ref → snapshot
│   │   ├── brand_loader.py
│   │   ├── guardrails.py        ← hard rules + audit log, wired in front of every mutation
│   │   ├── mcp_clients.py       ← Google / Meta / LinkedIn read clients + GA4 enrichment
│   │   ├── mutations.py         ← proposer: pause zombies, ±cap% on CPA spike/drop, desired-state campaign reconciliation (Meta)
│   │   ├── mcp_runner.py        ← synchronous MCP stdio JSON-RPC client
│   │   ├── executors/
│   │   │   ├── google.py        ← adloop MCP preview/confirm
│   │   │   ├── linkedin.py      ← direct REST guarded campaign updates
│   │   │   └── meta.py          ← Marketing Graph API direct (ad-set/lifetime budgets, create_campaign + ad-set scaffolding, Custom/Lookalike Audiences)
│   │   ├── recommender.py       ← OpenRouter → Anthropic → rule-based chain
│   │   └── telegram_report.py
│   ├── references/
│   │   ├── brand.schema.json
│   │   └── brand.example.json
│   └── mcp-servers/             ← vendored submodules
│       ├── adloop/              → kLOsk/adloop @ v0.7.0 (Google Ads + GA4 cross-reference)
│       └── linkedin-ads/        → danielpopamd/linkedin-ads-mcp @ 05a2761
├── tests/                       ← 269 tests covering every module
├── install.sh                   ← one-shot first-run install (uv + submodule build + venv)
├── setup.md                     ← operator-facing setup (creds, cron, exit codes)
└── requirements.txt
```

## Quick start

```bash
./install.sh                                  # installs uv, syncs submodules, builds, sets up .venv, runs tests
cd skill                                       # run.py is a module under skill/ — invoke from here
../.venv/bin/python -m scripts.run --scaffold # creates ~/.adloops/brand (override: ADLOOPS_BRAND_DIR)
# fill in brand.json + wire creds (see setup.md §3), then:
../.venv/bin/python -m scripts.run --dry-run  # audit + propose + show previews, no real changes, no Telegram
```

Run modes (all from the `skill/` directory — e.g. `cd skill && ../.venv/bin/python -m scripts.run --check`):

```bash
python -m scripts.run --check                 # preflight: brand.json + per-platform env + live Telegram ping. Run this first.
python -m scripts.run                         # full pipeline: audit, dispatch AUTO mutations, send to Telegram
python -m scripts.run --dry-run               # no real side effects (executors run in preview mode)
python -m scripts.run --no-mutate             # audit + report + "would have fired" proposer preview (observation mode)
python -m scripts.run --approve <run>:<idx>   # replay a queued APPROVAL row after re-checking guardrails
```

See [`setup.md`](./setup.md) for credentials, scheduling, exit codes.

### LinkedIn OAuth scopes

Use the smallest scope set that matches the run mode:

```text
# Reporting only
r_ads r_ads_reporting

# Guardrailed pause / enable / daily-budget changes
r_ads r_ads_reporting rw_ads
```

`rw_ads` permits campaign management but does not bypass AdLoops guardrails.
Every live mutation still requires a preflight read and read-back verification;
scheduled LinkedIn mutations stay disabled until a named live action succeeds.

## Phasing

1. **Phase 1** — read-only audit + GA4 cross-reference + Telegram report. ✅ shipped.
2. **Phase 2** — guardrailed Google + Meta mutations, LLM-driven recommendations, `--approve` mode. ✅ shipped.
3. **LinkedIn** — campaign reads are live-verified with `r_ads` + `r_ads_reporting`. Guardrailed pause, enable, and budget mutations use direct versioned REST updates with `rw_ads`, preflight reads, and read-back verification. Automatic scheduled writes remain off until a named live action succeeds.
4. **Phase 4** — creative generation. Out of scope for this repo.
