# AdLoops setup

This is the operator-facing setup guide. The skill itself is documented in
`skill/SKILL.md`. Read both.

## 0. What you're getting

Twice-weekly (Tue/Fri 09:00 local) ad-account audit across Google Ads,
Meta Ads, and LinkedIn Ads, posted to Telegram via the OpenClaw bot.

- **Phase 1 (read-only audit)**: 7-day performance per campaign, week-over-week
  deltas, top movers (best + worst), and the GA4 cross-reference flags
  (consent gap, attribution gap, real CPA) on Google Ads campaigns.
- **Phase 2 (guardrailed mutations on Google + Meta — current)**: proposes
  three rules — pause zombies, decrease budget −20% on CPA spike,
  increase budget +20% on CPA drop with conversion lift. Each proposal
  runs through `guardrails.check_mutation()`: AUTO dispatches immediately,
  APPROVAL queues to the report's "Pending approvals" with a copy-paste
  `--approve` command, REJECTED logs to `.audit.jsonl` and surfaces
  nowhere else. Capped at 10 mutations per run. LLM-driven recommendations
  (OpenRouter Nemotron → Anthropic Haiku → rule-based fallback) replace
  the rule-based recommendations when a key is present.
- **Phase 3 (LinkedIn mutations)**: executor code is in place and tested
  against MCP mocks. To go live against real accounts you still need
  (a) LinkedIn Marketing Developer Platform approval on the LinkedIn app
  and (b) a one-time `node dist/auth-cli.js` run inside
  `skill/mcp-servers/linkedin-ads` to seed the MCP's token store.
  Without those, calls will fail with a LinkedIn auth error from the
  MCP — same as if the OAuth wasn't done.

## 1. First-run install

There is **one command** that prepares everything:

```bash
cd /home/ubuntu/adloops
./install.sh
```

This script:

1. Initializes git submodules (`mcp-servers/adloop`, `mcp-servers/linkedin-ads`).
2. Installs `uv` if it isn't already on PATH (via Astral's official installer).
3. `uv sync` in `mcp-servers/adloop` — pulls Google Ads + GA4 Python deps.
4. `npm install && npm run build` in `mcp-servers/linkedin-ads` — compiles the TypeScript.
5. Creates `.venv/` and installs `requirements.txt` for the skill itself.
6. Runs the test suite as a sanity check.

It is idempotent — safe to re-run after pulling new submodule SHAs.

What it does *not* do (intentionally — these need interactive input):

- Run the OAuth wizards for Google / LinkedIn.
- Create the brand directory or `brand.json`.
- Install a cron entry.

Those four steps are below.

## 2. Brand directory

The brand directory defaults to `~/.adloops/brand/`. Override it with
`ADLOOPS_BRAND_DIR` to keep the config elsewhere (e.g. a synced folder). On the
OC server we keep it in Syncthing and point the default at it via a symlink
(`~/.adloops/brand -> ~/Syncthing/adloops-brand`); `ADLOOPS_BRAND_DIR` works too.
Two options:

- Point at an existing share: `export ADLOOPS_BRAND_DIR=/path/to/share/adloops-brand`.
- Or scaffold a placeholder locally to test:

```bash
cd skill && ../.venv/bin/python -m scripts.run --scaffold
```

This creates `assets/{logo,linkedin,screenshots}/`, `campaigns/.archive/`, and
copies `references/brand.example.json` into place as `brand.json`. **Edit it
before running an audit** — the loader rejects empty `icp.personas`.

## 3. Platform credentials

You need at least one of these to get a non-empty report.

### Google Ads + GA4 (one combined OAuth flow)

The vendored `kLOsk/adloop` MCP bundles a wizard that handles both Google Ads
*and* Google Analytics 4 in one OAuth dance — the same wizard the upstream MCP
uses interactively.

**Step 1 — apply for a Google Ads developer token:**
https://developers.google.com/google-ads/api/docs/get-started/dev-token
(New tokens start as "Test"; you'll need at least "Explorer" to see
production data — it's granted automatically after your first API call.)

**Step 2 — run the wizard:**

```bash
cd skill/mcp-servers/adloop
uv run adloop init
```

It opens a browser, walks through Google Cloud project + OAuth + GA4 property
selection, and writes its config to `~/.config/adloop/config.yaml`. Copy the
relevant values to your `.env` (the wizard prints them at the end):

```
GOOGLE_ADS_DEVELOPER_TOKEN=...
GOOGLE_ADS_CLIENT_ID=...
GOOGLE_ADS_CLIENT_SECRET=...
GOOGLE_ADS_REFRESH_TOKEN=...
GOOGLE_ADS_LOGIN_CUSTOMER_ID=1234567890   # MCC if applicable, else operating account
GOOGLE_ADS_CUSTOMER_ID=1234567890         # operating account, if different from login
GA4_PROPERTY_ID=987654321                 # numeric, e.g. from GA4 → Admin → Property
GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
```

**Finding the two that trip people up** (the wizard usually captures these, but
if you're filling them by hand):

- **`GOOGLE_ADS_DEVELOPER_TOKEN`** — Google Ads → **Admin → API Center**
  (direct: <https://ads.google.com/aw/apicenter>). It's a 22-character string.
  If you've never requested one, that page shows an API-access form instead —
  submit it to get a Test token immediately.
- **`GOOGLE_ADS_LOGIN_CUSTOMER_ID`** — your Manager/MCC account's 10-digit
  Customer ID, shown top-right of Google Ads (next to your account) or in the
  page URL as `cid=`/`ocid=`. **Enter digits only** (`1234567890`), not the
  dashed `123-456-7890` shown in the UI. This is the account you authenticate
  *through*; `GOOGLE_ADS_CUSTOMER_ID` is the specific ad account being audited
  (the same value if you don't use a manager account).

**Note on GA4 auth.** GA4 supports OAuth user credentials (what the wizard
sets up) but a service-account JSON is more reliable for a cron context — it
doesn't expire. To create one: Google Cloud Console → APIs & Services →
Credentials → Create service account → grant it the **Viewer** role on the
GA4 property (GA4 → Admin → Property Access Management → add by email). Then
download the JSON and point `GOOGLE_APPLICATION_CREDENTIALS` at it.

If both are set, the service account wins.

### Meta Ads

1. Create a Marketing API app: https://developers.facebook.com/apps/
2. Generate a System User access token. **Required scopes**:
   - `ads_read` — for the audit
   - `ads_management` — for Phase 2 mutations (pause/enable/budget change)
3. Find your ad account id (it looks like `act_1234567890`).

```
META_ACCESS_TOKEN=...
META_AD_ACCOUNT_ID=act_1234567890
```

The official Meta Ads CLI (announced 2026-04-29) wraps the same API; we hit
the Marketing Graph directly so we don't depend on a tool that's still moving.
If Meta ships a stable MCP shim later, swap it into `skill/mcp-servers/meta-ads/`.

### LinkedIn Ads

1. Apply for the **Marketing Developer Platform — Advertising API** product
   at https://www.linkedin.com/developers/apps. Approval takes 1–5 business
   days. **Start this now if you haven't.**
2. Once approved, create an app, then run the auth CLI in the vendored MCP
   (already built by `install.sh`):

```bash
cd skill/mcp-servers/linkedin-ads
node dist/auth-cli.js
```

Copy the credentials into `.env`:

```
LINKEDIN_ACCESS_TOKEN=...
LINKEDIN_AD_ACCOUNT_URN=urn:li:sponsoredAccount:1234567890
```

### Recommendations (optional)

To upgrade the report's recommendations section from rule-based to
LLM-driven, set one of:

```
OPENROUTER_API_KEY=sk-...      # Nemotron 3 Super free tier — preferred
ANTHROPIC_API_KEY=sk-ant-...   # Claude Haiku — fallback if OpenRouter unset
```

If neither is set the rule-based recommender runs (flags zero-conversion
spend and scaled-down winners). If an LLM call fails mid-flight the
recommender falls through to the rule-based output — the section is
never blanked.

## 4. Telegram

The OpenClaw server already has `TELEGRAM_BOT_TOKEN` set for the bundled
`@openclaw/telegram` extension (v2026.5.7). AdLoops re-uses it. You must add
one new variable: which chat to send to.

```
ADLOOPS_TELEGRAM_CHAT_ID=-1001234567890
```

You can find the chat id by sending a message to the bot in the target chat,
then hitting:

```bash
curl "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates" | jq '.result[-1].message.chat.id'
```

## 5. Preflight — run this before scheduling

Once you've wired credentials and dropped `brand.json` in place, run:

```bash
cd skill && ../.venv/bin/python -m scripts.run --check
```

The preflight does four things, each one line of output:

1. Loads and validates `brand.json` (catches missing personas, malformed
   guardrails block, etc.).
2. Tries to construct each enabled platform's read client to confirm env
   vars are present (no live API call, so no quota cost).
3. Tries to construct the GA4 client — surfaces a `[warn]` if missing,
   not a `[FAIL]`, since enrichment is optional.
4. Hits the Telegram bot API live: `getMe` confirms the token is valid,
   `getChat` confirms the bot is actually a member of your chat. This
   catches the single most common first-run mistake (right token, wrong
   chat id).

Exit code is `0` if everything's green, `1` if anything's red. Wire this
into a deploy gate if you like.

Note: `--check` does NOT make live API calls to Google Ads / Meta /
LinkedIn — for that, run `cd skill && ../.venv/bin/python -m scripts.run --dry-run`,
which exercises every read client end-to-end without applying any
mutations.

## 6. Cron / scheduling

Suggested cadence per spec: **Tuesday and Friday at 09:00 local time**. On the
OC server (Linux):

```bash
crontab -e
```

Add (assuming server TZ matches user TZ — confirm with `date`):

```
0 9 * * 2,5 cd /home/ubuntu/adloops/skill && /home/ubuntu/adloops/.venv/bin/python -m scripts.run >> /home/ubuntu/.openclaw/logs/adloops.log 2>&1
```

If you use OC's cron skill instead, the equivalent shell command is:

```bash
cd /home/ubuntu/adloops/skill && ../.venv/bin/python -m scripts.run
```

## 7. Operational

- **Audit log**: every guardrail decision (AUTO / APPROVAL / REJECTED) writes
  a JSONL line to `~/Syncthing/adloops-brand/campaigns/.audit.jsonl`. Read it
  after every run in the first weeks of Phase 2 to catch anything weird.
  Each line includes `run_id`, `platform`, `campaign_id`, `kind`, `decision`,
  `rule`, `before`, `after`, `applied`.
- **Snapshots**: live in `~/Syncthing/adloops-brand/campaigns/.archive/<run_id>.json`.
  Used as the diff baseline for the next run. Failed runs (no platform fetched)
  do not persist a snapshot — they'd poison the diff.
- **Approve a queued mutation**: when a budget change exceeds the cap or a
  new campaign would launch non-PAUSED, the proposal lands in "Pending
  approvals" with a copy-paste `--approve <run_id>:<index>` command. The
  re-check uses the *current* brand config and audit context, so a 30%
  change on Tuesday may pass on its own by Friday.
- **Exit codes**: 0 ok · 2 brand config bad · 3 no platforms enabled · 4
  audit crashed · 5 Telegram unreachable · 6 Telegram send failed · 7 every
  enabled platform errored · 8 `--approve` spec malformed · 9 `--approve`
  row not found · 10 `--approve` re-check still APPROVAL · 11 `--approve`
  dispatch failed.

## 8. Things still on Michiel's plate

- [ ] LinkedIn MDP application submitted (1–5 day approval)
- [ ] Google Ads developer token applied for
- [ ] OAuth + GA4 wizard run (`uv run adloop init` in `mcp-servers/adloop`)
- [ ] GA4 service-account JSON created and `GOOGLE_APPLICATION_CREDENTIALS` set (preferred over OAuth for cron)
- [ ] GA4 property granted Viewer access to the service account
- [ ] Meta Marketing API system user token captured
- [ ] `ADLOOPS_TELEGRAM_CHAT_ID` set in OC env
- [ ] ICP filled into `brand.json`
- [ ] Confirmed Syncthing share name and mount path on the OC server
