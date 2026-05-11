from __future__ import annotations

import pytest

from scripts.mcp_clients import (
    CampaignPerf,
    GoogleAnalyticsClient,
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
