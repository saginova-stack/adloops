from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.brand_loader import BrandConfigError, ensure_scaffold, load_or_raise


def _write(p: Path, raw: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(raw))


VALID = {
    "company": {"name": "Acme"},
    "icp": {"personas": ["Head of Growth"]},
    "valueProps": ["Saves time"],
    "brandVoice": {"tone": "direct"},
    "guardrails": {
        "maxDailyBudgetChangePct": 20,
        "platforms": {"google": True, "meta": True, "linkedin": False},
    },
}


def test_load_valid_brand(tmp_path: Path):
    p = tmp_path / "brand.json"
    _write(p, VALID)
    b = load_or_raise(p)
    assert b.company_name == "Acme"
    assert b.personas == ["Head of Growth"]
    assert b.max_daily_budget_change_pct == 20
    assert b.enabled_platforms == {"google", "meta"}


def test_missing_file_raises(tmp_path: Path):
    with pytest.raises(BrandConfigError, match="not found"):
        load_or_raise(tmp_path / "nope.json")


def test_invalid_json_raises(tmp_path: Path):
    p = tmp_path / "brand.json"
    p.write_text("{not json")
    with pytest.raises(BrandConfigError, match="not valid JSON"):
        load_or_raise(p)


def test_empty_personas_raises(tmp_path: Path):
    raw = {**VALID, "icp": {"personas": []}}
    p = tmp_path / "brand.json"
    _write(p, raw)
    with pytest.raises(BrandConfigError, match="personas is empty"):
        load_or_raise(p)


def test_missing_company_name(tmp_path: Path):
    raw = {**VALID, "company": {}}
    p = tmp_path / "brand.json"
    _write(p, raw)
    with pytest.raises(BrandConfigError, match="company.name"):
        load_or_raise(p)


def test_bad_pct_value(tmp_path: Path):
    raw = {**VALID, "guardrails": {**VALID["guardrails"], "maxDailyBudgetChangePct": 500}}
    p = tmp_path / "brand.json"
    _write(p, raw)
    with pytest.raises(BrandConfigError, match="maxDailyBudgetChangePct"):
        load_or_raise(p)


def test_missing_platform_keys(tmp_path: Path):
    raw = {**VALID, "guardrails": {**VALID["guardrails"], "platforms": {"google": True}}}
    p = tmp_path / "brand.json"
    _write(p, raw)
    with pytest.raises(BrandConfigError, match="platforms"):
        load_or_raise(p)


def test_ensure_scaffold_creates_layout(tmp_path: Path):
    bjson = ensure_scaffold(tmp_path)
    assert bjson.exists()
    assert (tmp_path / "assets" / "logo").is_dir()
    assert (tmp_path / "assets" / "linkedin").is_dir()
    assert (tmp_path / "assets" / "screenshots").is_dir()
    assert (tmp_path / "campaigns" / ".archive").is_dir()
    # scaffold uses the example file → loadable
    raw = json.loads(bjson.read_text())
    assert "company" in raw and raw["company"]["name"]


def test_ensure_scaffold_idempotent(tmp_path: Path):
    bjson = ensure_scaffold(tmp_path)
    bjson.write_text(json.dumps({**VALID, "company": {"name": "Edited"}}))
    bjson2 = ensure_scaffold(tmp_path)
    raw = json.loads(bjson2.read_text())
    assert raw["company"]["name"] == "Edited"


# ---- proposer thresholds ---------------------------------------------------

def test_thresholds_default_when_proposer_block_absent(tmp_path: Path):
    """Brands written before the proposer block existed keep working —
    the loader fills in the historical defaults."""
    p = tmp_path / "brand.json"
    _write(p, VALID)
    th = load_or_raise(p).thresholds
    assert th.pause_zombie_min_spend == 50.0
    assert th.decrease_cpa_spike_pct == 50.0
    assert th.increase_cpa_drop_pct == -25.0
    assert th.max_proposals_per_run == 10


def test_thresholds_override_per_field(tmp_path: Path):
    raw = {**VALID, "guardrails": {**VALID["guardrails"], "proposer": {
        "pauseZombieMinSpend": 200,
        "maxProposalsPerRun": 3,
    }}}
    p = tmp_path / "brand.json"
    _write(p, raw)
    th = load_or_raise(p).thresholds
    # Overridden values:
    assert th.pause_zombie_min_spend == 200.0
    assert th.max_proposals_per_run == 3
    # Unset values fall through to defaults — proves it's per-field, not all-or-nothing:
    assert th.decrease_cpa_spike_pct == 50.0
    assert th.increase_cpa_drop_pct == -25.0


def test_negative_pause_threshold_rejected(tmp_path: Path):
    raw = {**VALID, "guardrails": {**VALID["guardrails"], "proposer": {
        "pauseZombieMinSpend": -10,
    }}}
    p = tmp_path / "brand.json"
    _write(p, raw)
    with pytest.raises(BrandConfigError, match="pauseZombieMinSpend"):
        load_or_raise(p)


def test_positive_cpa_drop_threshold_rejected(tmp_path: Path):
    """A positive value here would mean 'fire on a CPA increase', which is
    the opposite of what this rule does — must be non-positive."""
    raw = {**VALID, "guardrails": {**VALID["guardrails"], "proposer": {
        "increaseCpaDropPct": 25,
    }}}
    p = tmp_path / "brand.json"
    _write(p, raw)
    with pytest.raises(BrandConfigError, match="non-positive"):
        load_or_raise(p)


def test_zero_max_proposals_rejected(tmp_path: Path):
    """maxProposalsPerRun=0 would silently disable the entire proposer —
    if the operator wants to disable mutations they should use --no-mutate
    or flip platform booleans. Reject zero loudly."""
    raw = {**VALID, "guardrails": {**VALID["guardrails"], "proposer": {
        "maxProposalsPerRun": 0,
    }}}
    p = tmp_path / "brand.json"
    _write(p, raw)
    with pytest.raises(BrandConfigError, match="positive integer"):
        load_or_raise(p)


def test_non_integer_max_proposals_rejected(tmp_path: Path):
    raw = {**VALID, "guardrails": {**VALID["guardrails"], "proposer": {
        "maxProposalsPerRun": 5.5,
    }}}
    p = tmp_path / "brand.json"
    _write(p, raw)
    with pytest.raises(BrandConfigError, match="integer"):
        load_or_raise(p)


# ---- guardrails.proposer.desiredCampaigns (create_campaign reconciler) ----

def _with_desired(*specs):
    raw = json.loads(json.dumps(VALID))
    raw["guardrails"]["proposer"] = {"desiredCampaigns": list(specs)}
    return raw


def test_desired_campaigns_parsed_into_specs(tmp_path: Path):
    p = tmp_path / "brand.json"
    _write(p, _with_desired(
        {"platform": "meta", "name": "Q3 Leads", "objective": "OUTCOME_LEADS",
         "dailyBudget": 25, "specialAdCategories": ["EMPLOYMENT"]},
    ))
    specs = load_or_raise(p).desired_campaigns
    assert len(specs) == 1
    s = specs[0]
    assert (s.platform, s.name, s.objective) == ("meta", "Q3 Leads", "OUTCOME_LEADS")
    assert s.daily_budget == 25.0
    assert s.special_ad_categories == ["EMPLOYMENT"]


def test_desired_campaigns_absent_yields_empty(tmp_path: Path):
    p = tmp_path / "brand.json"
    _write(p, VALID)
    assert load_or_raise(p).desired_campaigns == []


def test_desired_campaign_non_meta_platform_rejected(tmp_path: Path):
    """Only Meta's executor can create campaigns today; declaring a Google one
    must fail loud at load time, not silently error every run at dispatch."""
    p = tmp_path / "brand.json"
    _write(p, _with_desired({"platform": "google", "name": "x", "objective": "SALES"}))
    with pytest.raises(BrandConfigError, match="must be \"meta\""):
        load_or_raise(p)


def test_desired_campaign_missing_objective_rejected(tmp_path: Path):
    p = tmp_path / "brand.json"
    _write(p, _with_desired({"platform": "meta", "name": "x"}))
    with pytest.raises(BrandConfigError, match="objective is required"):
        load_or_raise(p)


def test_desired_campaign_missing_name_rejected(tmp_path: Path):
    p = tmp_path / "brand.json"
    _write(p, _with_desired({"platform": "meta", "objective": "OUTCOME_LEADS"}))
    with pytest.raises(BrandConfigError, match="name is required"):
        load_or_raise(p)


def test_desired_campaign_non_positive_budget_rejected(tmp_path: Path):
    p = tmp_path / "brand.json"
    _write(p, _with_desired(
        {"platform": "meta", "name": "x", "objective": "OUTCOME_LEADS", "dailyBudget": 0},
    ))
    with pytest.raises(BrandConfigError, match="dailyBudget must be a positive number"):
        load_or_raise(p)


def test_desired_campaign_bad_special_ad_categories_rejected(tmp_path: Path):
    p = tmp_path / "brand.json"
    _write(p, _with_desired(
        {"platform": "meta", "name": "x", "objective": "OUTCOME_LEADS",
         "specialAdCategories": "EMPLOYMENT"},
    ))
    with pytest.raises(BrandConfigError, match="specialAdCategories must be an array"):
        load_or_raise(p)
