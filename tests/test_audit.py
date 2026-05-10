from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from scripts.audit import (
    PlatformResult,
    compute_deltas,
    latest_archive,
    top_movers,
    write_snapshot,
)
from scripts.mcp_clients import CampaignPerf
import scripts.audit as audit_mod


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


def test_compute_deltas_with_prior_snapshot():
    now = [PlatformResult("google", True, True, None, [
        cp(campaign_id="A", spend=100, conversions=4),
        cp(campaign_id="B", spend=50, conversions=0),
    ])]
    prev = {
        "platforms": [
            {"campaigns": [
                cp(campaign_id="A", spend=80, conversions=2).to_dict(),
                cp(campaign_id="B", spend=40, conversions=1).to_dict(),
            ]},
        ],
    }
    deltas = compute_deltas(now, prev)
    by_id = {d.campaign_id: d for d in deltas}
    assert by_id["A"].spend_change_pct == 25.0
    assert by_id["A"].conversions_prev == 2
    assert by_id["B"].cpa_prev == 40.0
    assert by_id["B"].cpa_now is None  # zero conversions


def test_compute_deltas_without_prior():
    now = [PlatformResult("google", True, True, None, [cp(campaign_id="A", spend=100)])]
    deltas = compute_deltas(now, prev=None)
    assert deltas[0].spend_prev is None
    assert deltas[0].spend_change_pct is None


def test_top_movers_best_picks_conversion_gainers():
    deltas = compute_deltas(
        [PlatformResult("google", True, True, None, [
            cp(campaign_id="GAIN", campaign_name="gain", spend=100, conversions=10),
            cp(campaign_id="FLAT", campaign_name="flat", spend=100, conversions=2),
            cp(campaign_id="LOSS", campaign_name="loss", spend=50, conversions=0),
        ])],
        prev={"platforms": [{"campaigns": [
            cp(campaign_id="GAIN", spend=80, conversions=2).to_dict(),
            cp(campaign_id="FLAT", spend=80, conversions=2).to_dict(),
            cp(campaign_id="LOSS", spend=40, conversions=4).to_dict(),
        ]}]},
    )
    best, worst = top_movers(deltas, n=3)
    assert best[0].campaign_id == "GAIN"
    # FLAT didn't gain → not in best
    assert "FLAT" not in [d.campaign_id for d in best]
    # LOSS spends with zero conversions → most-wasteful sort puts it first
    assert worst[0].campaign_id == "LOSS"


def test_write_then_read_snapshot(tmp_path: Path):
    pr = PlatformResult("google", True, True, None, [cp(campaign_id="A", spend=12.34)])
    report = audit_mod.AuditReport(
        run_id="20260101T000000Z",
        generated_at="2026-01-01T00:00:00Z",
        window_start="2025-12-25",
        window_end="2025-12-31",
        platforms=[pr],
        deltas=[],
        top_movers_best=[],
        top_movers_worst=[],
        actions_taken=[],
        pending_approvals=[],
        recommendations=[],
    )
    p = write_snapshot(report, archive_dir=tmp_path)
    assert p.exists()
    raw = json.loads(p.read_text())
    assert raw["run_id"] == "20260101T000000Z"
    assert raw["platforms"][0]["totals"]["spend"] == 12.34


def test_latest_archive_picks_newest(tmp_path: Path):
    (tmp_path / "20260101T000000Z.json").write_text(json.dumps({"run_id": "old"}))
    (tmp_path / "20260201T000000Z.json").write_text(json.dumps({"run_id": "new"}))
    raw = latest_archive(tmp_path)
    assert raw["run_id"] == "new"


def test_latest_archive_missing_dir(tmp_path: Path):
    assert latest_archive(tmp_path / "nope") is None


def test_recommendations_flag_zero_conversion_spend():
    deltas = compute_deltas(
        [PlatformResult("google", True, True, None, [
            cp(campaign_id="WASTE", campaign_name="zombie", spend=200, conversions=0),
            cp(campaign_id="OK", campaign_name="ok", spend=100, conversions=5),
        ])],
        prev=None,
    )
    recs = audit_mod._rule_based_recommendations(deltas)
    assert any("zombie" in r and "Pause" in r for r in recs)


def test_fetch_all_skips_disabled_platforms(monkeypatch):
    # Even if creds exist for disabled platforms, we shouldn't try to call them.
    called = {"google": False, "meta": False, "linkedin": False}

    class FakeOK:
        def __init__(self, plat):
            self.plat = plat
        def fetch_perf_7d(self):
            called[self.plat] = True
            return []

    monkeypatch.setattr(audit_mod, "GoogleAdsClient", lambda: FakeOK("google"))
    monkeypatch.setattr(audit_mod, "MetaAdsClient", lambda: FakeOK("meta"))
    monkeypatch.setattr(audit_mod, "LinkedInAdsClient", lambda: FakeOK("linkedin"))
    results = audit_mod.fetch_all({"google"})
    by_plat = {r.platform: r for r in results}
    assert by_plat["google"].fetched is True
    assert by_plat["meta"].enabled is False
    assert by_plat["meta"].fetched is False
    assert by_plat["linkedin"].fetched is False
    assert called == {"google": True, "meta": False, "linkedin": False}


def test_fetch_all_records_missing_creds(monkeypatch):
    from scripts.mcp_clients import MissingCredentialsError

    def raise_missing():
        raise MissingCredentialsError("Meta Ads: missing env ['META_ACCESS_TOKEN']")

    monkeypatch.setattr(audit_mod, "GoogleAdsClient", raise_missing)
    monkeypatch.setattr(audit_mod, "MetaAdsClient", raise_missing)
    monkeypatch.setattr(audit_mod, "LinkedInAdsClient", raise_missing)
    results = audit_mod.fetch_all({"google", "meta", "linkedin"})
    for r in results:
        assert r.enabled is True
        assert r.fetched is False
        assert "missing env" in (r.error or "")
