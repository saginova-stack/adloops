# Onboarding AdLoops — zero to first report

A guided path from a fresh clone to a first safe audit, with the gotchas that
actually bite called out inline. For exhaustive per-credential detail, this doc
links to [`setup.md`](./setup.md); read this one first for the *order* and the
*traps*.

**The golden rule:** observe before you mutate. Get a clean read-only audit
working, run `--no-mutate` for ~2 weeks, and only then let it change budgets.

---

## 0. Install the skill

```bash
git clone --recursive https://github.com/saginova-stack/adloops.git
cd adloops && ./install.sh      # uv + submodule build + .venv + runs tests
```

The installable skill is the **`skill/`** subdirectory; the repo root is the
build/dev harness. Run the entrypoint from inside `skill/`:

```bash
cd skill && ../.venv/bin/python -m scripts.run --check
```

**Installing into an agent harness (OpenClaw / Claude Code)?** Two traps we hit:

- **Symlinks may be rejected** ("symlink escape"). Install a **real copy** of
  `skill/` into the harness's skills dir, and **re-sync it from the repo after
  every update** (a copy silently drifts from git otherwise). An rsync that
  excludes `.venv/`, `.git/`, `__pycache__/` does the job.
- **The copy needs its own deps.** Give the copy its own venv with
  `requirements.txt` installed (or point invocations at the repo's `.venv`).
  System Python won't have the SDKs and is externally-managed (PEP 668).

---

## 1. Brand config

```bash
cd skill && ../.venv/bin/python -m scripts.run --scaffold
```

Edit the created `brand.json` (default `~/.adloops/brand/brand.json`; override
with `ADLOOPS_BRAND_DIR`). **`icp.personas` cannot be empty — the skill refuses
to run otherwise.** Set `guardrails.platforms` to enable only the platforms you
have creds for (e.g. `linkedin: true`, `google: false`, `meta: false`) so
`--check` doesn't fail on platforms you're not using yet.

---

## 2. Prerequisite most people miss: conversion tracking

AdLoops audits **whatever conversions the ad platform reports**. If the account
has no conversion tracking set up, the conversion/CPA signals are empty and the
proposer can't reason (e.g. "pause zero-conversion campaigns" would match
everything). **Before expecting a useful audit**, make sure each account has
working conversion tracking:

- **Google Ads:** a conversion action (or GA4 key events imported as
  conversions) firing on your real high-intent actions (signup, demo, contact).
- **GA4:** mark the relevant events (e.g. `generate_lead`, `sign_up`) as key
  events so they count.

A brand-new account/site with no measurement will produce an honest but empty
report until traffic + conversions accumulate.

---

## 3. Credentials, in recommended order

Full step-by-step per platform is in [`setup.md` §3](./setup.md). The
order and the gotchas:

### Google Ads + GA4 (most setup, do when you need it)
- **You need a Manager (MCC) account to get a developer token.** A regular ad
  account has no API Center. Create one at
  <https://ads.google.com/home/tools/manager-accounts>. Some creation choices
  are **irreversible** (primary use, company type) — see `setup.md`.
- **The OAuth wizard (`uv run adloop init`) opens a browser + localhost
  callback — which fails on a headless server.** Two ways through:
  - **SSH port-forward** the wizard's callback port to your laptop
    (`ssh -L <port>:localhost:<port> user@server`), run the wizard on the
    server, approve in your laptop browser.
  - **Manual OAuth:** build the auth URL with your client ID + the `adwords` +
    `analytics.readonly` scopes, approve in a browser, and redeem the
    `code=` from the callback URL. (This is what we ended up doing — the
    port-forward is cleaner if you can.)
- **`GA4_PROPERTY_ID`** lives in GA4 → Admin → Property Settings (numeric). To
  let tooling discover it programmatically, **enable the Analytics Admin API**
  in your Google Cloud project, or you'll get permission-denied.
- Maps to: `GOOGLE_ADS_DEVELOPER_TOKEN`, `GOOGLE_ADS_CLIENT_ID`,
  `GOOGLE_ADS_CLIENT_SECRET`, `GOOGLE_ADS_REFRESH_TOKEN`,
  `GOOGLE_ADS_LOGIN_CUSTOMER_ID` (the MCC), `GOOGLE_ADS_CUSTOMER_ID` (the ad
  account), `GA4_PROPERTY_ID`, optional `GOOGLE_APPLICATION_CREDENTIALS`.

### Meta (usually the fastest)
- System User token in Business Settings → Users → System Users, assigned to the
  ad account. Scope `ads_read` for the audit; add `ads_management` only when you
  want it to actually change budgets.
- Maps to: `META_ACCESS_TOKEN`, `META_AD_ACCOUNT_ID` (`act_…`).

### LinkedIn (may be approval-gated)
- The **Advertising API at Development Tier** is enough to read *and* write your
  own managed ad accounts (Standard Tier only needed for client accounts you
  don't admin). Add the Advertising API product to your app.
- **The auth CLI is also headless-unfriendly** (browser + localhost callback) —
  SSH-port-forward it, then:
  ```bash
  cd skill/mcp-servers/linkedin-ads && node dist/auth-cli.js
  ```
- Maps to: `LINKEDIN_ACCESS_TOKEN`, `LINKEDIN_AD_ACCOUNT_URN`
  (`urn:li:sponsoredAccount:<digits>`).

---

## 4. Telegram (where the report goes)

The bot token (`TELEGRAM_BOT_TOKEN`) is read from the environment. Get the chat
id and set `ADLOOPS_TELEGRAM_CHAT_ID` — see `setup.md` for the `getUpdates`
trick.

---

## 5. Validate, then go live safely

```bash
cd skill
../.venv/bin/python -m scripts.run --check      # all green? brand + per-platform env + live Telegram ping
../.venv/bin/python -m scripts.run --dry-run    # live reads, previewed (not applied) writes
```

Then schedule **observation-only** for ~2 weeks (touches nothing live):

```cron
0 9 * * 2,5  cd /path/to/adloops/skill && ../.venv/bin/python -m scripts.run --no-mutate
```

Read the report each run. When you trust the proposer, **drop `--no-mutate`** to
let guardrailed mutations apply. On a non-USD account, eyeball the first real
budget change before trusting auto-pilot.

---

## Secret handling

Onboarding produces several secrets (developer token, OAuth client secret,
platform tokens). Keep them in a `.env` / secret store with tight permissions,
**never paste them into chat or commit them**, and **rotate anything that was
exposed** (e.g. a client secret shown in a screenshot).

---

## Gotchas quick-reference (the things that actually bit us)

| Trap | Fix |
|---|---|
| Agent harness rejects symlinked skill | Install a real copy; re-sync from repo on updates |
| Skill copy runs on system Python (missing SDKs) | Give the copy its own venv with `requirements.txt` |
| `python -m scripts.run` fails from repo root | Run from `skill/` (or `python skill/scripts/run.py`) |
| No API Center in Google Ads | You're in a regular account — create a Manager (MCC) account |
| OAuth wizard / LinkedIn auth-cli hang on a headless box | SSH-port-forward the callback, or do manual OAuth |
| Empty conversions / CPA in the report | Set up conversion tracking first (§2) |
| GA4 property ID permission-denied | Enable the Analytics Admin API |
| `--check` fails on a platform you're not using | Disable it in `brand.json` `guardrails.platforms` |
| Secrets pasted during setup | Rotate them once things work |
