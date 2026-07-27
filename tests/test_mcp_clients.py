from __future__ import annotations

import pytest

import scripts.mcp_clients as mcp_clients
from scripts.mcp_clients import (
    CampaignPerf,
    GoogleAnalyticsClient,
    MetaAdsClient,
    MissingCredentialsError,
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
        clicks=200,
        conversions=4.0,
        revenue=None,
    )
    base.update(kw)
    return CampaignPerf(**base)


# ---------- CampaignPerf cross-reference properties ----------

def test_consent_gap_pct_none_without_ga4():
    c = cp()
    assert c.consent_gap_pct is None
    assert c.attribution_gap_pct is None
    assert c.real_cpa is None


def test_consent_gap_pct_with_smaller_ga4_sessions():
    # Ads says 200 clicks, GA4 only saw 120 sessions → 40% gap.
    c = cp(clicks=200, ga4_sessions=120)
    assert c.consent_gap_pct == pytest.approx(40.0)


def test_consent_gap_pct_zero_when_ga4_higher():
    # GA4 reporting more sessions than Ads clicks happens with attribution
    # leakage / clicks before window. Floor at zero rather than report negative.
    c = cp(clicks=100, ga4_sessions=150)
    assert c.consent_gap_pct == 0.0


def test_consent_gap_pct_none_when_no_clicks():
    c = cp(clicks=0, ga4_sessions=10)
    assert c.consent_gap_pct is None


def test_attribution_gap_pct_basic():
    # Ads says 10 conv, GA4 says 14 → 40% gap.
    c = cp(conversions=10.0, ga4_conversions=14.0)
    assert c.attribution_gap_pct == pytest.approx(40.0)


def test_attribution_gap_pct_zero_when_aligned():
    c = cp(conversions=10.0, ga4_conversions=10.0)
    assert c.attribution_gap_pct == 0.0


def test_attribution_gap_pct_none_when_no_ads_conversions():
    c = cp(conversions=0, ga4_conversions=5)
    assert c.attribution_gap_pct is None


def test_real_cpa_divides_against_ga4_conversions():
    c = cp(spend=200.0, conversions=4.0, ga4_conversions=8.0)
    # Reported CPA = 200/4 = 50, real CPA via GA4 = 200/8 = 25.
    assert c.cpa == 50.0
    assert c.real_cpa == 25.0


def test_real_cpa_none_when_ga4_conversions_zero():
    c = cp(ga4_conversions=0.0)
    assert c.real_cpa is None


# ---------- GoogleAnalyticsClient env validation ----------

def test_ga4_client_missing_property_id(monkeypatch):
    monkeypatch.delenv("GA4_PROPERTY_ID", raising=False)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/sa.json")
    with pytest.raises(MissingCredentialsError, match="GA4_PROPERTY_ID"):
        GoogleAnalyticsClient()


def test_ga4_client_missing_credentials(monkeypatch):
    monkeypatch.setenv("GA4_PROPERTY_ID", "12345")
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.delenv("GOOGLE_ADS_REFRESH_TOKEN", raising=False)
    with pytest.raises(MissingCredentialsError, match="service-account"):
        GoogleAnalyticsClient()


def test_ga4_client_accepts_raw_property_id(monkeypatch):
    monkeypatch.setenv("GA4_PROPERTY_ID", "12345")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/sa.json")
    c = GoogleAnalyticsClient()
    assert c.property_id == "12345"


def test_ga4_client_accepts_prefixed_property_id(monkeypatch):
    monkeypatch.setenv("GA4_PROPERTY_ID", "properties/12345")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/sa.json")
    c = GoogleAnalyticsClient()
    assert c.property_id == "12345"


def test_ga4_client_falls_back_to_oauth_refresh_token(monkeypatch):
    monkeypatch.setenv("GA4_PROPERTY_ID", "12345")
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setenv("GOOGLE_ADS_REFRESH_TOKEN", "fake-refresh")
    # Shouldn't raise — OAuth fallback is acceptable.
    GoogleAnalyticsClient()


# ---------- Meta ad-set / lifetime budget resolution ----------

def _meta_env(monkeypatch):
    monkeypatch.setenv("META_ACCESS_TOKEN", "TOK")
    monkeypatch.setenv("META_AD_ACCOUNT_ID", "act_1")


def _fake_paged_factory(campaigns, adsets, insights):
    def fake_paged(url, params, *, items_key, headers=None, max_pages=50):
        if "/insights" in url:
            return insights
        if "/adsets" in url:
            return adsets
        if "/campaigns" in url:
            return campaigns
        return []
    return fake_paged


def test_meta_non_cbo_campaign_sums_adset_daily_budgets(monkeypatch):
    """The whole point of ad-set budget support: a campaign with no
    campaign-level budget must report the sum of its ad-set daily budgets,
    otherwise the proposer never proposes a budget change for it."""
    _meta_env(monkeypatch)
    monkeypatch.setattr(mcp_clients, "_fetch_paged", _fake_paged_factory(
        campaigns=[{"id": "C1", "name": "Prospecting", "status": "ACTIVE"}],  # no budget
        adsets=[
            {"campaign_id": "C1", "daily_budget": "3000"},
            {"campaign_id": "C1", "daily_budget": "2000"},
        ],
        insights=[{"campaign_id": "C1", "campaign_name": "Prospecting",
                   "spend": "40", "impressions": "1000", "clicks": "50",
                   "actions": [{"action_type": "lead", "value": "4"}]}],
    ))
    rows = MetaAdsClient().fetch_perf_7d()
    assert len(rows) == 1
    # 3000 + 2000 cents = $50.00
    assert rows[0].daily_budget == 50.0
    assert rows[0].extra["meta_budget"] == {"level": "adset", "type": "daily"}


def test_meta_cbo_campaign_uses_campaign_daily_budget(monkeypatch):
    """CBO campaign keeps its budget on the campaign — no ad-set summing."""
    _meta_env(monkeypatch)
    monkeypatch.setattr(mcp_clients, "_fetch_paged", _fake_paged_factory(
        campaigns=[{"id": "C1", "name": "CBO", "status": "ACTIVE", "daily_budget": "8000"}],
        adsets=[],
        insights=[{"campaign_id": "C1", "campaign_name": "CBO", "spend": "10",
                   "impressions": "100", "clicks": "5", "actions": []}],
    ))
    rows = MetaAdsClient().fetch_perf_7d()
    assert rows[0].daily_budget == 80.0
    assert rows[0].extra["meta_budget"] == {"level": "campaign", "type": "daily"}


def test_meta_lifetime_only_campaign_has_no_daily_but_records_source(monkeypatch):
    """A campaign whose only budget is a lifetime budget has no daily figure —
    daily_budget stays None (proposer skips it) but the lifetime amount is
    still surfaced so the write path and report can use it."""
    _meta_env(monkeypatch)
    monkeypatch.setattr(mcp_clients, "_fetch_paged", _fake_paged_factory(
        campaigns=[{"id": "C1", "name": "Burst", "status": "ACTIVE",
                    "lifetime_budget": "500000"}],
        adsets=[],
        insights=[{"campaign_id": "C1", "campaign_name": "Burst", "spend": "10",
                   "impressions": "100", "clicks": "5", "actions": []}],
    ))
    rows = MetaAdsClient().fetch_perf_7d()
    assert rows[0].daily_budget is None
    assert rows[0].extra["meta_budget"] == {
        "level": "campaign", "type": "lifetime", "lifetime_budget": 5000.0,
    }


def test_meta_client_missing_credentials(monkeypatch):
    monkeypatch.delenv("META_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("META_AD_ACCOUNT_ID", raising=False)
    with pytest.raises(MissingCredentialsError, match="Meta Ads"):
        MetaAdsClient()


def test_meta_fetch_campaign_names_returns_full_inventory(monkeypatch):
    """The reconciler needs every campaign name — including zero-delivery PAUSED
    ones that never appear in the spend-filtered perf rows."""
    _meta_env(monkeypatch)

    def fake_paged(url, params, *, items_key, headers=None, max_pages=50):
        assert "/campaigns" in url and params.get("fields") == "name"
        return [{"name": "Live One"}, {"name": "Paused Zero-Spend"}, {"id": "no-name"}]

    monkeypatch.setattr(mcp_clients, "_fetch_paged", fake_paged)
    assert MetaAdsClient().fetch_campaign_names() == ["Live One", "Paused Zero-Spend"]
