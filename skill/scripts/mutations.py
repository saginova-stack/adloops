"""Mutation proposer for Phase 2.

Pure function `propose(report, brand) -> list[Mutation]`. Reads the latest
audit report + brand config and proposes campaign-level mutations. Every
proposal is just a `Mutation` dataclass — it does NOT execute. The caller
runs each proposal through `guardrails.check_mutation()` and dispatches
the AUTO ones to the platform executors.

Phase 2 starter rules:

1. **Pause zombies** — pause any campaign that spent ≥ `PAUSE_ZOMBIE_MIN_SPEND`
   over the 7d window with zero conversions. (Pause is always AUTO via the
   guardrails, so these execute immediately.)

2. **Decrease on CPA spike** — if a campaign's CPA grew ≥ `DECREASE_CPA_SPIKE_PCT`
   week-over-week, propose a budget decrease at the brand's `±N%` cap.

3. **Increase on CPA drop** — if a campaign's CPA dropped ≥ `INCREASE_CPA_DROP_PCT`
   AND conversions rose week-over-week, propose a budget increase at the
   cap. The cross-platform ceiling rule in `guardrails.py` will reject any
   increase that breaches `neverIncreaseBudgetAbove`.

Caps: the proposer never emits more than `MAX_PROPOSALS_PER_RUN` mutations
to keep runaway changes physically impossible. Pause proposals are prioritized
over budget changes (kill the zombie before scaling neighbours), and a single
campaign never gets more than one proposal per run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .audit import AuditReport, CampaignDelta, PlatformResult
from .brand_loader import Brand
from .guardrails import Mutation
from .mcp_clients import CampaignPerf


# ---- thresholds (tunable from data once Phase 2 has been running) ----------

# Pause any campaign spending at least this much over the 7-day window with
# zero conversions. Tuned to avoid pausing tiny test campaigns; bump if it's
# too aggressive in practice.
PAUSE_ZOMBIE_MIN_SPEND = 50.0

# Week-over-week CPA growth that triggers a budget *decrease* proposal.
DECREASE_CPA_SPIKE_PCT = 50.0

# Week-over-week CPA drop (negative number) that, combined with positive
# conversion delta, triggers a budget *increase* proposal.
INCREASE_CPA_DROP_PCT = -25.0

# Hard cap on proposals emitted per run. Even with ten zombie campaigns we'd
# rather make ten conservative tweaks across two weeks than ten today.
MAX_PROPOSALS_PER_RUN = 10


# ---- propose ---------------------------------------------------------------

def propose(report: AuditReport, brand: Brand) -> list[Mutation]:
    """Build a deduped, capped list of Mutation proposals for this audit run.

    A single campaign gets at most one proposal per run — if it qualifies
    for both a pause and a budget change, pause wins (more conservative).
    """
    cap_pct = brand.max_daily_budget_change_pct
    seen: set[tuple[str, str]] = set()
    out: list[Mutation] = []

    perf_index = _index_perf(report.platforms)
    delta_index = _index_deltas(report.deltas)

    # Order matters: pause first (highest priority), then decreases, then
    # increases. A campaign that qualifies for multiple rules only gets the
    # first match.
    for proposer in (
        _propose_pause_zombies,
        _propose_decrease_on_cpa_spike,
        _propose_increase_on_cpa_drop,
    ):
        for m in proposer(report, perf_index, delta_index, cap_pct):
            key = (m.platform, m.campaign_id)
            if key in seen:
                continue
            seen.add(key)
            out.append(m)
            if len(out) >= MAX_PROPOSALS_PER_RUN:
                return out
    return out


# ---- individual rules ------------------------------------------------------

def _propose_pause_zombies(
    report: AuditReport,
    perf: dict[tuple[str, str], CampaignPerf],
    deltas: dict[tuple[str, str], CampaignDelta],
    cap_pct: float,
) -> Iterable[Mutation]:
    for pr in report.platforms:
        if not pr.fetched:
            continue
        for c in pr.campaigns:
            if c.status != "ENABLED":
                continue
            if c.spend < PAUSE_ZOMBIE_MIN_SPEND:
                continue
            if c.conversions > 0:
                continue
            yield Mutation(
                platform=c.platform,
                campaign_id=c.campaign_id,
                campaign_name=c.campaign_name,
                kind="pause",
                before={"status": "ENABLED"},
                after={"status": "PAUSED"},
                reason=(
                    f"Spent ${c.spend:.0f} over the 7d window with 0 conversions; "
                    f"pausing to stop the bleed."
                ),
            )


def _propose_decrease_on_cpa_spike(
    report: AuditReport,
    perf: dict[tuple[str, str], CampaignPerf],
    deltas: dict[tuple[str, str], CampaignDelta],
    cap_pct: float,
) -> Iterable[Mutation]:
    for d in report.deltas:
        if d.cpa_prev is None or d.cpa_now is None or d.cpa_prev <= 0:
            continue
        growth_pct = ((d.cpa_now - d.cpa_prev) / d.cpa_prev) * 100.0
        if growth_pct < DECREASE_CPA_SPIKE_PCT:
            continue
        c = perf.get((d.platform, d.campaign_id))
        if not c or c.daily_budget is None or c.daily_budget <= 0:
            continue
        new_budget = round(c.daily_budget * (1 - cap_pct / 100.0), 2)
        yield Mutation(
            platform=d.platform,
            campaign_id=d.campaign_id,
            campaign_name=d.campaign_name,
            kind="budget_change",
            before={"daily_budget": c.daily_budget},
            after={"daily_budget": new_budget},
            reason=(
                f"CPA up {growth_pct:+.0f}% week-over-week "
                f"(${d.cpa_prev:.2f} → ${d.cpa_now:.2f}); decreasing budget "
                f"by the {cap_pct:.0f}% cap to slow spend until performance recovers."
            ),
        )


def _propose_increase_on_cpa_drop(
    report: AuditReport,
    perf: dict[tuple[str, str], CampaignPerf],
    deltas: dict[tuple[str, str], CampaignDelta],
    cap_pct: float,
) -> Iterable[Mutation]:
    for d in report.deltas:
        if (
            d.cpa_prev is None or d.cpa_now is None or d.cpa_prev <= 0
            or d.conversions_prev is None
        ):
            continue
        drop_pct = ((d.cpa_now - d.cpa_prev) / d.cpa_prev) * 100.0
        if drop_pct > INCREASE_CPA_DROP_PCT:
            continue
        if d.conversions_now <= d.conversions_prev:
            continue
        c = perf.get((d.platform, d.campaign_id))
        if not c or c.daily_budget is None or c.daily_budget <= 0:
            continue
        new_budget = round(c.daily_budget * (1 + cap_pct / 100.0), 2)
        yield Mutation(
            platform=d.platform,
            campaign_id=d.campaign_id,
            campaign_name=d.campaign_name,
            kind="budget_change",
            before={"daily_budget": c.daily_budget},
            after={"daily_budget": new_budget},
            reason=(
                f"CPA down {drop_pct:+.0f}% and conversions up "
                f"({d.conversions_prev:.0f} → {d.conversions_now:.0f}); "
                f"increasing budget by the {cap_pct:.0f}% cap to scale a winner."
            ),
        )


# ---- helpers ---------------------------------------------------------------

def _index_perf(
    platforms: list[PlatformResult],
) -> dict[tuple[str, str], CampaignPerf]:
    out: dict[tuple[str, str], CampaignPerf] = {}
    for pr in platforms:
        for c in pr.campaigns:
            out[(c.platform, c.campaign_id)] = c
    return out


def _index_deltas(
    deltas: list[CampaignDelta],
) -> dict[tuple[str, str], CampaignDelta]:
    return {(d.platform, d.campaign_id): d for d in deltas}


def projected_total_spend(
    report: AuditReport,
    pending: list[Mutation],
) -> float:
    """Sum of every campaign's projected daily budget after `pending` is applied.

    Used by guardrails.check_mutation() for the cross-platform ceiling rule.
    A `Mutation.after.daily_budget` overrides the campaign's current budget;
    paused campaigns are treated as $0 contribution to the ceiling.
    """
    overrides: dict[tuple[str, str], float] = {}
    pauses: set[tuple[str, str]] = set()
    enables: set[tuple[str, str]] = set()
    for m in pending:
        key = (m.platform, m.campaign_id)
        if m.kind == "budget_change":
            overrides[key] = float(m.after.get("daily_budget", 0))
        elif m.kind == "pause":
            pauses.add(key)
        elif m.kind == "enable":
            enables.add(key)

    total = 0.0
    for pr in report.platforms:
        for c in pr.campaigns:
            key = (c.platform, c.campaign_id)
            if key in pauses:
                continue
            if c.status != "ENABLED" and key not in enables:
                continue
            total += overrides.get(key, c.daily_budget or 0.0)
    return total
