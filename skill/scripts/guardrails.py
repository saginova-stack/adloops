"""Hard guardrails for AdLoops mutations.

This module wraps every mutation tool call. Every check is enforced in code so
the LLM cannot talk its way around it. Phase 1 only writes audit log entries
for read operations (none of these checks fire), but the module is fully
implemented and tested so Phase 2 can wire mutations in without changing the
contract.

Spec rules (from build prompt):
1. Max ±N% daily budget change per run, per campaign (default N=20). Larger
   diffs queue for approval rather than auto-execute.
2. New campaigns must launch PAUSED regardless of approval setting. Whether
   a new campaign is *allowed at all* depends on
   `guardrails.newCampaignRequiresApproval`.
3. If `neverIncreaseBudgetAbove` is set, the sum of projected daily spend
   across every active campaign after the proposed mutations must not
   exceed it.
4. Every mutation (proposed, applied, or rejected) appended to the audit log.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from . import paths


class Decision(str, Enum):
    AUTO = "auto"            # within guardrails, execute
    APPROVAL = "approval"    # exceeds soft limit, queue for human approval
    REJECTED = "rejected"    # violates a hard rule, never execute


@dataclass
class Mutation:
    """A proposed change to a single campaign on a single platform."""
    platform: str               # "google" | "meta" | "linkedin"
    campaign_id: str
    campaign_name: str
    kind: str                   # "budget_change" | "pause" | "enable" | "create_campaign" | ...
    before: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)
    reason: str = ""


@dataclass
class GuardrailResult:
    decision: Decision
    rule: str                   # short rule id, e.g. "budget_pct_cap"
    explanation: str
    mutation: Mutation


@dataclass
class GuardrailConfig:
    max_daily_budget_change_pct: float
    new_campaign_requires_approval: bool
    never_increase_above: float | None


def _budget_delta_pct(before: float, after: float) -> float:
    if before <= 0:
        # 0 → anything counts as a +100% increase for purposes of the cap.
        # We guard against div-by-zero by returning a value > any real cap.
        return float("inf") if after > 0 else 0.0
    return ((after - before) / before) * 100.0


def check_mutation(
    m: Mutation,
    cfg: GuardrailConfig,
    *,
    projected_active_daily_spend: float | None = None,
) -> GuardrailResult:
    """Evaluate a single proposed mutation against all guardrails.

    `projected_active_daily_spend` is the sum of every campaign's projected
    daily budget *if this mutation were applied*. The caller is responsible
    for computing this — guardrails just compares it to the ceiling.
    """
    if m.kind == "budget_change":
        before = float(m.before.get("daily_budget", 0))
        after = float(m.after.get("daily_budget", 0))
        delta_pct = _budget_delta_pct(before, after)

        if abs(delta_pct) > cfg.max_daily_budget_change_pct:
            return GuardrailResult(
                decision=Decision.APPROVAL,
                rule="budget_pct_cap",
                explanation=(
                    f"Budget change {delta_pct:+.1f}% exceeds the "
                    f"±{cfg.max_daily_budget_change_pct}% per-run cap "
                    f"({before} → {after})."
                ),
                mutation=m,
            )

        if (
            cfg.never_increase_above is not None
            and projected_active_daily_spend is not None
            and after > before
            and projected_active_daily_spend > cfg.never_increase_above
        ):
            return GuardrailResult(
                decision=Decision.REJECTED,
                rule="cross_platform_ceiling",
                explanation=(
                    f"Projected combined daily spend "
                    f"{projected_active_daily_spend:.2f} would exceed the ceiling "
                    f"{cfg.never_increase_above:.2f}."
                ),
                mutation=m,
            )

        return GuardrailResult(
            decision=Decision.AUTO,
            rule="budget_pct_cap",
            explanation=f"Within ±{cfg.max_daily_budget_change_pct}% cap.",
            mutation=m,
        )

    if m.kind == "pause":
        return GuardrailResult(
            decision=Decision.AUTO,
            rule="pause_always_allowed",
            explanation="Pausing a poor performer is always allowed.",
            mutation=m,
        )

    if m.kind == "enable":
        # Enabling a previously-paused campaign can blow up spend. Treat as
        # approval-required regardless of size.
        return GuardrailResult(
            decision=Decision.APPROVAL,
            rule="enable_requires_approval",
            explanation="Re-enabling a paused campaign always requires human approval.",
            mutation=m,
        )

    if m.kind == "create_campaign":
        # Two checks: (a) is creation allowed at all, (b) is the new campaign
        # marked PAUSED. Even when allowed, it MUST be paused.
        proposed_status = str(m.after.get("status", "")).upper()
        if proposed_status != "PAUSED":
            return GuardrailResult(
                decision=Decision.REJECTED,
                rule="new_campaign_must_be_paused",
                explanation=(
                    "New campaigns must launch PAUSED. Got "
                    f"status={proposed_status!r}."
                ),
                mutation=m,
            )
        if cfg.new_campaign_requires_approval:
            return GuardrailResult(
                decision=Decision.APPROVAL,
                rule="new_campaign_approval",
                explanation="brand.json sets newCampaignRequiresApproval=true.",
                mutation=m,
            )
        return GuardrailResult(
            decision=Decision.AUTO,
            rule="new_campaign_paused_ok",
            explanation="Allowed; will launch PAUSED.",
            mutation=m,
        )

    # Unknown mutation kind → reject by default. Better to over-restrict
    # than to silently let something through.
    return GuardrailResult(
        decision=Decision.REJECTED,
        rule="unknown_mutation_kind",
        explanation=f"Unrecognized mutation kind {m.kind!r}; refusing to apply.",
        mutation=m,
    )


def append_audit(entry: dict[str, Any], path: Path | None = None) -> None:
    """Append-only JSONL audit log. One line per call."""
    log = path or paths.audit_log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=True, default=str) + "\n")


def log_decision(result: GuardrailResult, *, applied: bool) -> None:
    append_audit({
        "ts": time.time(),
        "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "platform": result.mutation.platform,
        "campaign_id": result.mutation.campaign_id,
        "campaign_name": result.mutation.campaign_name,
        "kind": result.mutation.kind,
        "decision": result.decision.value,
        "rule": result.rule,
        "explanation": result.explanation,
        "applied": applied,
        "before": result.mutation.before,
        "after": result.mutation.after,
        "reason": result.mutation.reason,
        "run_id": os.environ.get("ADLOOPS_RUN_ID", ""),
    })


def config_from_brand(brand) -> GuardrailConfig:
    """Build a GuardrailConfig from a Brand instance (see brand_loader)."""
    return GuardrailConfig(
        max_daily_budget_change_pct=brand.max_daily_budget_change_pct,
        new_campaign_requires_approval=brand.new_campaign_requires_approval,
        never_increase_above=brand.never_increase_above,
    )


def serialize_result(r: GuardrailResult) -> dict[str, Any]:
    """For inclusion in audit reports."""
    return {
        "decision": r.decision.value,
        "rule": r.rule,
        "explanation": r.explanation,
        "mutation": asdict(r.mutation),
    }
