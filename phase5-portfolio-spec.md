# Phase 5 — cross-campaign / portfolio budget allocation (spec / plan)

**Status:** planned, not started. Idea from Mike Silberberg (ex-Google,
2026-06-03). This is the "fuller spec" the `development.md` Phase 5 entry points
at.

**One-line:** beyond per-campaign tweaks, treat an account as a portfolio —
pull budget from underperformers and feed *budget-constrained* winners as a
coordinated, spend-neutral set of moves, gated by guardrails and approval.

**The core shift:** Phase 2 emits *independent* per-campaign budget changes.
Phase 5 emits a *plan* — a set of paired `−X on A / +X on B` legs reasoned about
together. That changes the proposer's unit of work and the risk profile, so it
gets its own design rather than another rule bolted onto `mutations.propose`.

---

## 1. Decisions / scope (lock before building)

| Axis | Decision |
|---|---|
| Unit of work | A **reallocation plan** (set of paired legs), not independent mutations |
| Spend posture | **Spend-neutral within the account** — total daily budget unchanged (auto-respects the cross-platform ceiling) |
| Risk gate | **APPROVAL-only** at launch — never AUTO until there's observed trust |
| Platform scope | **Single platform first** (cross-campaign within Google, or within Meta); cross-platform reallocation is a later, separate question |
| Direction | Reallocate among **live performers**; pausing zombies stays Phase 2's job |

---

## 2. Where it sits in the pipeline

```
audit.run_audit → report (CampaignPerf per campaign, with spend/conv/revenue)
   │
   ├─ Phase 2: mutations.propose         → per-campaign pause / ±cap% budget
   │                                        (these campaigns are EXCLUDED from
   │                                         reallocation this run — see §7)
   └─ Phase 5: portfolio.propose_plan    → ReallocationPlan (paired legs)
                     │
              guardrails (per-leg ±cap% + plan-level checks) → APPROVAL
                     │
              executor: apply DECREASES first, then INCREASES (§6)
                     │
              audit log: the plan + each leg's result
```

New module `skill/scripts/portfolio.py` (sibling to `mutations.py`), reusing
`CampaignPerf`, `guardrails`, and the executors. `run.py` calls it after the
per-campaign proposer.

---

## 3. The hard part: a fair cross-campaign efficiency metric

Raw CPA isn't comparable across campaigns with different objectives/maturity.
From the data we already have on `CampaignPerf` (spend, conversions, revenue?):

- **If `revenue` is present → ROAS** (`revenue / spend`). Cleanest ranking.
- **Else → cost-per-conversion** (`spend / conversions`), compared only among
  campaigns sharing a comparable objective.
- **Target-relative if available:** CPA-vs-targetCPA ratio is fairer than
  absolute CPA, but we don't reliably have targets today — treat as optional.

Guardrails on the metric itself (avoid acting on noise):

- **Minimum data:** a campaign is only eligible (as donor *or* recipient) if it
  cleared `minConversions` and `minSpend` over the window.
- **Maturity:** exclude very new campaigns (still in learning).
- **Gap threshold:** only reallocate when the efficiency gap between the chosen
  donor and recipient exceeds `minEfficiencyGapPct` — small gaps aren't worth
  the churn.

---

## 4. Donor / recipient selection + transfer sizing

- **Donors** = worst-efficiency eligible campaigns. (Distinct from zombies,
  which Phase 2 pauses — reallocation reslices among campaigns worth keeping.)
- **Recipients** = best-efficiency eligible campaigns that are **budget-
  constrained** — feeding a winner only helps if it can absorb more spend.
  Proxy for "constrained": `spend ≈ daily_budget` (spending its full budget).
  Ideal signal is impression-share-lost-to-budget, but we may not have it; start
  with the spend≈budget proxy and note the limitation.
- **Transfer amount per pair**, bounded by the tightest of:
  - the per-run `±maxDailyBudgetChangePct` on **each** leg (donor down ≤cap%,
    recipient up ≤cap%),
  - `maxReallocationPctPerRun` of the account's total daily budget,
  - the recipient's `neverIncreaseBudgetAbove` ceiling (existing brand rule).
- **Spend-neutral:** Σ decreases = Σ increases. Keep it simple at launch — one
  donor → one recipient per plan, or N donors → N recipients matched by rank.

---

## 5. Build order (sub-phases — de-risk)

| Sub-phase | Deliverable | Risk retired |
|---|---|---|
| **5a — report only** | Compute the efficiency ranking + a *suggested* reallocation, surface it in the Telegram report. **No action.** | Validates the metric + donor/recipient picks on real data before moving a cent |
| **5b — APPROVAL plans** | `portfolio.propose_plan` emits paired legs; queued as APPROVAL, applied via `--approve`, decreases-first | Real moves, human-gated |
| **5c — bounded AUTO** | Allow AUTO only for small, high-confidence plans within tight caps, once 5b has a track record | Trust earned incrementally |
| **5d — cross-platform** | Reallocate across platforms (raises attribution comparability) | Hardest; last |

5a alone is valuable — it tells the operator "you're leaving money on the table
between campaigns" without any write risk, and it's how we tune the metric.

---

## 6. Execution & failure handling

Ad platforms have no multi-campaign transaction, so a plan is a sequence of
independent `budget_change` calls. Ordering makes partial failure safe:

1. **Apply all DECREASES first** → frees budget, lowers account spend.
2. **Then apply INCREASES.** If an increase leg fails, the freed budget simply
   goes unused that cycle — the account **under-spends, never over-spends** the
   ceiling. Fail loud: log the unapplied leg; never silently leave it.

Each leg is logged to the audit log individually, plus a plan-level record
(plan id, metric, net delta) so a reallocation is auditable and reversible.

---

## 7. Guardrail integration (the part most likely to bite)

- **Per-leg:** each leg is a `budget_change` Mutation → existing
  `guardrails.check_mutation` (±cap%, ceiling) applies unchanged.
- **Plan-level (new):** `maxReallocationPctPerRun`, `minEfficiencyGapPct`,
  `minConversions`, `minSpend`, and a hard "**plans are APPROVAL-only**" until
  flipped. Add these under `guardrails.proposer` in `brand.json`.
- **No double-touch:** a campaign already getting a Phase 2 per-campaign budget
  change this run must be **excluded** from reallocation — otherwise it could be
  cut by the CPA-spike rule *and* drained as a donor, blowing past the per-run
  cap. Cleanest: reallocation runs on the set of campaigns *not* touched by
  `mutations.propose` this cycle.
- **Spend ceiling:** spend-neutral plans don't raise the account total, so the
  cross-platform ceiling is respected by construction — still assert it.

---

## 8. Data model

```jsonc
ReallocationPlan {
  "plan_id": "...", "platform": "google",
  "metric": "roas" | "cpa",
  "net_delta": 0.0,                 // spend-neutral within the account
  "legs": [
    { "campaign_id": "...", "role": "donor",
      "before_budget": 120.0, "after_budget": 100.0, "delta": -20.0,
      "metric_value": 0.8, "reason": "lowest ROAS, eligible (conv≥min)" },
    { "campaign_id": "...", "role": "recipient",
      "before_budget": 80.0,  "after_budget": 100.0, "delta": +20.0,
      "metric_value": 3.1, "reason": "top ROAS, budget-constrained (spend≈budget)" }
  ]
}
```
Each leg materializes as a `Mutation(kind="budget_change")` for guardrails +
executor reuse; the plan wraps them for approval + audit.

---

## 9. Open questions

1. **Metric without revenue.** Many lead-gen accounts have no `revenue` → ROAS
   unavailable; cost-per-conversion is the fallback but only fair within one
   objective. Do we need per-campaign objective tagging in `brand.json`?
2. **Detecting a constrained winner** without impression-share data — is the
   `spend ≈ daily_budget` proxy good enough, or do we pull IS-lost-to-budget
   from the platform (more API surface)?
3. **One-pair vs N-pair plans** at launch — start with the single best pair for
   explainability, or rank-match N donors to N recipients?
4. **Rule-based vs optimizer** — launch is rule-based (shift within caps from
   worst to best); revisit a real optimizer only with `--no-mutate`/5a data.

---

## 10. Why this is sequenced last

Moving real budget *between* campaigns is a bigger trust ask than pausing a
zombie or nudging one budget. Build it **after** the per-campaign proposer has
earned AUTO trust through the observation window, keep it **APPROVAL-only well
past** when single tweaks go AUTO, and lean on 5a (report-only) to prove the
metric before any money moves.
