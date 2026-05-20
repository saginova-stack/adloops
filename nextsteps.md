# AdLoops — Next Steps

Handover doc for what comes after Phase 3. Phases 1, 2, and 3 are
shipped (158 tests passing). What stands between "code complete" and
"operationally running twice a week" is credential work, not more code.

For a deeper picture of the build itself read
[`development.md`](./development.md); for the Claude-side state machine
read [`RESUME.md`](./RESUME.md).

---

## The long pole: start this today

**LinkedIn Marketing Developer Platform — Advertising API application.**

Apply at https://www.linkedin.com/developers/apps → create an app →
request the "Advertising API" product. LinkedIn's SLA is 1–5 business
days. The Phase 3 executor is shipped and tested against mocks, but
live calls fail until this approval lands and `node dist/auth-cli.js`
has seeded the MCP's token store.

Everything else can run in parallel.

---

## Operational checklist

Run from the OpenClaw server. Full step-by-step is in
[`setup.md`](./setup.md) §1–§7 — this is the compressed view.

1. **Install everything**

   ```bash
   cd /home/ubuntu/adloops
   ./install.sh
   ```

   Idempotent. Installs `uv`, syncs the adloop MCP, builds the LinkedIn
   MCP, creates the skill's `.venv`, runs the test suite.

2. **Google Ads + GA4 — combined OAuth wizard**

   ```bash
   cd skill/mcp-servers/adloop && uv run adloop init
   ```

   Browser-based wizard. Walks through Google Cloud project + OAuth
   consent + GA4 property selection. Apply for the Google Ads developer
   token at https://developers.google.com/google-ads/api/docs/get-started/dev-token
   if you haven't.

3. **GA4 service account (preferred over OAuth for cron stability)**

   - Google Cloud Console → APIs & Services → Credentials → Create service account
   - GA4 → Admin → Property Access Management → grant the service-account email **Viewer**
   - Download the JSON, point `GOOGLE_APPLICATION_CREDENTIALS` at the path
   - Set `GA4_PROPERTY_ID` to the numeric property id

4. **Meta Marketing API token**

   - https://developers.facebook.com/apps/ → create app → Marketing API
   - Generate a System User access token with **both** scopes:
     - `ads_read` (for the audit)
     - `ads_management` (required for Phase 2 mutations — pause/enable/budget change)
   - Set `META_ACCESS_TOKEN` and `META_AD_ACCOUNT_ID` (the `act_*` form)

5. **LinkedIn auth (once MDP approval lands)**

   ```bash
   cd skill/mcp-servers/linkedin-ads && node dist/auth-cli.js
   ```

   Set `LINKEDIN_ACCESS_TOKEN` and `LINKEDIN_AD_ACCOUNT_URN`. Phase 1
   audit will start picking up LinkedIn data immediately; Phase 3
   mutations need the executor (see "What Claude can do in parallel"
   below).

6. **Telegram destination**

   The OC bot's `TELEGRAM_BOT_TOKEN` is already set. You need to
   choose the chat:

   ```bash
   # Send a message to your bot in the target chat, then:
   curl "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates" \
     | jq '.result[-1].message.chat.id'
   ```

   Export `ADLOOPS_TELEGRAM_CHAT_ID=<that_id>`.

7. **Brand directory + ICP**

   ```bash
   .venv/bin/python -m scripts.run --scaffold
   # edit ~/Syncthing/adloops-brand/brand.json — fill personas, value props, voice
   ```

   The skill refuses to run if `icp.personas` is empty (fail-loud).

   *Note: `~/Syncthing/` is **not currently mounted** on the OC server.
   Either mount the share, or override with `ADLOOPS_BRAND_DIR=/path/to/share/adloops-brand`.*

8. **Optional: LLM-driven recommendations**

   ```
   OPENROUTER_API_KEY=sk-...        # Nemotron 3 Super free tier (preferred)
   ANTHROPIC_API_KEY=sk-ant-...     # Claude Haiku fallback
   ```

   If neither is set, the rule-based recommender runs. If an LLM call
   errors mid-run, the chain silently falls through to rules — the
   recommendations section is never blanked.

9. **Cron — observation-only first**

   Run for the first 2 weeks in audit-only mode while you watch the
   audit log:

   ```cron
   0 9 * * 2,5  cd /home/ubuntu/adloops && .venv/bin/python -m scripts.run --no-mutate
   ```

   Or to see what mutations *would* fire (without applying):

   ```cron
   0 9 * * 2,5  cd /home/ubuntu/adloops && .venv/bin/python -m scripts.run --dry-run
   ```

   When you trust the proposer, flip to full mode by dropping
   `--no-mutate` / `--dry-run`. Mutations cap at 10 per run and every
   one is gated by guardrails (±20% budget, new campaigns PAUSED,
   optional cross-platform spend ceiling).

10. **Read `.audit.jsonl` after every run in the first month**

    Lives at `~/Syncthing/adloops-brand/campaigns/.audit.jsonl`. One
    JSONL line per guardrail decision (AUTO / APPROVAL / REJECTED) with
    `run_id`, `platform`, `campaign_id`, `kind`, `before`, `after`,
    `rule`, `applied`. Confirm the AUTO decisions look sane; if not,
    tune the constants in `skill/scripts/mutations.py` or switch back
    to `--no-mutate`.

---

## What Claude can do in parallel (code work, no creds needed)

Pick any combination. Each is ~1 commit. Listed in recommended order:

### 1. Week-over-week trend on cross-reference signals — recommended next

Today the report shows current-state consent gap (e.g. "41%"); comparing
to last run gives "consent gap +8pp w/w" which is more actionable.
Needs to read prior snapshot, compute delta on `ga4_sessions` /
`consent_gap_pct`, surface in the report.

### 2. Batch LinkedIn AUTOs on one MCP session

`run.py` currently routes Meta and LinkedIn AUTOs through `_dispatch_one()`
(spawn-per-call). Meta is HTTP so there's no spawn cost; LinkedIn is a
Node MCP and pays a spawn per mutation. `LinkedInExecutor` is already
a context manager — mirror `_dispatch_google_batch()` for LinkedIn.
Marginal unless a single run produces multiple LinkedIn AUTOs.

---

## Recommended path

Start the LinkedIn MDP application today (free, slowest dependency).
The Phase 3 executor is already in place, so the moment approval lands
+ `node dist/auth-cli.js` runs, the pipeline mutates LinkedIn too. Once
you have any one platform's creds in place, do one `--dry-run` execution
to validate the pipeline end-to-end against real data — that's the only
thing the unit-test suite doesn't cover.

Then enable cron in `--no-mutate` for 2 weeks → flip to `--dry-run`
for 1 week → full mode.

---

## Where things live

- **Code**: `/home/ubuntu/adloops/skill/scripts/`
- **Tests**: `/home/ubuntu/adloops/tests/` (178 passing — `.venv/bin/pytest tests/ -q`)
- **Vendored MCPs**: `/home/ubuntu/adloops/skill/mcp-servers/{adloop,linkedin-ads}/`
- **Brand config (not yet present)**: `~/Syncthing/adloops-brand/brand.json`
- **Audit log (not yet present)**: `~/Syncthing/adloops-brand/campaigns/.audit.jsonl`
- **Snapshots (not yet present)**: `~/Syncthing/adloops-brand/campaigns/.archive/`
- **OpenClaw registration**: `~/.openclaw/workspace/skills/adloops` → symlink to `/home/ubuntu/adloops/skill`

## Other handover docs

- [`README.md`](./README.md) — repo overview + layout
- [`setup.md`](./setup.md) — full operator setup with cred application steps
- [`SKILL.md`](./skill/SKILL.md) — OpenClaw skill manifest + behaviour
- [`development.md`](./development.md) — what shipped per phase + design decisions
- [`RESUME.md`](./RESUME.md) — Claude-side resume state (what to read first on a fresh session)
