from __future__ import annotations

import json
import urllib.error

import pytest

from scripts import recommender
from scripts.audit import AuditReport, CampaignDelta, PlatformResult
from scripts.brand_loader import Brand
from scripts.mcp_clients import CampaignPerf


def _brand(tone: str = "Direct, no-nonsense"):
    return Brand(raw={
        "company": {"name": "Acme"},
        "icp": {"personas": ["Ops director"]},
        "valueProps": ["Saves time"],
        "brandVoice": {"tone": tone},
        "guardrails": {"maxDailyBudgetChangePct": 20,
                       "platforms": {"google": True, "meta": False, "linkedin": False}},
    })


def _report_with_zombie():
    camp = CampaignPerf(
        platform="google", campaign_id="Z", campaign_name="Zombie",
        status="ENABLED", daily_budget=50.0,
        spend=200.0, impressions=1000, clicks=20, conversions=0, revenue=None,
    )
    return AuditReport(
        run_id="R", generated_at="x", window_start="2026-05-04", window_end="2026-05-10",
        platforms=[PlatformResult("google", True, True, None, [camp])],
        deltas=[CampaignDelta(
            platform="google", campaign_id="Z", campaign_name="Zombie",
            spend_now=200.0, spend_prev=None, spend_change_pct=None,
            cpa_now=None, cpa_prev=None, conversions_now=0, conversions_prev=None,
        )],
        top_movers_best=[], top_movers_worst=[],
        actions_taken=[], pending_approvals=[], recommendations=[],
    )


# ---- fallback (no LLM key) -----------------------------------------------

def test_falls_back_to_rule_based_when_no_keys(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    recs = recommender.recommendations(_report_with_zombie(), _brand())
    assert any("Pause" in r and "Zombie" in r for r in recs)


# ---- OpenRouter path ------------------------------------------------------

def _canned_urlopen(payload: dict):
    """Return a context-manager factory whose .read() yields json.dumps(payload)."""
    class _CM:
        def __enter__(self_inner):
            return self_inner
        def __exit__(self_inner, *a):
            return False
        def read(self_inner):
            return json.dumps(payload).encode("utf-8")
    def _fake(req, timeout=None):
        # Capture the call for inspection by the test
        _fake.last_request = req
        return _CM()
    _fake.last_request = None
    return _fake


def test_openrouter_called_when_key_present(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-router")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fake = _canned_urlopen({
        "choices": [{"message": {"content": (
            "1. Pause google:Zombie — 0 conversions on $200 spend.\n"
            "2. Investigate google:Brand for tracking gaps.\n"
            "3. Increase budget on google:Search-US if CPA stable.\n"
        )}}]
    })
    monkeypatch.setattr(recommender.urllib.request, "urlopen", fake)
    recs = recommender.recommendations(_report_with_zombie(), _brand())
    assert any("Pause google:Zombie" in r for r in recs)
    assert len(recs) == 3
    # Confirm the request was the OpenRouter endpoint with the auth header
    req = fake.last_request
    assert "openrouter.ai" in req.full_url
    assert req.get_header("Authorization") == "Bearer sk-router"


def test_openrouter_failure_falls_through_to_anthropic(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-r")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-a")

    called = {"router": 0, "anthropic": 0}

    def fake_urlopen(req, timeout=None):
        if "openrouter" in req.full_url:
            called["router"] += 1
            raise urllib.error.URLError("DNS failed")
        called["anthropic"] += 1

        class CM:
            def __enter__(self_inner): return self_inner
            def __exit__(self_inner, *a): return False
            def read(self_inner):
                return json.dumps({
                    "content": [{"type": "text", "text": "- Anthropic suggestion line 1\n- Another"}]
                }).encode("utf-8")
        return CM()

    monkeypatch.setattr(recommender.urllib.request, "urlopen", fake_urlopen)
    recs = recommender.recommendations(_report_with_zombie(), _brand())
    assert called == {"router": 1, "anthropic": 1}
    assert recs[0].startswith("Anthropic suggestion")


def test_all_llms_fail_falls_through_to_rule_based(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-r")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-a")

    def always_fail(req, timeout=None):
        raise urllib.error.URLError("boom")

    monkeypatch.setattr(recommender.urllib.request, "urlopen", always_fail)
    recs = recommender.recommendations(_report_with_zombie(), _brand())
    # rule-based always flags zombies
    assert any("Pause" in r and "Zombie" in r for r in recs)


# ---- prompt construction -------------------------------------------------

def test_prompt_includes_brand_voice_and_window():
    report = _report_with_zombie()
    prompt = recommender._build_prompt(report, _brand(tone="Witty British SaaS"))
    assert "Acme" in prompt
    assert "Witty British SaaS" in prompt
    assert "2026-05-04" in prompt and "2026-05-10" in prompt


def test_prompt_surfaces_consent_gap_flag():
    # Build a Google campaign with a clearly-flagged consent gap (clicks=200,
    # GA4 sessions=100 → 50% gap, above the 30% threshold).
    camp = CampaignPerf(
        platform="google", campaign_id="X", campaign_name="EU-Brand",
        status="ENABLED", daily_budget=50.0,
        spend=400.0, impressions=10000, clicks=200, conversions=8.0, revenue=None,
        ga4_sessions=100, ga4_conversions=8.0, ga4_revenue=None,
    )
    report = AuditReport(
        run_id="R", generated_at="x", window_start="2026-05-04", window_end="2026-05-10",
        platforms=[PlatformResult("google", True, True, None, [camp])],
        deltas=[], top_movers_best=[], top_movers_worst=[],
        actions_taken=[], pending_approvals=[], recommendations=[],
    )
    prompt = recommender._build_prompt(report, _brand())
    assert "consent gap" in prompt
    assert "EU-Brand" in prompt


# ---- response parsing ----------------------------------------------------

def test_parse_recs_strips_bullets_and_numbers():
    text = (
        "Here are 4 recommendations:\n"
        "1. Pause google:Z — wasted $200\n"
        "- Cut meta:Y budget by 20%\n"
        "* Investigate consent gap on EU\n"
        "  • Scale google:US-Search\n"
    )
    out = recommender._parse_recs(text)
    assert out == [
        "Pause google:Z — wasted $200",
        "Cut meta:Y budget by 20%",
        "Investigate consent gap on EU",
        "Scale google:US-Search",
    ]


def test_parse_recs_caps_at_max():
    text = "\n".join(f"- rec {i}" for i in range(20))
    out = recommender._parse_recs(text)
    assert len(out) == recommender.MAX_RECS


def test_parse_recs_drops_header_lines():
    text = "Recommendations:\n- alpha\n- beta"
    out = recommender._parse_recs(text)
    assert out == ["alpha", "beta"]
