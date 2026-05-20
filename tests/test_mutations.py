from __future__ import annotations

import pytest

from scripts.audit import AuditReport, CampaignDelta, PlatformResult
from scripts.brand_loader import Brand
from scripts.mcp_clients import CampaignPerf
from scripts.mutations import (
    MAX_PROPOSALS_PER_RUN,
    projected_total_spend,
    propose,
)


def cp(**kw):
    base = dict(
        platform="google",
        campaign_id="1",
        campaign_name="c1",
        status="ENABLED",
        daily_budget=50.0,
        spend=100.0,
        impressions=1000,
        clicks=20,
        conversions=2.0,
        revenue=None,
    )
    base.update(kw)
    return CampaignPerf(**base)


def cd(**kw):
    base = dict(
        platform="google",
        campaign_id="1",
        campaign_name="c1",
        spend_now=100.0,
        spend_prev=80.0,
        spend_change_pct=25.0,
        cpa_now=50.0,
        cpa_prev=40.0,
        conversions_now=2.0,
        conversions_prev=2.0,
    )
    base.update(kw)
    return CampaignDelta(**base)


def make_report(*, campaigns, deltas=None):
    pr = PlatformResult("google", True, True, None, campaigns)
    return AuditReport(
        run_id="TEST", generated_at="2026-05-11T00:00:00Z",
        window_start="2026-05-04", window_end="2026-05-10",
        platforms=[pr], deltas=deltas or [],
        top_movers_best=[], top_movers_worst=[],
        actions_taken=[], pending_approvals=[], recommendations=[],
    )


def brand_with(pct=20.0, ceiling=None, requires_approval=False, proposer=None):
    """proposer=None means no proposer block — the loader applies its
    defaults. Pass a dict to override one or more thresholds."""
    raw = {
        "company": {"name": "Test Co"},
        "icp": {"personas": [{"name": "x", "role": "y", "painPoints": ["z"]}]},
        "valueProps": ["a"],
        "brandVoice": {"tone": "b"},
        "guardrails": {
            "maxDailyBudgetChangePct": pct,
            "newCampaignRequiresApproval": requires_approval,
            "platforms": {"google": True, "meta": True, "linkedin": True},
            "neverIncreaseBudgetAbove": ceiling,
        },
    }
    if proposer is not None:
        raw["guardrails"]["proposer"] = proposer
    return Brand(raw=raw)


# ---- rule: pause zombies ---------------------------------------------------

def test_pause_zombie_with_high_spend_zero_conversions():
    r = make_report(campaigns=[cp(campaign_id="Z", spend=200.0, conversions=0)])
    ms = propose(r, brand_with())
    assert len(ms) == 1
    assert ms[0].kind == "pause"
    assert ms[0].campaign_id == "Z"
    assert ms[0].before == {"status": "ENABLED"}
    assert ms[0].after == {"status": "PAUSED"}
    assert "0 conversions" in ms[0].reason


def test_pause_skipped_below_min_spend():
    # spend of $40 is below the $50 floor — too small to bother
    r = make_report(campaigns=[cp(campaign_id="SMALL", spend=40.0, conversions=0)])
    ms = propose(r, brand_with())
    assert ms == []


def test_pause_skipped_when_already_paused():
    r = make_report(campaigns=[cp(campaign_id="P", status="PAUSED", spend=200.0, conversions=0)])
    ms = propose(r, brand_with())
    assert ms == []


def test_pause_skipped_when_has_conversions():
    r = make_report(campaigns=[cp(spend=500.0, conversions=0.5)])
    ms = propose(r, brand_with())
    assert ms == []


# ---- rule: decrease on CPA spike ------------------------------------------

def test_decrease_on_cpa_spike():
    # CPA grew 100% week-over-week → propose -20% budget
    r = make_report(
        campaigns=[cp(campaign_id="X", daily_budget=100.0, spend=300.0, conversions=3.0)],
        deltas=[cd(campaign_id="X", cpa_now=100.0, cpa_prev=50.0, conversions_now=3.0, conversions_prev=4.0)],
    )
    ms = propose(r, brand_with(pct=20))
    assert len(ms) == 1
    assert ms[0].kind == "budget_change"
    assert ms[0].before == {"daily_budget": 100.0}
    assert ms[0].after == {"daily_budget": 80.0}  # -20%
    assert "CPA up" in ms[0].reason


def test_decrease_skipped_when_cpa_growth_below_threshold():
    # CPA growth of 30% is below the 50% trigger
    r = make_report(
        campaigns=[cp(campaign_id="X", daily_budget=100.0)],
        deltas=[cd(campaign_id="X", cpa_now=130.0, cpa_prev=100.0, conversions_now=2.0, conversions_prev=2.0)],
    )
    ms = propose(r, brand_with())
    assert ms == []


def test_decrease_skipped_without_prior_cpa():
    r = make_report(
        campaigns=[cp(campaign_id="X", daily_budget=100.0)],
        deltas=[cd(campaign_id="X", cpa_prev=None, cpa_now=100.0, conversions_prev=None)],
    )
    ms = propose(r, brand_with())
    assert ms == []


def test_decrease_skipped_without_daily_budget():
    # Some Meta campaigns use shared/campaign-budget — daily_budget is None
    r = make_report(
        campaigns=[cp(campaign_id="X", daily_budget=None)],
        deltas=[cd(campaign_id="X", cpa_now=100.0, cpa_prev=50.0)],
    )
    ms = propose(r, brand_with())
    assert ms == []


# ---- rule: increase on CPA drop -------------------------------------------

def test_increase_on_cpa_drop_with_conversion_lift():
    # CPA dropped 50%, conversions up → propose +20% budget
    r = make_report(
        campaigns=[cp(campaign_id="WIN", daily_budget=100.0, conversions=10.0)],
        deltas=[cd(campaign_id="WIN", cpa_now=20.0, cpa_prev=40.0, conversions_now=10.0, conversions_prev=4.0)],
    )
    ms = propose(r, brand_with(pct=20))
    assert len(ms) == 1
    assert ms[0].kind == "budget_change"
    assert ms[0].after == {"daily_budget": 120.0}  # +20%


def test_increase_skipped_when_conversions_flat():
    # CPA dropped but conversions didn't actually go up
    r = make_report(
        campaigns=[cp(campaign_id="X", daily_budget=100.0, conversions=4.0)],
        deltas=[cd(campaign_id="X", cpa_now=20.0, cpa_prev=40.0, conversions_now=4.0, conversions_prev=4.0)],
    )
    ms = propose(r, brand_with())
    assert ms == []


def test_increase_skipped_when_drop_below_threshold():
    # CPA dropped only 20% — not enough to fire the rule (needs >=25% drop)
    r = make_report(
        campaigns=[cp(campaign_id="X", daily_budget=100.0)],
        deltas=[cd(campaign_id="X", cpa_now=80.0, cpa_prev=100.0, conversions_now=6, conversions_prev=4)],
    )
    ms = propose(r, brand_with())
    assert ms == []


# ---- dedup + cap behaviour -----------------------------------------------

def test_single_campaign_gets_at_most_one_mutation():
    # A campaign with $500 spend, 0 conversions, AND a CPA spike → pause wins,
    # not budget change.
    r = make_report(
        campaigns=[cp(campaign_id="D", daily_budget=100.0, spend=500.0, conversions=0)],
        deltas=[cd(campaign_id="D", cpa_now=200.0, cpa_prev=100.0)],
    )
    ms = propose(r, brand_with())
    assert len(ms) == 1
    assert ms[0].kind == "pause"


def test_max_proposals_per_run_caps_output():
    n = MAX_PROPOSALS_PER_RUN + 5
    campaigns = [cp(campaign_id=f"Z{i}", spend=200.0, conversions=0) for i in range(n)]
    r = make_report(campaigns=campaigns)
    ms = propose(r, brand_with())
    assert len(ms) == MAX_PROPOSALS_PER_RUN


def test_skips_platforms_that_did_not_fetch():
    r = make_report(campaigns=[cp(spend=200.0, conversions=0)])
    r.platforms[0].fetched = False  # simulate fetch failure
    ms = propose(r, brand_with())
    assert ms == []


# ---- brand-configured thresholds ------------------------------------------

def test_zombie_threshold_overridden_from_brand_config():
    """A more cautious brand wants $200 spend before pausing; $80 zombies
    that used to fire under the default $50 floor should now stay alive."""
    r = make_report(campaigns=[
        cp(campaign_id="SMALL_ZOMBIE", spend=80.0, conversions=0),
        cp(campaign_id="BIG_ZOMBIE", spend=300.0, conversions=0),
    ])
    ms = propose(r, brand_with(proposer={"pauseZombieMinSpend": 200}))
    assert len(ms) == 1
    assert ms[0].campaign_id == "BIG_ZOMBIE"


def test_cpa_spike_threshold_overridden_from_brand_config():
    """A jumpy account wants the budget cut to fire at 30% CPA growth, not
    the default 50%. Without the override the rule wouldn't trip."""
    r = make_report(
        campaigns=[cp(campaign_id="X", daily_budget=100.0)],
        deltas=[cd(campaign_id="X", cpa_now=130.0, cpa_prev=100.0, conversions_now=2.0, conversions_prev=2.0)],
    )
    # Default behaviour: nothing fires.
    assert propose(r, brand_with()) == []
    # With a 30% trigger, the same delta now produces a budget cut.
    ms = propose(r, brand_with(proposer={"decreaseCpaSpikePct": 30}))
    assert len(ms) == 1
    assert ms[0].kind == "budget_change"
    assert ms[0].after == {"daily_budget": 80.0}  # still capped at 20%


def test_cpa_drop_threshold_overridden_from_brand_config():
    """Pickier brand wants the scale-up to require a 40% drop, not 25%.
    A 30% drop that used to scale should now stay flat."""
    r = make_report(
        campaigns=[cp(campaign_id="W", daily_budget=100.0, conversions=8.0)],
        deltas=[cd(campaign_id="W", cpa_now=70.0, cpa_prev=100.0, conversions_now=8.0, conversions_prev=4.0)],
    )
    # 30% drop fires under default -25 trigger.
    assert len(propose(r, brand_with())) == 1
    # Same delta no longer fires when the operator demands -40.
    assert propose(r, brand_with(proposer={"increaseCpaDropPct": -40})) == []


def test_max_proposals_per_run_overridden_from_brand_config():
    """A conservative brand wants at most 3 changes per run regardless of
    how many zombies showed up. Cap respected without touching code."""
    campaigns = [cp(campaign_id=f"Z{i}", spend=200.0, conversions=0) for i in range(8)]
    r = make_report(campaigns=campaigns)
    ms = propose(r, brand_with(proposer={"maxProposalsPerRun": 3}))
    assert len(ms) == 3


def test_partial_proposer_block_uses_defaults_for_unset_fields():
    """Operators should be able to override one threshold without listing
    the other three. The loader merges per-field, not all-or-nothing."""
    # Override only the pause threshold; the CPA-spike default (50%) still
    # applies — so a 30% spike does NOT fire.
    r = make_report(
        campaigns=[cp(campaign_id="X", daily_budget=100.0)],
        deltas=[cd(campaign_id="X", cpa_now=130.0, cpa_prev=100.0)],
    )
    ms = propose(r, brand_with(proposer={"pauseZombieMinSpend": 1000}))
    assert ms == []


# ---- projected_total_spend -----------------------------------------------

def test_projected_total_sums_active_with_overrides():
    r = make_report(campaigns=[
        cp(campaign_id="A", daily_budget=100.0),
        cp(campaign_id="B", daily_budget=50.0),
        cp(campaign_id="C", daily_budget=200.0),
    ])
    from scripts.guardrails import Mutation

    pending = [
        Mutation(platform="google", campaign_id="A", campaign_name="A",
                 kind="budget_change",
                 before={"daily_budget": 100.0}, after={"daily_budget": 120.0}),
        Mutation(platform="google", campaign_id="B", campaign_name="B",
                 kind="pause", before={"status": "ENABLED"}, after={"status": "PAUSED"}),
    ]
    total = projected_total_spend(r, pending)
    # A: 120 (override), B: 0 (paused), C: 200 (unchanged) → 320
    assert total == 320.0


def test_projected_total_treats_paused_campaigns_as_zero():
    r = make_report(campaigns=[
        cp(campaign_id="A", status="ENABLED", daily_budget=100.0),
        cp(campaign_id="B", status="PAUSED", daily_budget=50.0),
    ])
    total = projected_total_spend(r, [])
    assert total == 100.0
