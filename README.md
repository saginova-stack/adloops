# AdLoops

Twice-weekly audit + tweak loop for paid ads on Google Ads, Meta Ads, and LinkedIn Ads. Packaged as an OpenClaw skill (also runnable as a Claude Code skill — same shape). Posts a structured report to Telegram via the existing OpenClaw bot.

> Phase 1 is read-only. Mutations (with hard ±20% budget guardrails) ship in Phase 2. LinkedIn mutations in Phase 3.

## Layout

```
adloops/
├── skill/                  ← the actual skill — symlinked into OC's skills dir
│   ├── SKILL.md
│   ├── scripts/
│   │   ├── run.py          ← entrypoint (`python -m scripts.run`)
│   │   ├── audit.py
│   │   ├── brand_loader.py
│   │   ├── guardrails.py   ← hard rules, fully tested, wired in Phase 2
│   │   ├── mcp_clients.py  ← Google / Meta / LinkedIn read clients
│   │   └── telegram_report.py
│   ├── references/
│   │   ├── brand.schema.json
│   │   └── brand.example.json
│   └── mcp-servers/        ← vendored submodules
│       ├── google-ads/     → kLOsk/adloop @ v0.7.0
│       └── linkedin-ads/   → danielpopamd/linkedin-ads-mcp @ 05a2761
├── tests/                  ← 56 tests covering guardrails, brand, audit, report, entrypoint
├── setup.md                ← operator-facing setup
└── requirements.txt
```

## Quick start

```bash
git submodule update --init
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pytest tests/ -q          # 56 passing
.venv/bin/python -m scripts.run --scaffold   # creates ~/Syncthing/adloops-brand
# fill in brand.json, then:
.venv/bin/python -m scripts.run --dry-run    # prints to stdout, no Telegram
```

See [`setup.md`](./setup.md) for credentials, scheduling, exit codes.

## Phasing

1. **Phase 1** — read-only audit + Telegram report. ✅ this build.
2. **Phase 2** — guardrailed Meta + Google mutations. Wires `guardrails.py` into mutation code paths.
3. **Phase 3** — LinkedIn mutations (after Marketing Developer Platform approval).
4. **Phase 4** — creative generation. Out of scope for this repo.
