from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.guardrails import (
    Decision,
    GuardrailConfig,
    Mutation,
    append_audit,
    check_mutation,
    log_decision,
    serialize_result,
)


def cfg(pct: float = 20, ceiling: float | None = None, requires_approval: bool = False):
    return GuardrailConfig(
        max_daily_budget_change_pct=pct,
        new_campaign_requires_approval=requires_approval,
        never_increase_above=ceiling,
    )


def budget_change(before: float, after: float, *, name: str = "c1") -> Mutation:
    return Mutation(
        platform="google",
        campaign_id="111",
        campaign_name=name,
        kind="budget_change",
        before={"daily_budget": before},
        after={"daily_budget": after},
        reason="test",
    )


# --- ±N% cap ---

def test_budget_within_cap_auto():
    r = check_mutation(budget_change(100, 119), cfg(20))
    assert r.decision == Decision.AUTO
    assert r.rule == "budget_pct_cap"


def test_budget_at_cap_auto():
    r = check_mutation(budget_change(100, 120), cfg(20))
    assert r.decision == Decision.AUTO


def test_budget_just_over_cap_requires_approval():
    r = check_mutation(budget_change(100, 121), cfg(20))
    assert r.decision == Decision.APPROVAL
    assert "20" in r.explanation


def test_budget_decrease_over_cap_requires_approval():
    r = check_mutation(budget_change(100, 50), cfg(20))
    assert r.decision == Decision.APPROVAL


def test_budget_zero_to_positive_requires_approval():
    # Going from $0 → anything positive looks like infinite % growth.
    r = check_mutation(budget_change(0, 10), cfg(20))
    assert r.decision == Decision.APPROVAL


def test_budget_zero_to_zero_auto():
    r = check_mutation(budget_change(0, 0), cfg(20))
    assert r.decision == Decision.AUTO


# --- ceiling ---

def test_ceiling_blocks_increase_when_total_over_ceiling():
    r = check_mutation(
        budget_change(100, 110),
        cfg(20, ceiling=500),
        projected_active_daily_spend=510,
    )
    assert r.decision == Decision.REJECTED
    assert r.rule == "cross_platform_ceiling"


def test_ceiling_allows_decrease_even_if_total_over_ceiling():
    # We rejected on increase only — a decrease should still be allowed
    # because it moves us *toward* the ceiling, not over it.
    r = check_mutation(
        budget_change(100, 90),
        cfg(20, ceiling=500),
        projected_active_daily_spend=510,
    )
    assert r.decision == Decision.AUTO


def test_ceiling_allows_increase_when_total_under_ceiling():
    r = check_mutation(
        budget_change(100, 110),
        cfg(20, ceiling=500),
        projected_active_daily_spend=400,
    )
    assert r.decision == Decision.AUTO


def test_ceiling_only_evaluated_when_over_pct_cap_passes():
    # Pct cap takes precedence: if a change fails the pct cap, we don't
    # need the ceiling to also reject — approval queue is enough.
    r = check_mutation(
        budget_change(100, 200),
        cfg(20, ceiling=500),
        projected_active_daily_spend=600,
    )
    assert r.decision == Decision.APPROVAL
    assert r.rule == "budget_pct_cap"


# --- pause / enable ---

def test_pause_always_auto():
    r = check_mutation(
        Mutation("meta", "m1", "campaign", "pause"),
        cfg(20),
    )
    assert r.decision == Decision.AUTO


def test_enable_always_requires_approval():
    r = check_mutation(
        Mutation("meta", "m1", "campaign", "enable"),
        cfg(20),
    )
    assert r.decision == Decision.APPROVAL


# --- new campaigns ---

def test_new_campaign_must_be_paused():
    r = check_mutation(
        Mutation(
            platform="google",
            campaign_id="new-1",
            campaign_name="Brand Search",
            kind="create_campaign",
            after={"status": "ENABLED", "daily_budget": 50},
        ),
        cfg(),
    )
    assert r.decision == Decision.REJECTED
    assert r.rule == "new_campaign_must_be_paused"


def test_new_campaign_paused_auto_when_allowed():
    r = check_mutation(
        Mutation(
            platform="google",
            campaign_id="new-1",
            campaign_name="Brand Search",
            kind="create_campaign",
            after={"status": "PAUSED", "daily_budget": 50},
        ),
        cfg(requires_approval=False),
    )
    assert r.decision == Decision.AUTO


def test_new_campaign_paused_approval_when_required():
    r = check_mutation(
        Mutation(
            platform="google",
            campaign_id="new-1",
            campaign_name="Brand Search",
            kind="create_campaign",
            after={"status": "PAUSED", "daily_budget": 50},
        ),
        cfg(requires_approval=True),
    )
    assert r.decision == Decision.APPROVAL


# --- unknown mutations ---

def test_unknown_mutation_rejected():
    r = check_mutation(
        Mutation("google", "1", "x", "send_money_to_attacker"),
        cfg(),
    )
    assert r.decision == Decision.REJECTED


# --- audit log ---

def test_log_decision_appends_jsonl(tmp_path: Path, monkeypatch):
    log = tmp_path / "audit.jsonl"
    monkeypatch.setattr("scripts.guardrails.paths.audit_log_path", lambda: log)

    r1 = check_mutation(budget_change(100, 110), cfg(20))
    r2 = check_mutation(
        Mutation("meta", "x", "y", "pause"), cfg(20)
    )
    log_decision(r1, applied=True)
    log_decision(r2, applied=False)

    lines = log.read_text().strip().splitlines()
    assert len(lines) == 2
    rows = [json.loads(l) for l in lines]
    assert rows[0]["decision"] == "auto"
    assert rows[0]["applied"] is True
    assert rows[1]["kind"] == "pause"
    assert rows[1]["applied"] is False
    # required keys
    for row in rows:
        for k in ("ts", "ts_iso", "platform", "campaign_id", "kind", "rule", "explanation"):
            assert k in row


def test_append_audit_creates_parent_dirs(tmp_path: Path):
    log = tmp_path / "nested" / "deeper" / "audit.jsonl"
    append_audit({"x": 1}, path=log)
    assert log.exists()
    assert json.loads(log.read_text().strip()) == {"x": 1}


def test_serialize_result_shape():
    r = check_mutation(budget_change(100, 110), cfg(20))
    out = serialize_result(r)
    assert set(out) == {"decision", "rule", "explanation", "mutation"}
    assert out["mutation"]["platform"] == "google"
