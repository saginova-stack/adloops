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
