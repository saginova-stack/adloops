# AdLoops setup

This is the operator-facing setup guide. The skill itself is documented in
`skill/SKILL.md`. Read both.

## 0. What you're getting in Phase 1

Read-only audit of Google / Meta / LinkedIn ads, every Tuesday and Friday at
09:00 local, posted to Telegram via the existing OpenClaw bot. Mutations
(±20% cap) come in Phase 2; LinkedIn mutations in Phase 3.

## 1. Bootstrap

```bash
cd /home/ubuntu/adloops
git submodule update --init   # vendored MCP servers
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pytest tests/ -q
```

All tests pass before you continue. If they don't, stop.

## 2. Brand directory

The skill expects the brand directory at `~/Syncthing/adloops-brand/`. **It does
not exist on this server yet** — the user (Michiel) needs to confirm the actual
Syncthing share name. Two options:

- If Syncthing is mounted, override with `ADLOOPS_BRAND_DIR=/path/to/share/adloops-brand`.
- Or scaffold a placeholder locally to test:

```bash
.venv/bin/python -m scripts.run --scaffold
```

This creates `assets/{logo,linkedin,screenshots}/`, `campaigns/.archive/`, and
copies `references/brand.example.json` into place as `brand.json`. **Edit it
before running an audit** — the loader rejects empty `icp.personas`.

## 3. Platform credentials

You need at least one of these to get a non-empty report.

### Google Ads

Apply for a developer token: https://developers.google.com/google-ads/api/docs/get-started/dev-token

Then create OAuth credentials at https://console.cloud.google.com/apis/credentials.
The `adloop` MCP we vendored has a built-in `adloop init` wizard that walks you
through the OAuth dance — easiest path:

```bash
cd skill/mcp-servers/google-ads
uv sync && uv run adloop init
```

When done, copy the values into your `.env`:

```
GOOGLE_ADS_DEVELOPER_TOKEN=...
GOOGLE_ADS_CLIENT_ID=...
GOOGLE_ADS_CLIENT_SECRET=...
GOOGLE_ADS_REFRESH_TOKEN=...
GOOGLE_ADS_LOGIN_CUSTOMER_ID=1234567890   # MCC if applicable, else operating account
GOOGLE_ADS_CUSTOMER_ID=1234567890         # operating account, if different from login
```

### Meta Ads

1. Create a Marketing API app: https://developers.facebook.com/apps/
2. Generate a System User access token with `ads_read` scope (and `ads_management` for Phase 2).
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
2. Once approved, create an app, then run the auth CLI in the vendored MCP:

```bash
cd skill/mcp-servers/linkedin-ads
npm install && npm run build
node dist/auth-cli.js
```

Copy the credentials into `.env`:

```
LINKEDIN_ACCESS_TOKEN=...
LINKEDIN_AD_ACCOUNT_URN=urn:li:sponsoredAccount:1234567890
```

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

## 5. Cron / scheduling

Suggested cadence per spec: **Tuesday and Friday at 09:00 local time**. On the
OC server (Linux):

```bash
crontab -e
```

Add (assuming server TZ matches user TZ — confirm with `date`):

```
0 9 * * 2,5 cd /home/ubuntu/adloops && /home/ubuntu/adloops/.venv/bin/python -m scripts.run >> /home/ubuntu/.openclaw/logs/adloops.log 2>&1
```

If you use OC's cron skill instead, the equivalent shell command is:

```bash
cd /home/ubuntu/adloops && .venv/bin/python -m scripts.run
```

## 6. Operational

- **Audit log**: every mutation (Phase 2+) writes a JSONL line to
  `~/Syncthing/adloops-brand/campaigns/.audit.jsonl`. Read it after every run
  in the first weeks of Phase 2 to catch anything weird.
- **Snapshots**: live in `~/Syncthing/adloops-brand/campaigns/.archive/<run_id>.json`.
  Used as the diff baseline for the next run. Failed runs (no platform fetched)
  do not persist a snapshot — they'd poison the diff.
- **Exit codes**: 0 ok, 2 brand config bad, 3 no platforms enabled, 4 audit
  crashed, 5 Telegram unreachable, 6 Telegram send failed, 7 every enabled
  platform errored.

## 7. Things still on Michiel's plate

- [ ] LinkedIn MDP application submitted (1–5 day approval)
- [ ] Google Ads developer token applied for + OAuth refresh token captured
- [ ] Meta Marketing API system user token captured
- [ ] `ADLOOPS_TELEGRAM_CHAT_ID` set in OC env
- [ ] ICP filled into `brand.json`
- [ ] Confirmed Syncthing share name and mount path on the OC server
