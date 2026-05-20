from __future__ import annotations

import pytest

from scripts.audit import AuditReport, CampaignDelta, PlatformResult
from scripts.mcp_clients import CampaignPerf
from scripts.telegram_report import (
    MAX_MSG,
    TelegramConfigError,
    _chunk,
    render_report,
    send_messages,
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


def make_report(**overrides):
    base = dict(
        run_id="20260510T090000Z",
        generated_at="2026-05-10T09:00:00Z",
        window_start="2026-05-03",
        window_end="2026-05-09",
        platforms=[],
        deltas=[],
        top_movers_best=[],
        top_movers_worst=[],
        actions_taken=[],
        pending_approvals=[],
        recommendations=[],
    )
    base.update(overrides)
    return AuditReport(**base)


def test_render_includes_header_and_window():
    r = make_report(platforms=[
        PlatformResult("google", True, True, None, [cp()]),
    ])
    out = "\n\n".join(render_report(r))
    assert "AdLoops" in out
    assert "2026-05-03" in out and "2026-05-09" in out
    assert "google" in out


def test_render_disabled_platform_marked():
    r = make_report(platforms=[
        PlatformResult("google", True, True, None, [cp()]),
        PlatformResult("linkedin", False, False, None, []),
    ])
    out = "\n\n".join(render_report(r))
    assert "linkedin: disabled" in out


def test_render_error_platform_surfaced():
    r = make_report(platforms=[
        PlatformResult("meta", True, False, "Meta Ads: missing env ['META_ACCESS_TOKEN']", []),
    ])
    out = "\n\n".join(render_report(r))
    assert "ERROR" in out and "META_ACCESS_TOKEN" in out


def test_render_top_movers_section():
    r = make_report(platforms=[PlatformResult("google", True, True, None, [cp()])],
                    top_movers_best=[CampaignDelta("google", "1", "winner", 100, 50, 100.0, 25.0, 50.0, 4, 2)],
                    top_movers_worst=[CampaignDelta("google", "2", "zombie", 80, 40, 100.0, None, None, 0, 5)])
    out = "\n\n".join(render_report(r))
    assert "Top movers:" in out
    assert "winner" in out and "zombie" in out
    assert "+100.0%" in out


def test_render_recommendations_section():
    r = make_report(platforms=[PlatformResult("google", True, True, None, [])],
                    recommendations=["Pause campaign X", "Scale Y"])
    out = "\n\n".join(render_report(r))
    assert "Recommendations:" in out
    assert "Pause campaign X" in out


def test_render_no_actions_message():
    r = make_report(platforms=[PlatformResult("google", True, True, None, [])])
    out = "\n\n".join(render_report(r))
    assert "Actions taken: none" in out


def test_chunk_keeps_sections_intact_when_possible():
    sections = ["a" * 100, "b" * 100, "c" * 100]
    chunks = _chunk(sections, max_chars=250)
    assert len(chunks) == 2
    assert chunks[0].count("a") == 100
    # second section fits with first (100+2+100 = 202)
    # third overflows → second chunk


def test_chunk_splits_oversized_section():
    huge = "\n".join([f"line{i:04}" for i in range(500)])  # ~5000 chars
    chunks = _chunk([huge], max_chars=1000)
    assert len(chunks) > 1
    # rejoining preserves all lines
    assert "\n".join(chunks).count("line0001") == 1


def test_send_requires_token(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("ADLOOPS_TELEGRAM_CHAT_ID", "123")
    with pytest.raises(TelegramConfigError, match="TELEGRAM_BOT_TOKEN"):
        send_messages(["hello"])


def test_send_requires_chat_id(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake")
    monkeypatch.delenv("ADLOOPS_TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    with pytest.raises(TelegramConfigError, match="chat id"):
        send_messages(["hello"])


def test_send_dry_run_returns_previews(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake")
    monkeypatch.setenv("ADLOOPS_TELEGRAM_CHAT_ID", "999")
    out = send_messages(["msg one", "msg two"], dry_run=True)
    assert len(out) == 2
    assert out[0]["preview"].startswith("msg one")
    assert all(o["dry_run"] for o in out)


def test_render_chunks_under_telegram_max():
    r = make_report(
        platforms=[PlatformResult("google", True, True, None, [cp(campaign_name=f"campaign-{i}") for i in range(50)])],
        recommendations=[f"recommendation #{i} that's a bit long" for i in range(10)],
    )
    chunks = render_report(r)
    assert all(len(c) <= MAX_MSG for c in chunks)


# ---------- Tracking & consent section ----------

def test_tracking_section_flags_consent_gap():
    pr = PlatformResult("google", True, True, None, [
        cp(campaign_name="EU brand", clicks=200, ga4_sessions=100, conversions=4.0, ga4_conversions=4.0),
    ])
    pr.ga4_status = "ok"
    r = make_report(platforms=[pr])
    out = "\n\n".join(render_report(r))
    assert "Tracking & consent:" in out
    assert "EU brand" in out
    assert "50% consent gap" in out


def test_tracking_section_flags_attribution_gap():
    pr = PlatformResult("google", True, True, None, [
        cp(campaign_name="Search US", clicks=200, ga4_sessions=190, conversions=10.0, ga4_conversions=14.0),
    ])
    pr.ga4_status = "ok"
    r = make_report(platforms=[pr])
    out = "\n\n".join(render_report(r))
    assert "Tracking & consent:" in out
    assert "attribution gap" in out
    assert "Search US" in out


def test_tracking_section_flags_cpa_drift():
    # Reported CPA: 200/4 = 50; real CPA via GA4: 200/8 = 25 → 50% lower.
    pr = PlatformResult("google", True, True, None, [
        cp(campaign_name="Brand", spend=200.0, clicks=190, ga4_sessions=190, conversions=4.0, ga4_conversions=8.0),
    ])
    pr.ga4_status = "ok"
    r = make_report(platforms=[pr])
    out = "\n\n".join(render_report(r))
    assert "real CPA" in out


def test_tracking_section_omitted_when_within_thresholds():
    pr = PlatformResult("google", True, True, None, [
        cp(clicks=200, ga4_sessions=190, conversions=4.0, ga4_conversions=4.0),
    ])
    pr.ga4_status = "ok"
    r = make_report(platforms=[pr])
    out = "\n\n".join(render_report(r))
    assert "Tracking & consent:" not in out


def test_tracking_section_shows_ga4_skip_reason():
    pr = PlatformResult("google", True, True, None, [cp()])
    pr.ga4_status = "skipped: GA4: missing env ['GA4_PROPERTY_ID']"
    r = make_report(platforms=[pr])
    out = "\n\n".join(render_report(r))
    assert "Tracking & consent: GA4 enrichment skipped" in out
    assert "GA4_PROPERTY_ID" in out


def test_tracking_section_only_flags_google_campaigns():
    # Meta campaign with the same "consent gap" shape — should be ignored
    # because Meta doesn't have a GA4 cross-reference.
    meta_pr = PlatformResult("meta", True, True, None, [
        cp(platform="meta", campaign_name="meta-camp", clicks=200, ga4_sessions=100),
    ])
    r = make_report(platforms=[meta_pr])
    out = "\n\n".join(render_report(r))
    assert "Tracking & consent:" not in out


# ---------- Landing-page flags (paid traffic, 0 conversions) ----------

def _cp_with_landing(landing):
    """CampaignPerf factory that bolts ga4_landing_pages on after construction.

    GA4 numbers are deliberately balanced with the campaign-level numbers so
    the existing consent/attribution/CPA flags don't fire — these tests are
    specifically about the landing-page subsection, not the cross-reference
    flags it lives next to."""
    from scripts.mcp_clients import LandingPagePerf
    c = cp(
        campaign_name="Pricing search",
        clicks=200, conversions=10.0, spend=100.0,
        ga4_sessions=200, ga4_conversions=10.0,
    )
    c.ga4_landing_pages = [LandingPagePerf(**lp) for lp in landing]
    return c


def test_landing_flag_surfaces_paid_pages_with_zero_conversions():
    pr = PlatformResult("google", True, True, None, [
        _cp_with_landing([
            {"page_path": "/features", "sessions": 80, "conversions": 0.0},
            {"page_path": "/pricing", "sessions": 120, "conversions": 12.0},  # converts — should NOT flag
        ]),
    ])
    pr.ga4_status = "ok"
    pr.ga4_landing_status = "ok"
    r = make_report(platforms=[pr])
    out = "\n\n".join(render_report(r))
    assert "Tracking & consent:" in out
    assert "Landing pages with paid traffic, 0 conversions:" in out
    assert "/features" in out
    assert "80 paid sessions" in out
    # The converting page must not appear in the landing-flag list.
    assert "/pricing" not in out


def test_landing_flag_skips_pages_below_session_threshold():
    """Low-traffic pages are noise — the threshold keeps the report tight."""
    pr = PlatformResult("google", True, True, None, [
        _cp_with_landing([
            {"page_path": "/quiet", "sessions": 5, "conversions": 0.0},
            {"page_path": "/dead", "sessions": 19, "conversions": 0.0},  # just under threshold
        ]),
    ])
    pr.ga4_status = "ok"
    pr.ga4_landing_status = "ok"
    r = make_report(platforms=[pr])
    out = "\n\n".join(render_report(r))
    assert "Landing pages with paid traffic" not in out


def test_landing_flag_caps_to_top_n_by_sessions():
    """When many pages qualify, the top-traffic ones win — anything
    beyond the cap is dropped, not folded into a 'and N more' line."""
    pr = PlatformResult("google", True, True, None, [
        _cp_with_landing([
            {"page_path": f"/page-{i}", "sessions": 100 + i, "conversions": 0.0}
            for i in range(10)
        ]),
    ])
    pr.ga4_status = "ok"
    pr.ga4_landing_status = "ok"
    r = make_report(platforms=[pr])
    out = "\n\n".join(render_report(r))
    # Top by sessions desc: /page-9 (109) … /page-5 (105). The cap is 5.
    assert "/page-9" in out
    assert "/page-5" in out
    assert "/page-4" not in out  # would be 6th, must be dropped


def test_landing_flag_renders_alongside_consent_flag_in_one_section():
    """The two kinds of cross-reference signal share the same section
    so operators have one place to look — verify they don't split."""
    from scripts.mcp_clients import LandingPagePerf

    c = cp(campaign_name="EU brand", clicks=200, ga4_sessions=100, conversions=4.0, ga4_conversions=4.0)
    c.ga4_landing_pages = [LandingPagePerf(page_path="/dead", sessions=60, conversions=0.0)]
    pr = PlatformResult("google", True, True, None, [c])
    pr.ga4_status = "ok"
    pr.ga4_landing_status = "ok"
    r = make_report(platforms=[pr])
    rendered = render_report(r)
    # One Tracking & consent section, not two.
    tracking_sections = [s for s in rendered if "Tracking & consent:" in s]
    assert len(tracking_sections) == 1
    section = tracking_sections[0]
    assert "consent gap" in section
    assert "/dead" in section


def test_landing_flag_section_omitted_when_landing_pages_clean():
    pr = PlatformResult("google", True, True, None, [
        _cp_with_landing([
            {"page_path": "/converting", "sessions": 200, "conversions": 10.0},
        ]),
    ])
    pr.ga4_status = "ok"
    pr.ga4_landing_status = "ok"
    r = make_report(platforms=[pr])
    out = "\n\n".join(render_report(r))
    assert "Tracking & consent:" not in out


# ---- Phase 2 action rendering ---------------------------------------------

def _action_row(*, applied=False, dry_run=False, error=None, kind="pause",
                platform="google", name="Zombie", before=None, after=None):
    return {
        "decision": "auto",
        "rule": "x",
        "explanation": "exp",
        "mutation": {
            "platform": platform,
            "campaign_id": "C1",
            "campaign_name": name,
            "kind": kind,
            "before": before or {"status": "ENABLED"},
            "after": after or {"status": "PAUSED"},
            "reason": "r",
        },
        "applied": applied,
        "dry_run": dry_run,
        "error": error,
    }


def test_actions_taken_renders_applied_row():
    r = make_report(
        platforms=[PlatformResult("google", True, True, None, [cp()])],
        actions_taken=[_action_row(applied=True)],
    )
    out = "\n\n".join(render_report(r))
    assert "Actions taken:" in out
    assert "Zombie" in out
    assert "applied" in out
    assert "ENABLED → PAUSED" in out


def test_actions_taken_renders_dry_run_row():
    r = make_report(
        platforms=[PlatformResult("google", True, True, None, [cp()])],
        actions_taken=[_action_row(dry_run=True)],
    )
    out = "\n\n".join(render_report(r))
    assert "preview only" in out and "dry-run" in out


def test_actions_taken_renders_failed_row():
    r = make_report(
        platforms=[PlatformResult("google", True, True, None, [cp()])],
        actions_taken=[_action_row(applied=False, error="MCPToolError: not found")],
    )
    out = "\n\n".join(render_report(r))
    assert "FAILED" in out and "not found" in out


def test_actions_taken_renders_budget_change_detail():
    r = make_report(
        platforms=[PlatformResult("google", True, True, None, [cp()])],
        actions_taken=[_action_row(applied=True, kind="budget_change",
                                   before={"daily_budget": 100.0},
                                   after={"daily_budget": 80.0})],
    )
    out = "\n\n".join(render_report(r))
    assert "$100.00" in out and "$80.00" in out


def test_pending_approvals_section_includes_approve_command():
    approval = {
        "decision": "approval",
        "rule": "budget_pct_cap",
        "explanation": "Budget change +35.0% exceeds the ±20% per-run cap (100 → 135).",
        "mutation": {
            "platform": "google", "campaign_id": "X", "campaign_name": "Search Brand",
            "kind": "budget_change",
            "before": {"daily_budget": 100.0}, "after": {"daily_budget": 135.0},
            "reason": "Conversions up 80%",
        },
    }
    r = make_report(
        platforms=[PlatformResult("google", True, True, None, [cp()])],
        pending_approvals=[approval],
    )
    out = "\n\n".join(render_report(r))
    assert "Pending approvals:" in out
    assert "Search Brand" in out
    # The instruction should include the actual --approve command shape
    assert "--approve" in out
    assert f"{r.run_id}:0" in out
