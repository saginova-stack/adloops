# AdLoops — Development Log

Build log for the AdLoops OpenClaw skill. One section per phase.
Source spec lives in the original build prompt (Tuesday/Friday cadence,
3-platform read+write, Telegram delivery via OC's existing bot, ±20%
guardrails, Syncthing-backed brand directory).

## Phase 1 — read-only audit + Telegram report ✅

**Commits:**
- `e8d3a6a` — initial Phase 1 (read-only audit, Telegram delivery, guardrails module unwired)
- `8a57867` — rename `mcp-servers/google-ads` → `mcp-servers/adloop`, `install.sh`, GA4 env documented
- *(this commit)* — GA4 cross-reference enrichment: consent gap, attribution gap, real CPA, new "Tracking & consent" report section

**Date:** Phase 1 read-only landed 2026-05-10. GA4 cross-reference landed 2026-05-11.
**Test status:** 81 passing (`.venv/bin/pytest tests/ -q`).

### What ships

| File | Purpose |
|------|---------|
| `skill/SKILL.md` | OC skill manifest + procedural docs. `primaryEnv: TELEGRAM_BOT_TOKEN`, all platform creds listed in `optionalEnv`. |
| `skill/scripts/run.py` | Entrypoint (`python -m scripts.run`). Parses `--scaffold` / `--dry-run`, returns explicit exit codes per failure mode. |
| `skill/scripts/paths.py` | Resolved on-disk paths — brand dir, archive dir, audit log, references. Single source of truth for filesystem layout. |
| `skill/scripts/brand_loader.py` | Loads + validates `brand.json`. Refuses to run if `icp.personas` empty. `ensure_scaffold()` creates the directory layout + copies the example file as a starting brand. |
| `skill/scripts/guardrails.py` | Mutation guardrails — ±N% budget cap, new-campaigns-must-be-paused, cross-platform spend ceiling, append-only audit log. **Fully implemented & tested but not yet wired** (Phase 1 is read-only; Phase 2 wires this in front of every mutation tool call). |
| `skill/scripts/mcp_clients.py` | Read clients for Google Ads (google-ads SDK), Meta (Marketing Graph API), LinkedIn (Marketing API). Each exposes a single `fetch_perf_7d()` returning a normalized `CampaignPerf` dataclass. |
| `skill/scripts/audit.py` | The audit cycle — fetch all → diff vs prior snapshot → top movers → snapshot. Returns an `AuditReport` for the renderer. |
| `skill/scripts/telegram_report.py` | Formats `AuditReport` into the spec'd 6-section Telegram message, splits ≤3000 chars per chunk, sends via `TELEGRAM_BOT_TOKEN`. |
| `skill/references/brand.schema.json` | JSON Schema for `brand.json`. |
| `skill/references/brand.example.json` | Filled-in example used as scaffold seed. |
| `skill/mcp-servers/adloop/` | Submodule → `kLOsk/adloop @ v0.7.0`. Combined Google Ads + GA4 MCP. |
| `skill/mcp-servers/linkedin-ads/` | Submodule → `danielpopamd/linkedin-ads-mcp @ 05a2761` (no releases yet — pinned to commit SHA). |
| `install.sh` | One-shot first-run bootstrap (uv install + adloop sync + LinkedIn build + skill venv). |
| `tests/` | 56 tests across 5 test files. |
| `setup.md` | Operator-facing setup: cred application, env vars, cron entry, exit codes. |
| `README.md` | Repo-level overview + quick start. |
| `requirements.txt` | `google-ads`, `pytest`, `jsonschema`. SDKs imported lazily in `mcp_clients` so the test suite passes without them. |

### Design decisions worth keeping

1. **Headless Python pipeline, not LLM-orchestrated.** The cron use case wants a deterministic process: fetch → diff → render → send. Putting an LLM in the hot path would make every run ~$0.05 and turn timeouts into bug reports. Phase 2 will use an LLM (or Nemotron via OpenRouter) for the *recommendations* section only — everything else stays deterministic.
2. **Direct API calls in Phase 1, MCP servers in Phase 2 (hybrid).** The vendored MCPs (kLOsk/adloop, danielpopamd/linkedin-ads-mcp) are designed for an LLM client. For a one-shot read we don't need the MCP layer, so Phase 1 hits the Google Ads / GA4 / Meta / LinkedIn APIs directly and computes the cross-reference signals (consent gap, attribution gap, real CPA) ourselves. The vendored MCPs are still installed via `install.sh` and become the canonical *write* path in Phase 2 because their preview/confirm pattern matches our guardrail flow exactly. We get the cross-reference value now without paying the subprocess/auth-state cost at cron time.
3. **GA4 is enrichment, not a platform.** GA4 is fetched only when Google Ads has a fetched result, and only enriches Google rows. If GA4 creds are missing or the API errors, we still report Google Ads numbers — the `PlatformResult.ga4_status` field surfaces *why* the cross-reference signals aren't available so the operator sees the gap.
4. **Meta has no public MCP shim.** Meta announced an official Ads CLI on 2026-04-29; their blog post doesn't link a public repo and GitHub search returns nothing under `org:facebook`. Decision: hit the Marketing Graph API directly with the same `META_ACCESS_TOKEN` + `META_AD_ACCOUNT_ID` contract Meta's CLI uses. When/if Meta ships a stable MCP, swap it into `skill/mcp-servers/meta-ads/` — `mcp_clients.MetaAdsClient` is the only file that changes.
5. **Skill installed via symlink.** `~/.openclaw/workspace/skills/adloops` → `~/adloops/skill/`. Lets us version the actual code in this repo while OC sees a normal skill directory. Submodules under `skill/mcp-servers/` are inside the symlinked tree, so the OC LLM sees them too.
6. **Guardrail decisions are tristate, not binary.** `Decision.AUTO` / `APPROVAL` / `REJECTED`. The "queue for human approval" path is the safety valve that prevents the LLM from talking its way around hard rules — even if it argues 25% is fine, the cap kicks the request to APPROVAL not AUTO.
7. **Snapshots are skipped on total-failure runs.** If every enabled platform errored, we don't write a snapshot, because a corrupt baseline poisons the next run's diff. Run exits with code 7 so the cron operator notices.
8. **Audit log key is `run_id` set in env, not generated per call.** The entrypoint stamps `ADLOOPS_RUN_ID` in `os.environ` so every guardrail decision in that run logs the same id. Lets us reconstruct an entire run from `.audit.jsonl`.
9. **Fail-loud, not fail-quiet, when brand.json is broken.** Loader raises `BrandConfigError`; entrypoint catches and routes to Telegram before exiting nonzero. Operator sees the message in the same place as a normal report.
10. **Cross-reference thresholds are tuned to the kLOsk/adloop defaults.** Consent gap flagged at ≥30% (EU traffic floor with normal GDPR consent rejection), attribution gap at ≥25% (Ads vs GA4 conversion deltas above this usually mean a real wiring issue, not noise), real-CPA drift at ≥25%. Defined as module constants in `telegram_report.py` so they're easy to tune from data.

### Test coverage

| Module | Tests | Notes |
|--------|-------|-------|
| `guardrails.py` | 17 | Every rule + the audit log. Includes the divide-by-zero edge case (0 → positive = "infinite %" → APPROVAL). |
| `brand_loader.py` | 9 | Valid load, missing file, invalid JSON, empty personas, missing company.name, bad pct, missing platform keys, scaffold creation, scaffold idempotency. |
| `audit.py` | 13 | Deltas with/without prior, top movers, snapshot write/read, latest-archive selection, recommendations, fetch-all platform skipping, missing-creds path, plus GA4 enrichment (join, missing creds, zero matches, API error, skip-when-google-disabled). |
| `mcp_clients.py` | 13 | `CampaignPerf` cross-reference properties (consent gap, attribution gap, real CPA — all edge cases) + `GoogleAnalyticsClient` env validation (raw/prefixed property id, missing creds, OAuth fallback). |
| `telegram_report.py` | 17 | Header rendering, disabled/error platform lines, top movers section, recommendations, no-actions message, chunk splitting (intact + oversized), env-var validation, dry-run preview, max-message cap, plus the Tracking & consent section (consent gap flagged, attribution gap flagged, CPA drift flagged, omitted when below threshold, GA4 skip reason surfaced, Meta/LinkedIn campaigns ignored). |
| `run.py` | 7 | Scaffold mode, brand-missing → exit 2, empty personas → exit 2, no platforms enabled → exit 3, happy dry-run → exit 0, audit crash → exit 4, all-platforms-failed → exit 7. |

The mock surface is intentionally narrow: `audit.run_audit` and `telegram_report.send_messages` are monkeypatched in entrypoint tests; everything else is real code. We do *not* mock the API clients — those are exercised by the smoke test only (manual, with real creds).

### Smoke tests run during development

```bash
# scaffold (creates /tmp/adloops-final/brand.json from example)
ADLOOPS_BRAND_DIR=/tmp/adloops-final python -m scripts.run --scaffold

# fill placeholder personas, enable platforms
# ... edited brand.json to set personas + platforms.{google,meta,linkedin}=true

# dry run with no creds — should emit ERROR lines per platform
ADLOOPS_BRAND_DIR=/tmp/adloops-final TELEGRAM_BOT_TOKEN=fake \
  ADLOOPS_TELEGRAM_CHAT_ID=1 python -m scripts.run --dry-run
# rc=7, all three platforms surface "missing env" in the rendered report
```

What was *not* smoke-tested (no creds available):
- Live Google Ads `searchStream` query — only the SDK call shape was verified.
- Live GA4 `runReport` against `sessionGoogleAdsCampaignId` — only the request shape and response parsing are verified by unit tests with stubbed responses.
- Live Meta Marketing Graph paged read — `_fetch_paged` is exercised in unit tests at the structural level only.
- Live LinkedIn `adAnalytics` query — same.
- Real Telegram send — the formatter and chunking are tested; the HTTP path (`urlopen` to api.telegram.org) is not.

These are the explicit gaps for Phase 1 acceptance. The cron operator should run one `--dry-run` with creds in place to validate end-to-end before relying on the schedule.

### Things that broke during the build (and the fix)

1. **f-string with backslash escape in `_platform_summary_line`.** Python ≤3.12 doesn't allow `\"` inside an f-string. Fix: extract `cpa` and `roas` to local vars before the f-string.
2. **`datetime.utcnow()` deprecation warnings.** Switched to `datetime.now(timezone.utc)` in `audit.py` and `run.py`.
3. **`pytest` blocked by PEP 668.** System Python is externally managed (Ubuntu 24+ behavior). Created `.venv` and installed deps there.
4. **Submodules landed in `/home/ubuntu/.git`, not `/home/ubuntu/adloops/.git`.** The home dir had a pre-existing empty git repo with branch `master`, no commits. My first `git submodule add` registered the submodules there. **Recovery:** reset the home repo (cleared its index + `.git/modules/{google-ads,linkedin-ads}` + removed the two `submodule.*` config sections), put HEAD back on master, removed the cloned submodule trees from `adloops/skill/mcp-servers/`, ran `git init -b main` inside `adloops/`, then re-added both submodules. No work lost.

### Submodule pins — full SHAs

```
skill/mcp-servers/adloop         c3c8067e2e862255884a641b4816669291258375  (tag: v0.7.0)
skill/mcp-servers/linkedin-ads   05a27618628408af263ac56a1ca62a8aef404718  (no tag)
```

### Repo state at end of Phase 1

```
.
├── README.md
├── RESUME.md
├── development.md          ← this file
├── install.sh              ← first-run bootstrap (uv install + submodule build + venv)
├── requirements.txt
├── setup.md
├── skill/
│   ├── SKILL.md
│   ├── mcp-servers/
│   │   ├── adloop/          [submodule — Google Ads + GA4 MCP, kLOsk/adloop @ v0.7.0]
│   │   └── linkedin-ads/    [submodule — danielpopamd/linkedin-ads-mcp @ 05a2761]
│   ├── references/
│   │   ├── brand.example.json
│   │   └── brand.schema.json
│   └── scripts/
│       ├── __init__.py
│       ├── audit.py
│       ├── brand_loader.py
│       ├── guardrails.py
│       ├── mcp_clients.py
│       ├── paths.py
│       ├── run.py
│       └── telegram_report.py
└── tests/
    ├── __init__.py
    ├── conftest.py
    ├── test_audit.py
    ├── test_brand_loader.py
    ├── test_guardrails.py
    ├── test_mcp_clients.py
    ├── test_run_entrypoint.py
    └── test_telegram_report.py
```

Skill symlinked: `/home/ubuntu/.openclaw/workspace/skills/adloops -> /home/ubuntu/adloops/skill/`.

---

## Phase 2 — guardrailed Meta + Google mutations ✅

**Commits:**
- `7f43f7f` — mutation proposer (`scripts/mutations.py`)
- `d308e85` — synchronous MCP stdio client (`scripts/mcp_runner.py`)
- `9288418` — Google + Meta executors (`scripts/executors/`)
- `9a44ad7` — wired into `run.py` + `--approve` mode + report renderer rewrite
- *(latest)* — LLM recommender chain (`scripts/recommender.py`) + docs

**Date:** 2026-05-11.
**Test status:** 178 passing.

### What ships

| Module | Purpose |
|--------|---------|
| `scripts/mutations.py` | Pure `propose(report, brand) → list[Mutation]`. Three rules: pause zombies (≥$50 spend + 0 conv), decrease budget −cap% on CPA spike (≥50% growth), increase budget +cap% on CPA drop (≥25% drop + conversion lift). Capped at 10 proposals/run; one mutation per campaign max. |
| `scripts/mcp_runner.py` | Hand-rolled synchronous JSON-RPC stdio client for MCP servers. Handles initialize handshake, tools/call, isError responses, unsolicited notifications mid-flight, early stream close. IO is injectable so tests use BytesIO instead of spawning. |
| `scripts/executors/google.py` | `GoogleExecutor` context manager — spawns the adloop MCP once and reuses the session across mutations in the same run. Dispatches via the adloop preview/confirm pattern: `pause_entity` / `enable_entity` / `update_campaign` → `confirm_and_apply`. dry_run propagates to confirm_and_apply (and adloop's own `safety.require_dry_run` config override is preserved). |
| `scripts/executors/meta.py` | Direct Marketing Graph API. No native preview — dry_run mode returns the would-be HTTP call. Budget changes encoded as cents per Meta's wire format. |
| `scripts/executors/__init__.py` | `dispatch(mutation, dry_run=False)` routes by platform. LinkedIn raises a clear "Phase 3 — blocked on MDP approval" error. |
| `scripts/recommender.py` | LLM chain for the recommendations section: OpenRouter (Nemotron 3 Super free tier) → Anthropic (Claude Haiku) → rule-based fallback. Each LLM call is wrapped in try/except so a 5xx never blanks the section. Prompt is token-conservative (~600 tokens): brand voice, per-platform totals, top movers, GA4 cross-reference flags. |
| `scripts/run.py` | New mutation pipeline between audit and report: propose → guardrails → dispatch AUTO (Google batched on one session, Meta per call) → queue APPROVAL → log REJECTED. New CLI flags: `--no-mutate`, `--approve <run_id>:<index>`. |
| `scripts/telegram_report.py` | New "Actions taken" row renderer with applied/dry-run/failed states + $before → $after for budget changes. Pending approvals now include the exact copy-paste `--approve` command. |

### Phase 2 design decisions

1. **Hand-rolled MCP client over the `mcp` PyPI package.** Saves the async surface (we run synchronously per cron) and ~150kB of dependencies. Wire protocol is small enough that a synchronous JSON-RPC implementation is ~150 lines. Tests inject byte streams instead of spawning subprocesses, which makes the test suite hermetic.
2. **One MCP session per Google batch.** kLOsk/adloop spawns take a few seconds (uv warmup + OAuth refresh). With cap=10 mutations per run that's a meaningful saving. The `GoogleExecutor` context manager owns the lifecycle; for one-offs (or `--approve`) the module-level `dispatch()` helper spawns and tears down.
3. **APPROVAL surface via the report.** Each pending approval lands in the Telegram message with its exact `--approve <run_id>:<index>` command. Operator copy-pastes; the re-check uses current brand config (a 30% change on Tuesday might be 18% on Friday and clear the cap on its own).
4. **Cross-platform ceiling computed per-mutation, not aggregated.** `projected_total_spend(report, [m])` computes the projected total if just this mutation applied. We don't accumulate AUTO decisions for the ceiling check — the per-mutation cap rule fires first in almost every realistic case. If this ever proves wrong we'll move to a two-pass model.
5. **LLM degrades to rules silently.** Any exception from OpenRouter/Anthropic falls through to the rule-based recommender, no operator visibility needed — we'd rather a useful section than a "LLM unavailable" placeholder.
6. **`--approve` re-runs guardrails against current config but skips the cross-platform ceiling.** The ceiling needs the full audit context which `--approve` doesn't reconstruct. Operators using `--approve` are explicitly opting in — the per-mutation cap is still enforced.
7. **Mutation pipeline crashes don't crash the run.** A bug in proposer/executor still ships the audit + report. Failure surfaces on stderr.

### Test coverage added in Phase 2

| Module | Tests | Notes |
|--------|-------|-------|
| `mutations.py` | 16 | All three rules + edge cases (already-paused, no daily_budget, drop below threshold, conversions flat). Dedup (pause wins over budget change), max-per-run cap, skip-on-fetch-failure, projected_total with overrides + pauses. |
| `mcp_runner.py` | 11 | Handshake order, idempotent initialize, missing protocolVersion, JSON-RPC error frames, mismatched response id, stream closed mid-request, unsolicited progress notifications, raw-text + JSON-text content unwrap. |
| `executors/` | 17 | Google: pause/enable/budget_change tool routing, dry_run propagation, non-positive budget rejection, missing plan_id, unsupported kind, inactive session. Meta: pause/enable/budget cents encoding, dry_run path doesn't call HTTP, missing token, unsupported kind. Top-level: dispatcher routing + LinkedIn rejection. |
| `run.py` Phase 2 cases | 9 | Mutations integration (proposer calls executor), --no-mutate flag, mutation crash doesn't crash run, APPROVAL queue, --approve invalid spec / not found / out of range / still-approval / now-auto-dispatches. |
| `telegram_report.py` Phase 2 cases | 5 | Applied row, dry-run row, failed row with error, budget change detail rendering, pending approvals with `--approve` command. |
| `recommender.py` | 9 | Fallback when no keys, OpenRouter happy path with auth header check, OpenRouter failure falls through to Anthropic, all LLMs fail falls through to rules, prompt contains brand voice + window + consent gap, bullet/numbered list parsing, max-recs cap, header line drop. |

### Smoke tests run

```bash
# scaffold against /tmp
ADLOOPS_BRAND_DIR=/tmp/adloops-smoke python -m scripts.run --scaffold
# dry run with one platform enabled (no creds) → exit 7 with structured error report
ADLOOPS_BRAND_DIR=/tmp/adloops-smoke TELEGRAM_BOT_TOKEN=fake ADLOOPS_TELEGRAM_CHAT_ID=1 \
  python -m scripts.run --dry-run
```

Not smoke-tested (no creds):
- Live adloop MCP spawn + `confirm_and_apply` — only the wire protocol is verified by unit tests.
- Live Meta Graph mutation POST — same.
- Live OpenRouter / Anthropic call — request shape verified with canned HTTP responses.

---

## Phase 3 — LinkedIn mutations (executor shipped, live calls pending auth)

---

What landed:

- `scripts/executors/linkedin.py` — a `LinkedInExecutor` context manager
  that spawns the vendored `danielpopamd/linkedin-ads-mcp` (Node, stdio)
  via `mcp_runner`. Unlike Google's adloop MCP, the LinkedIn MCP has no
  preview/confirm two-step — pause/enable/budget_change all flow through
  the single `update_campaign` tool with a partial-update payload.
  Semantically closer to Meta; transport-wise closer to Google.
- `scripts/executors/__init__.py:dispatch` routes `linkedin` to the new
  executor (the "Phase 3 — blocked on MDP approval" error is removed).
- `tests/test_executors.py` covers pause/enable/budget shape, URN →
  numeric ID stripping, explicit-currency passthrough, dry-run shape
  (no MCP call), missing `LINKEDIN_AD_ACCOUNT_URN`, unsupported kind,
  and inactive-session guard — same coverage envelope as Google/Meta.

Phase 3 design decisions:

1. **One context-manager executor with module-level `dispatch()` for
   one-offs.** Mirrors Google's structure so when batching becomes
   worth wiring into `run.py` it's a call-site change, not an executor
   rewrite. `executors.dispatch()` from `run.py`'s `_dispatch_one()`
   spawn-tears-down per mutation today — fine for the common case,
   noted in `RESUME.md` as a follow-up.
2. **All three kinds → `update_campaign`.** The MCP's tool surface
   covers pause/enable/budget via different fields on one call. Splitting
   into pseudo-tools would have meant matching against e.g.
   `pause_campaign` which the MCP doesn't expose. Mapping is in
   `_to_tool_args` so future kinds (audience, schedule) layer on
   without restructuring.
3. **URN ↔ numeric ID stripping happens in the executor**, not in the
   audit. The audit keeps the URN (it's the natural key on the LinkedIn
   API) so reports, snapshots, and `.audit.jsonl` stay consistent across
   read/write paths. The executor strips on the way out.
4. **Currency defaults to the MCP's USD default, with explicit
   `mutation.after['currency']` passthrough.** The audit doesn't capture
   currency today; rather than block on lifting that into the data
   model, the executor honors an opt-in field and otherwise lets the
   MCP default. Non-USD accounts surface as an API error from LinkedIn
   if there's a currency mismatch — preferred over silent miscoercion.
5. **Tokens are the MCP's problem.** The executor doesn't manage
   OAuth — the LinkedIn MCP refreshes via its on-disk token store using
   `LINKEDIN_CLIENT_ID`/`LINKEDIN_CLIENT_SECRET` from inherited env.
   This keeps the executor stateless and matches how Google's executor
   delegates to adloop.

Live-calls prerequisites are documented in `setup.md` §3.4 and `RESUME.md`.

---

## Phase 4 — creative generation (out of scope; separate spec)

Not addressed by this repo. If/when added, it should land as a sibling skill (`adloops-creative`) that consumes the same `brand.json` rather than entangling with the audit cycle.
