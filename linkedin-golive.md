# LinkedIn Go-Live — Handoff (pick up on the Oracle VPS)

**Goal:** make AdLoops issue *real* LinkedIn API calls, validate with one
live `--dry-run`, then schedule observation-only (`--no-mutate`).

**Status when this was written:** LinkedIn Advertising API access granted
(Development Tier). All code is in place; the only remaining work is
credential/config + one real validation run. This doc is self-contained —
follow it top to bottom on the Oracle VPS (`/home/ubuntu/adloops`).

Scope of this handoff is **LinkedIn + Telegram only.** Google/Meta wiring
is deliberately out of scope (see `nextsteps.md` if you want them later).

---

## What was already done on the laptop (so you don't redo it)

- **Forked** the LinkedIn MCP to a repo we control:
  `saginova-stack/linkedin-ads-mcp` (upstream `danielpopamd/linkedin-ads-mcp`
  kept as the `upstream` remote for future syncs).
- **Trimmed the OAuth scopes** to `['rw_ads', 'r_ads_reporting']` on the
  fork's `main` (commit `996b080`). The upstream MCP also requested
  `r_organization_social` + `w_organization_social`, which are granted by
  the **Community Management API** product — which this app is NOT
  provisioned for. Requesting them made LinkedIn's `/authorization`
  endpoint reject the whole flow with `unauthorized_scope_error`, so
  `auth-cli` could never get a token. AdLoops only needs `rw_ads`
  (for `update_campaign`) + `r_ads_reporting` (for analytics), so this is
  the correct minimal set.
- **Repointed the submodule** in this repo to the fork and bumped its
  pinned commit to `996b080`. 198 tests still pass.

So: the auth-blocking gotcha is already fixed in the code you're about to
pull. You just need to get the submodule moved on the server, then auth.

---

## Step 0 — Pull + move the submodule to the fork (DO THIS FIRST)

When you `git pull` on the VPS, the new `.gitmodules` url does **not**
auto-apply — git keeps the old upstream url in `.git/config`, and
`install.sh` only *inits* a missing submodule, it won't re-point an
existing one. So run this once after pulling:

```bash
cd /home/ubuntu/adloops
git pull origin main
git submodule sync skill/mcp-servers/linkedin-ads
git submodule update --init skill/mcp-servers/linkedin-ads   # moves to 996b080
```

Verify you're on the fork at the trimmed commit:

```bash
cd skill/mcp-servers/linkedin-ads
git remote -v                       # origin should be saginova-stack/...
git rev-parse HEAD                  # should be 996b080...
grep "SCOPES =" src/auth/oauth.ts    # source is TypeScript; dist/ may not exist yet
# if dist/ is stale or missing, rebuild:
npm install --no-audit --no-fund && npm run build
grep "SCOPES =" dist/auth/oauth.js  # MUST read: ['rw_ads', 'r_ads_reporting']
cd /home/ubuntu/adloops
```

> If you ever do a clean `./install.sh` instead, that's fine too — it
> runs `npm install && npm run build`, which compiles the trimmed scopes
> from the fork's `src/`. The submodule-sync above is only needed because
> the submodule was *already* checked out at the old url.

---

## Step 1 — LinkedIn app config (in the developer portal)

App: <https://www.linkedin.com/developers/apps> → your AdLoops app.

1. **Auth tab → Redirect URLs:** add exactly
   `http://localhost:3000/callback`
   (must match `LINKEDIN_REDIRECT_URI`; default is this value).
2. **Auth tab:** copy **Client ID** and **Client Secret**.
3. Confirm the **Advertising API** product shows as added (it is).

Export the OAuth app creds (the `auth-cli` needs them):

```bash
export LINKEDIN_CLIENT_ID=...        # from Auth tab
export LINKEDIN_CLIENT_SECRET=...    # from Auth tab
```

---

## Step 2 — Authenticate (the headless wrinkle)

`auth-cli` opens a browser and runs a callback server on
`localhost:3000`. The Oracle VPS is headless, so the browser can't open
there directly. Two options:

**Option A — SSH port-forward (recommended, keeps tokens on the VPS):**

```bash
# From your laptop, open a tunnel so localhost:3000 on the VPS
# is reachable in YOUR browser:
ssh -L 3000:localhost:3000 ubuntu@<oracle-vps>

# Then, inside that SSH session, on the VPS:
cd /home/ubuntu/adloops/skill/mcp-servers/linkedin-ads
node dist/auth-cli.js
# It prints an authorization URL. Paste that URL into your LAPTOP browser.
# Approve. The callback hits localhost:3000 → tunneled to the VPS server.
```

**Option B — auth on the laptop, copy the token file up:**

```bash
# On the laptop (has a browser), in the submodule dir with the same
# CLIENT_ID/SECRET exported:
node dist/auth-cli.js          # completes in the local browser
scp ~/.linkedin-ads-mcp/tokens.json ubuntu@<oracle-vps>:~/.linkedin-ads-mcp/tokens.json
```

Either way you end up with `~/.linkedin-ads-mcp/tokens.json` on the VPS.
The MCP (write path) auto-refreshes this file using CLIENT_ID/SECRET.

---

## Step 3 — Wire the AdLoops env vars

The **read path** (the audit) talks to the LinkedIn REST API directly and
needs a *static* access token in an env var — separate from the MCP's
token store. Pull it out of the token file:

```bash
export LINKEDIN_ACCESS_TOKEN=$(jq -r .access_token ~/.linkedin-ads-mcp/tokens.json)
export LINKEDIN_AD_ACCOUNT_URN=urn:li:sponsoredAccount:XXXXXXXXXX   # your account
# NB: LINKEDIN_AD_ACCOUNT_URN is required by BOTH the read path (campaign
# filter) and the write executor (strips it to the numeric accountId) —
# the executor raises if it's unset, even though it's set here under "reads".
```

> ⚠️ **Known limitation — token staleness.** `LINKEDIN_ACCESS_TOKEN` is a
> snapshot; LinkedIn access tokens expire in ~60 days. The MCP *write*
> path auto-refreshes, but the *read* path will start 401-ing in ~2
> months. For now: re-run the `jq` export when reads start failing.
> Proper fix (future, not blocking): make the read client pull from the
> refreshing token-store instead of an env var.

Telegram destination. `TELEGRAM_BOT_TOKEN` is *supposed* to be set on the OC
bot, but confirm it's actually exported in **this** shell first — if it isn't,
`--check` fails loud with `telegram: TELEGRAM_BOT_TOKEN not set`:

```bash
# Verify the bot token is present in this shell; export it if not:
printenv TELEGRAM_BOT_TOKEN >/dev/null || export TELEGRAM_BOT_TOKEN=<bot_token>

# Send any message to the bot in the target chat, then:
curl "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates" \
  | jq '.result[-1].message.chat.id'
export ADLOOPS_TELEGRAM_CHAT_ID=<that_id>
```

---

## Step 4 — Brand config (skill refuses to run without it)

```bash
cd /home/ubuntu/adloops/skill
../.venv/bin/python -m scripts.run --scaffold
# edit the brand.json it creates (default ~/.adloops/brand/brand.json,
# or set ADLOOPS_BRAND_DIR to an existing path) — fill icp.personas at minimum.
```

The run fails loud if `icp.personas` is empty — that's intentional.

---

## Step 5 — Preflight, then validate against real data

```bash
cd /home/ubuntu/adloops/skill

# Preflight: brand.json + per-platform env + live Telegram ping
../.venv/bin/python -m scripts.run --check

# Validate end-to-end: live LinkedIn reads + previewed (not applied) writes.
# This is the ONE thing the 198-test suite can't cover.
../.venv/bin/python -m scripts.run --dry-run
```

**Success criteria for the dry-run:**
- LinkedIn campaigns/analytics actually come back (no 401/403, no
  `unauthorized_scope_error`).
- The "would have fired" / preview section lists LinkedIn proposals with
  guardrail verdicts (AUTO / APPROVAL / REJECTED).
- No executor exceptions.

If you see `unauthorized_scope_error` here, the submodule didn't move to
the fork — go back to Step 0.

> **What `--dry-run` does NOT prove.** For an AUTO LinkedIn proposal it
> *spawns* the write MCP (so it catches a missing `node`/`dist/`), but it
> short-circuits before calling `update_campaign` — so it never exercises
> the write-side token store or OAuth refresh. A green dry-run does **not**
> guarantee writes work; the first real (non-`--dry-run`) run is the first
> true test of the write path. (`--dry-run` *does* write decision rows to
> `.audit.jsonl`; `--no-mutate` does not — see Step 6.)

---

## Step 6 — Schedule observation-only

Once the dry-run looks right, schedule `--no-mutate` (audit + report +
"would have fired" preview; touches nothing live):

```cron
0 9 * * 2,5  cd /home/ubuntu/adloops/skill && ../.venv/bin/python -m scripts.run --no-mutate
```

Run that for ~2 weeks, then drop `--no-mutate` to go live when you trust
the proposer. (Same rollout the other platforms follow — see `nextsteps.md`.)

> **Where to read the verdicts under `--no-mutate`.** `--no-mutate` writes
> **nothing** to `.audit.jsonl` — it only populates the "would have fired"
> section of the rendered Telegram/stdout report. Read *that* after each run
> to see the AUTO/APPROVAL/REJECTED decisions. (If you want a persisted
> decision log during observation, use `--dry-run` instead, which both
> previews and appends to `.audit.jsonl`.)

---

## Reminders / gotchas in one place

- **Dev Tier:** fine for an ad account you personally manage. Before
  pointing AdLoops at client accounts you don't admin, submit the app for
  **Standard Tier** review.
- **Submodule on the server** must be on `saginova-stack` fork @ `996b080`
  (Step 0). This is the #1 thing that will bite.
- **Two token sources:** env `LINKEDIN_ACCESS_TOKEN` (reads, static) vs
  `~/.linkedin-ads-mcp/tokens.json` (writes, auto-refresh). Don't conflate.
- **Conversions API / Lead Sync / Sign-In-with-LinkedIn** were also added
  to the app but AdLoops does not use them today. No action.

## Where things live

- Runbook (full operator setup): `setup.md`
- Post-Phase-3 next steps: `nextsteps.md`
- Claude-side resume state: `RESUME.md`
- LinkedIn executor: `skill/scripts/executors/linkedin.py`
- LinkedIn read client: `skill/scripts/mcp_clients.py` (`LinkedInAdsClient`)
- Fork: <https://github.com/saginova-stack/linkedin-ads-mcp> (`main` @ 996b080)
