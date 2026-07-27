"""Mutation proposer for Phase 2.

Pure function `propose(report, brand) -> list[Mutation]`. Reads the latest
audit report + brand config and proposes campaign-level mutations. Every
proposal is just a `Mutation` dataclass — it does NOT execute. The caller
runs each proposal through `guardrails.check_mutation()` and dispatches
the AUTO ones to the platform executors.

Phase 2 starter rules:

1. **Pause zombies** — pause any campaign that spent at least
   `brand.thresholds.pause_zombie_min_spend` over the 7d window with zero
   conversions. (Pause is always AUTO via the guardrails, so these
   execute immediately.)

2. **Decrease on CPA spike** — if a campaign's CPA grew at least
   `brand.thresholds.decrease_cpa_spike_pct` week-over-week, propose a
   budget decrease at the brand's `±N%` cap.

3. **Increase on CPA drop** — if a campaign's CPA dropped at least
   `brand.thresholds.increase_cpa_drop_pct` (a non-positive percent)
   AND conversions rose week-over-week, propose a budget increase at the
   cap. The cross-platform ceiling rule in `guardrails.py` will reject
   any increase that breaches `neverIncreaseBudgetAbove`.

Caps: the proposer never emits more than
`brand.thresholds.max_proposals_per_run` mutations to keep runaway
changes physically impossible. Pause proposals are prioritized over
budget changes (kill the zombie before scaling neighbours), and a single
campaign never gets more than one proposal per run.

Thresholds are configured per-brand via `guardrails.proposer` in
brand.json; the `DEFAULT_*` constants below remain importable as the
historical/example values.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .audit import AuditReport, CampaignDelta, PlatformResult
from .brand_loader import (
    Brand,
    DEFAULT_DECREASE_CPA_SPIKE_PCT,
    DEFAULT_INCREASE_CPA_DROP_PCT,
    DEFAULT_MAX_PROPOSALS_PER_RUN,
    DEFAULT_PAUSE_ZOMBIE_MIN_SPEND,
    ProposerThresholds,
)
from .guardrails import Mutation
from .mcp_clients import CampaignPerf


# Re-exports for callers that previously imported these directly. Live
# tuning happens via brand.json; these are the defaults used when the
# `guardrails.proposer` block is absent.
PAUSE_ZOMBIE_MIN_SPEND = DEFAULT_PAUSE_ZOMBIE_MIN_SPEND
DECREASE_CPA_SPIKE_PCT = DEFAULT_DECREASE_CPA_SPIKE_PCT
INCREASE_CPA_DROP_PCT = DEFAULT_INCREASE_CPA_DROP_PCT
MAX_PROPOSALS_PER_RUN = DEFAULT_MAX_PROPOSALS_PER_RUN


# ---- propose ---------------------------------------------------------------

def propose(report: AuditReport, brand: Brand) -> list[Mutation]:
    """Build a deduped, capped list of Mutation proposals for this audit run.

    A single campaign gets at most one proposal per run — if it qualifies
    for both a pause and a budget change, pause wins (more conservative).
    """
    cap_pct = brand.max_daily_budget_change_pct
    th = brand.thresholds
    seen: set[tuple[str, str]] = set()
    out: list[Mutation] = []

    perf_index = _index_perf(report.platforms)
    delta_index = _index_deltas(report.deltas)

    def _accept(m: Mutation) -> bool:
        """Dedup + append one proposal. Returns False once the per-run cap is
        hit so the caller stops. A create_campaign has no id yet, so the dedup
        key falls back to the campaign name."""
        key = (m.platform, m.campaign_id or m.campaign_name)
        if key in seen:
            return True
        seen.add(key)
        out.append(m)
        return len(out) < th.max_proposals_per_run

    # Order matters: pause first (highest priority), then decreases, then
    # increases. A campaign that qualifies for multiple rules only gets the
    # first match.
    for proposer in (
        _propose_pause_zombies,
        _propose_decrease_on_cpa_spike,
        _propose_increase_on_cpa_drop,
    ):
        for m in proposer(report, perf_index, delta_index, cap_pct, th):
            if not _accept(m):
                return out
    # Desired-state reconciliation runs last: protecting existing spend
    # (pauses, cuts) outranks spinning up new PAUSED campaigns and nudging
    # declared budgets when the per-run cap is tight.
    for m in _propose_desired_campaigns(report, brand):
        if not _accept(m):
            return out
    return out


# ---- individual rules ------------------------------------------------------

def _propose_pause_zombies(
    report: AuditReport,
    perf: dict[tuple[str, str], CampaignPerf],
    deltas: dict[tuple[str, str], CampaignDelta],
    cap_pct: float,
    th: ProposerThresholds,
) -> Iterable[Mutation]:
    for pr in report.platforms:
        if not pr.fetched:
            continue
        for c in pr.campaigns:
            if c.status != "ENABLED":
                continue
            if c.spend < th.pause_zombie_min_spend:
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
    th: ProposerThresholds,
) -> Iterable[Mutation]:
    for d in report.deltas:
        if d.cpa_prev is None or d.cpa_now is None or d.cpa_prev <= 0:
            continue
        growth_pct = ((d.cpa_now - d.cpa_prev) / d.cpa_prev) * 100.0
        if growth_pct < th.decrease_cpa_spike_pct:
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
    th: ProposerThresholds,
) -> Iterable[Mutation]:
    for d in report.deltas:
        if (
            d.cpa_prev is None or d.cpa_now is None or d.cpa_prev <= 0
            or d.conversions_prev is None
        ):
            continue
        drop_pct = ((d.cpa_now - d.cpa_prev) / d.cpa_prev) * 100.0
        if drop_pct > th.increase_cpa_drop_pct:
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


# ---- desired-state reconciliation ------------------------------------------

def _propose_desired_campaigns(report: AuditReport, brand: Brand) -> Iterable[Mutation]:
    """Reconcile `brand.desired_campaigns` against the account.

    Two deterministic behaviours per declared campaign:
      - **Missing** → propose create_campaign (forced PAUSED), carrying the
        optional ad-set spec so the executor scaffolds a populated launch.
      - **Present, budget drifted** → if the spec declares a dailyBudget and the
        live daily budget differs, propose a budget_change that steps toward the
        target *within the per-run cap*, so it stays AUTO and converges over runs
        rather than making one large jump that would queue for approval.

    Safety invariants:
      - Creates are always status=PAUSED; guardrails enforce it again downstream.
      - A platform whose inventory is unknown (fetch failed → names is None) is
        skipped entirely, so a transient read error never spawns a duplicate.
      - Drift only fires when exactly one live campaign matches the declared name
        (unambiguous) and its budget is visible — a declared campaign with no
        delivery this window is left alone rather than guessed at.
      - Name match is case-insensitive on the trimmed name.
    """
    desired = brand.desired_campaigns
    if not desired:
        return
    cap_pct = brand.max_daily_budget_change_pct
    names_by_platform: dict[str, set[str]] = {}
    perf_by_name: dict[tuple[str, str], list[CampaignPerf]] = {}
    for pr in report.platforms:
        if pr.existing_campaign_names is not None:
            names_by_platform[pr.platform] = {
                n.strip().casefold() for n in pr.existing_campaign_names
            }
        for c in pr.campaigns:
            perf_by_name.setdefault(
                (c.platform, c.campaign_name.strip().casefold()), []
            ).append(c)

    for spec in desired:
        existing = names_by_platform.get(spec.platform)
        if existing is None:
            continue  # inventory unavailable → never risk a duplicate or bad drift
        key = spec.name.strip().casefold()
        if key not in existing:
            yield _make_create_campaign(spec)
            continue
        # Already present → maybe reconcile budget drift toward the declared target.
        if spec.daily_budget is None:
            continue
        matches = perf_by_name.get((spec.platform, key), [])
        if len(matches) != 1:
            continue  # no delivery data, or ambiguous same-name campaigns
        c = matches[0]
        if c.daily_budget is None or c.daily_budget <= 0:
            continue
        new_budget = _capped_step_toward(c.daily_budget, spec.daily_budget, cap_pct)
        if new_budget is None or round(new_budget, 2) == round(c.daily_budget, 2):
            continue
        direction = "up" if new_budget > c.daily_budget else "down"
        yield Mutation(
            platform=spec.platform,
            campaign_id=c.campaign_id,
            campaign_name=c.campaign_name,
            kind="budget_change",
            before={"daily_budget": c.daily_budget},
            after={"daily_budget": new_budget},
            reason=(
                f"Declared daily budget ${spec.daily_budget:.0f} in brand.json; "
                f"live is ${c.daily_budget:.0f}. Stepping {direction} toward the "
                f"target within the {cap_pct:.0f}% cap."
            ),
        )


def _make_create_campaign(spec) -> Mutation:
    after: dict[str, Any] = {
        "name": spec.name,
        "objective": spec.objective,
        "status": "PAUSED",
        "special_ad_categories": list(spec.special_ad_categories),
    }
    if spec.daily_budget is not None:
        after["daily_budget"] = spec.daily_budget
    if spec.ad_set is not None:
        after["ad_set"] = spec.ad_set
    return Mutation(
        platform=spec.platform,
        campaign_id="",  # Meta assigns the id when the campaign is created
        campaign_name=spec.name,
        kind="create_campaign",
        before={},
        after=after,
        reason=(
            f"Declared in brand.json but not found in the {spec.platform} "
            "account; creating it PAUSED for you to populate and launch."
        ),
    )


def _capped_step_toward(current: float, target: float, cap_pct: float) -> float | None:
    """Step `current` toward `target` by at most `cap_pct` percent, without
    overshooting. Returns None when current is non-positive (can't scale).
    Keeps drift corrections inside the AUTO cap so they converge over runs."""
    if current <= 0:
        return None
    step = current * (cap_pct / 100.0)
    if target > current:
        return round(min(target, current + step), 2)
    return round(max(target, current - step), 2)


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
