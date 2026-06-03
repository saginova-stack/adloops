# Phase 4 — Creative Generation (spec / plan)

**Status:** planned, not started. Free + in-skill, architected to split into a
paid tier later. This doc is the "separate spec" that `development.md` Phase 4
pointed at.

**One-line:** the audit flags creative fatigue → the creative module extracts
the brand's design system from its website → generates on-brand ad **copy +
images** via OpenAI → pushes them as **PAUSED drafts** to the platform, gated by
guardrails and the existing Telegram approval queue.

---

## 1. Decisions locked (with the owner who set them)

| Axis | Decision |
|---|---|
| What it generates | Ad **copy + static images** |
| Where output goes | **Push as PAUSED drafts** to the platform (never auto-published) |
| Trigger | **Audit-suggested** — a creative-fatigue signal in the twice-weekly audit |
| Packaging / license | **Free, shipped in the skill.** Built as an isolated module so it can be carved into a paid tier later without untangling it from the audit |
| Image approach | **Scan `company.url`, extract the live design system**, generate creatives conditioned on it |
| Models | **OpenAI stack** — GPT-5-class for copy, `gpt-image-1` for images |
| Platforms | Meta (FB + IG, one API, multi-placement), LinkedIn, Google |

### Divergences from the original `development.md` note (intentional)
- Doc said *"rather than entangling with the audit cycle"* → we are **coupling**
  it to the audit (audit-suggested trigger). Accepted product decision.
- Doc framed it as a separate `adloops-creative` skill → it now lands **in this
  repo**, free, as a decouple-able module. `development.md` is being updated to
  match.

---

## 2. Architecture

```
[Audit — free, existing]                  [Creative module — free, new, isolated]
 audit.py
   ├─ campaign metrics (today)
   └─ creative-level metrics (NEW)  ──fatigue signal──┐
 report → Telegram                                    │
                                                      ▼
                                         creative/proposer.py
                                          ├─ brand_profile.py  (scan company.url → designSystem)
                                          ├─ copy.py           (OpenAI GPT-5-class; brandVoice-aware)
                                          ├─ image.py          (OpenAI gpt-image-1; design-system-conditioned)
                                          └─ guardrails (NEW creative rules)
                                                      │
                                                      ▼
                                         creative/executors/  (PAUSED draft push)
                                          ├─ meta.py     (AdImage → AdCreative → PAUSED Ad)
                                          ├─ linkedin.py (Assets upload → creative; extends fork MCP)
                                          └─ google.py   (RSA / PMax asset group; extends adloop MCP)
                                                      │
                                          approval queue (REUSE Phase 2/3) → Telegram
```

**Isolation for later monetization:** everything new lives under
`skill/scripts/creative/`. The only touchpoint into the free audit is (a) the
fatigue signal it reads and (b) the shared `brand.json` + approval queue. To
make it paid later: gate `creative/` behind an entitlement check and/or move the
dir to a private package — no audit refactor required.

---

## 3. Build order (sub-phases — de-risk the maximal scope)

| Sub-phase | Deliverable | Risk retired |
|---|---|---|
| **4a — brand profile** | `brand_profile.py`: fetch `company.url`, extract palette / type / logo / imagery style → write a `designSystem` block into `brand.json` (or a sibling `brand_design.json`) | Brand-consistency source of truth, before any generation |
| **4b — fatigue signal** | Creative-level metrics in `mcp_clients.py` (CTR decline, frequency rise, CPA drift per ad) + a "creative refresh suggested" line in the audit report | Validates the trigger cheaply; no generation yet |
| **4c — copy** | `copy.py` via OpenAI; respects `brandVoice.doSay/dontSay`, per-persona (`icp.personas`), per-platform char limits. Output **to Telegram for approval only** | Copy quality, zero write risk |
| **4d — images** | `image.py` via `gpt-image-1`, conditioned on the `designSystem`; emits the needed aspect ratios (1:1, 4:5, 9:16). Output to Telegram | Hardest quality problem, isolated from writes |
| **4e — draft push: Meta** | `creative/executors/meta.py`: AdImage → AdCreative → **PAUSED** Ad, guardrailed + approval | One platform end-to-end |
| **4f — draft push: LinkedIn + Google** | Extend the linkedin-ads fork (Assets + creative endpoints, likely new MDP scope) and the adloop MCP (RSA / PMax asset tools) | Most integration risk, last |

Shipping **4a→4c** alone already delivers value (on-brand copy suggestions in
Telegram) without any new platform-write surface.

---

## 4. Data model additions

### `brand.json` → new `designSystem` block (written by 4a)
```jsonc
"designSystem": {
  "source": "https://example.com",        // company.url it was scraped from
  "scrapedAt": "2026-06-03T00:00:00Z",
  "palette": { "primary": "#0F172A", "accent": "#22D3EE", "neutrals": ["#..."] },
  "typography": { "headline": "Readex Pro", "body": "Inter" },
  "logo": { "url": "...", "localPath": "assets/brand/logo.svg" },
  "imageryStyle": "flat, high-contrast, product-forward"  // vision-model summary
}
```
(Existing top-level `colors` stays for backward compat; `designSystem` supersedes
it for creative gen.)

### Creative proposal object (4c/4d → guardrails → executor)
```jsonc
{
  "campaign_id": "...", "platform": "meta",
  "reason": "creative-fatigue: CTR -41% over 21d",
  "copy": { "headline": "...", "primary": "...", "description": "...", "cta": "LEARN_MORE" },
  "images": [ { "aspect": "1:1", "path": "..." }, { "aspect": "9:16", "path": "..." } ],
  "persona": "Head of Growth at B2B SaaS"
}
```

---

## 5. Models / providers (OpenAI stack)

- **Copy:** GPT-5-class chat model. New `OPENAI_API_KEY`. Keep the existing
  `recommender.py` chain (OpenRouter→Anthropic) untouched — creative copy is its
  own module so the audit's recommender isn't disturbed.
- **Images:** `gpt-image-1`. Per-image cost is a real COGS line — log
  spend per run; this is the natural metering point if/when creative becomes paid.
- **Brand extraction (4a):** HTML/CSS scrape for color/type tokens + a vision
  model pass on the rendered page / OG image for `imageryStyle`. (Could also use
  a brand-data API like Brandfetch — open question §7.)

---

## 6. New creative guardrails

Reuse the guardrail *pattern*; add types:
- **Never AUTO.** Creative is higher-risk than a budget tweak → every creative
  proposal is **APPROVAL-only** (no auto-apply), always PAUSED on create.
- Max new creatives per run (cap, like `maxProposalsPerRun`).
- Brand-voice compliance: reject copy containing `brandVoice.dontSay` terms.
- Platform-policy lint: char limits, image text-density, aspect-ratio presence.

---

## 7. Open questions / risks

1. **Brand extraction fidelity** — scrape-and-infer vs a brand-data API
   (Brandfetch). Scrape is free + self-contained; API is more reliable. Decide
   in 4a.
2. **LinkedIn write surface** — the vendored fork only exposes `update_campaign`.
   Creatives need Assets upload + creative endpoints, and possibly a **new OAuth
   scope → another MDP review** (same gate that's blocking Phase 3 go-live).
3. **Google creative type** — RSA (text-only) vs Performance Max / Demand Gen
   asset groups (images). PMax is where generated images fit but is a bigger
   integration.
4. **Meta draft granularity** — create an unattached `AdCreative` for review, or
   a full PAUSED `Ad` under an existing ad set? Latter is more "ready to ship"
   but needs an ad-set target.
5. **Image cost ceiling** — set a per-run image budget guardrail so a fatigue
   storm can't run up `gpt-image-1` spend.

---

## 8. Decouple-to-paid path (when customers want it)

Because `creative/` is isolated: gate its entry behind an entitlement flag (env
or managed key), keep the **fatigue signal** in the free audit as the upsell
surface ("creative refresh suggested — enable creative gen"), and meter on
`gpt-image-1` spend already logged in §5. No audit rewrite required.
