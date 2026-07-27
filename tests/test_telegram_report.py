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


def test_consent_gap_shows_week_over_week_trend_when_prev_snapshot_present():
    """When a prior snapshot exists, the consent-gap line gets a 'Xpp w/w'
    suffix so the operator can tell a worsening gap from a stable bad one."""
    # Current: 200 clicks → 100 sessions = 50% gap
    # Prior:   200 clicks → 160 sessions = 20% gap
    # Delta: +30pp
    pr = PlatformResult("google", True, True, None, [
        cp(campaign_id="C1", campaign_name="EU brand",
           clicks=200, ga4_sessions=100, conversions=4.0, ga4_conversions=4.0),
    ])
    pr.ga4_status = "ok"
    prev = {
        "platforms": [{
            "campaigns": [{
                "platform": "google", "campaign_id": "C1",
                "clicks": 200, "ga4_sessions": 160,
                "conversions": 4.0, "ga4_conversions": 4.0,
            }],
        }],
    }
    r = make_report(platforms=[pr], prev_snapshot=prev)
    out = "\n\n".join(render_report(r))
    assert "50% consent gap (+30pp w/w)" in out


def test_consent_gap_trend_omitted_when_no_prev_snapshot():
    """First run has no prior — the trend suffix must be absent, not
    rendered as '+50pp w/w' off a None baseline."""
    pr = PlatformResult("google", True, True, None, [
        cp(campaign_id="C1", campaign_name="EU brand",
           clicks=200, ga4_sessions=100, conversions=4.0, ga4_conversions=4.0),
    ])
    pr.ga4_status = "ok"
    r = make_report(platforms=[pr], prev_snapshot=None)
    out = "\n\n".join(render_report(r))
    assert "50% consent gap" in out
    assert "w/w" not in out


def test_consent_gap_trend_suppressed_when_delta_is_noise():
    """A 0.5pp drift isn't worth surfacing — keeps the report quiet when
    nothing changed. Threshold lives in _trend_suffix."""
    pr = PlatformResult("google", True, True, None, [
        cp(campaign_id="C1", campaign_name="EU brand",
           clicks=200, ga4_sessions=100, conversions=4.0, ga4_conversions=4.0),
    ])
    pr.ga4_status = "ok"
    # Prior 199 clicks → 100 sessions = ~49.7% — 0.25pp delta, below noise floor.
    prev = {
        "platforms": [{
            "campaigns": [{
                "platform": "google", "campaign_id": "C1",
                "clicks": 199, "ga4_sessions": 100,
                "conversions": 4.0, "ga4_conversions": 4.0,
            }],
        }],
    }
    r = make_report(platforms=[pr], prev_snapshot=prev)
    out = "\n\n".join(render_report(r))
    assert "consent gap" in out
    assert "w/w" not in out


def test_attribution_gap_shows_trend_too():
    """Symmetric to consent gap — the second cross-reference signal
    also benefits from the directional context."""
    # Current: Ads 10 / GA4 14 → 40% gap
    # Prior:   Ads 10 / GA4 12 → 20% gap
    # Delta: +20pp
    pr = PlatformResult("google", True, True, None, [
        cp(campaign_id="C2", campaign_name="Search US",
           clicks=200, ga4_sessions=190, conversions=10.0, ga4_conversions=14.0),
    ])
    pr.ga4_status = "ok"
    prev = {
        "platforms": [{
            "campaigns": [{
                "platform": "google", "campaign_id": "C2",
                "clicks": 200, "ga4_sessions": 190,
                "conversions": 10.0, "ga4_conversions": 12.0,
            }],
        }],
    }
    r = make_report(platforms=[pr], prev_snapshot=prev)
    out = "\n\n".join(render_report(r))
    assert "attribution gap" in out
    assert "+20pp w/w" in out


def test_trend_shows_minus_sign_on_improving_gap():
    """A shrinking gap is good news — operator wants to see '-Xpp' not
    just absence of the suffix. Confirms the sign is rendered correctly."""
    # Current 50% gap (200 clicks / 100 sessions)
    # Prior 70% gap (200 clicks / 60 sessions). Delta: -20pp (improving).
    pr = PlatformResult("google", True, True, None, [
        cp(campaign_id="C1", campaign_name="EU brand",
           clicks=200, ga4_sessions=100, conversions=4.0, ga4_conversions=4.0),
    ])
    pr.ga4_status = "ok"
    prev = {
        "platforms": [{
            "campaigns": [{
                "platform": "google", "campaign_id": "C1",
                "clicks": 200, "ga4_sessions": 60,
                "conversions": 4.0, "ga4_conversions": 4.0,
            }],
        }],
    }
    r = make_report(platforms=[pr], prev_snapshot=prev)
    out = "\n\n".join(render_report(r))
    assert "(-20pp w/w)" in out


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


def test_previewed_actions_replace_actions_taken_section():
    """Under --no-mutate the run.py path populates previewed_actions
    instead of actions_taken. The report must surface the would-be
    decisions, NOT 'no mutations proposed' (which would be misleading
    when proposals actually existed but didn't ship)."""
    preview_row = {
        "decision": "auto",
        "rule": "always_allow_pause",
        "explanation": "Pausing is always allowed.",
        "mutation": {
            "platform": "google",
            "campaign_id": "Z1",
            "campaign_name": "Zombie ads",
            "kind": "pause",
            "before": {"status": "ENABLED"},
            "after": {"status": "PAUSED"},
            "reason": "$200 spend, 0 conv.",
        },
    }
    r = make_report(
        platforms=[PlatformResult("google", True, True, None, [cp()])],
        previewed_actions=[preview_row],
    )
    out = "\n\n".join(render_report(r))
    assert "Would have fired (--no-mutate observation):" in out
    assert "Zombie ads" in out
    assert "[AUTO]" in out
    # The misleading default copy must not appear when there's preview content.
    assert "no mutations proposed this run" not in out
    # And the live "Actions taken:" header must not be used in preview mode.
    assert "Actions taken:" not in out


def test_previewed_actions_show_each_decision_kind():
    """The whole point of --no-mutate is seeing the decision distribution
    (AUTO vs APPROVAL vs REJECTED) so the operator can tune brand.json
    before flipping mutations on. All three decisions must render."""
    rows = []
    for decision, name in (("auto", "Z"), ("approval", "Big"), ("rejected", "Bad")):
        rows.append({
            "decision": decision,
            "rule": "x",
            "explanation": f"sample {decision} verdict",
            "mutation": {
                "platform": "google",
                "campaign_id": name,
                "campaign_name": name,
                "kind": "budget_change",
                "before": {"daily_budget": 100.0},
                "after": {"daily_budget": 80.0},
                "reason": "r",
            },
        })
    r = make_report(
        platforms=[PlatformResult("google", True, True, None, [cp()])],
        previewed_actions=rows,
    )
    out = "\n\n".join(render_report(r))
    assert "[AUTO]" in out
    assert "[APPROVAL]" in out
    assert "[REJECTED]" in out


def test_landing_flag_shows_w_w_session_trend_when_prev_snapshot_present():
    """Same problem the consent-gap trend solves: telling a new dud
    landing page from a chronic one. Session delta is rendered as an
    absolute number (not pp) because landing pages think in counts."""
    pr = PlatformResult("google", True, True, None, [
        _cp_with_landing([{"page_path": "/features", "sessions": 80, "conversions": 0.0}]),
    ])
    pr.ga4_status = "ok"
    pr.ga4_landing_status = "ok"
    prev = {
        "platforms": [{
            "campaigns": [{
                "platform": "google", "campaign_id": "1",
                "ga4_landing_pages": [
                    {"page_path": "/features", "sessions": 30, "conversions": 0.0},
                ],
            }],
        }],
    }
    r = make_report(platforms=[pr], prev_snapshot=prev)
    out = "\n\n".join(render_report(r))
    assert "/features" in out
    assert "80 paid sessions (+50 w/w)" in out


def test_landing_flag_trend_omitted_when_no_prev():
    pr = PlatformResult("google", True, True, None, [
        _cp_with_landing([{"page_path": "/dead", "sessions": 60, "conversions": 0.0}]),
    ])
    pr.ga4_status = "ok"
    pr.ga4_landing_status = "ok"
    r = make_report(platforms=[pr], prev_snapshot=None)
    out = "\n\n".join(render_report(r))
    assert "60 paid sessions" in out
    assert "w/w" not in out


def test_landing_flag_trend_suppressed_below_noise_floor():
    """A 3-session delta on a 25-session baseline isn't a signal; the
    suffix would just add visual clutter to a probably-stable problem."""
    pr = PlatformResult("google", True, True, None, [
        _cp_with_landing([{"page_path": "/dead", "sessions": 25, "conversions": 0.0}]),
    ])
    pr.ga4_status = "ok"
    pr.ga4_landing_status = "ok"
    prev = {
        "platforms": [{
            "campaigns": [{
                "platform": "google", "campaign_id": "1",
                "ga4_landing_pages": [
                    {"page_path": "/dead", "sessions": 22, "conversions": 0.0},
                ],
            }],
        }],
    }
    r = make_report(platforms=[pr], prev_snapshot=prev)
    out = "\n\n".join(render_report(r))
    assert "/dead" in out
    assert "w/w" not in out


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


# ---- create_campaign rendering (reconciler call-to-action) ----------------

def _create_action(after, *, applied=True):
    return {
        "decision": "auto", "rule": "new_campaign_paused_ok", "explanation": "",
        "mutation": {"platform": "meta", "campaign_id": "", "campaign_name": after["name"],
                     "kind": "create_campaign", "before": {}, "after": after, "reason": ""},
        "applied": applied,
    }


def test_create_campaign_action_shows_objective_budget_and_launch_nudge():
    """A bare 'create_campaign (applied)' would leave the operator unaware they
    have a PAUSED shell to populate. The line must carry the objective, budget,
    and an explicit go-live nudge."""
    after = {"name": "Q3 Leads", "objective": "OUTCOME_LEADS",
             "status": "PAUSED", "daily_budget": 25.0}
    r = make_report(actions_taken=[_create_action(after)])
    body = "\n".join(render_report(r))
    assert "Q3 Leads: create_campaign" in body
    assert "OUTCOME_LEADS" in body
    assert "$25.00/day" in body
    assert "launches PAUSED" in body
    assert "add ad sets & enable to go live" in body


def test_create_campaign_action_notes_scaffolded_ad_set():
    after = {"name": "Q3 Leads", "objective": "OUTCOME_LEADS", "status": "PAUSED",
             "daily_budget": 25.0, "ad_set": {"name": "US", "optimization_goal": "LEAD_GENERATION",
                                              "billing_event": "IMPRESSIONS",
                                              "targeting": {"geo_locations": {"countries": ["US"]}}}}
    r = make_report(actions_taken=[_create_action(after)])
    body = "\n".join(render_report(r))
    assert "with a default ad set" in body
    assert "review & enable to go live" in body
