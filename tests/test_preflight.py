"""Tests for the operator preflight (`python -m scripts.run --check`).

Each test injects fake clients via monkeypatch and stubs urlopen for the
Telegram live ping so no real network calls escape the suite.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from scripts import preflight
from scripts.mcp_clients import MissingCredentialsError


# ---- shared scaffolding ----------------------------------------------------

VALID_BRAND = {
    "company": {"name": "Test Co"},
    "icp": {"personas": ["Head of Growth"]},
    "valueProps": ["a"],
    "brandVoice": {"tone": "b"},
    "guardrails": {
        "maxDailyBudgetChangePct": 20,
        "platforms": {"google": True, "meta": True, "linkedin": True},
    },
}


def _write_brand(tmp_path: Path, raw: dict | None = None) -> Path:
    p = tmp_path / "brand.json"
    p.write_text(json.dumps(raw or VALID_BRAND))
    return p


def _stub_loader(monkeypatch, brand_path: Path):
    """Point load_or_raise / paths at the temp brand.json so the test
    isn't reading the operator's real brand file."""
    from scripts import paths
    monkeypatch.setattr(paths, "brand_json_path", lambda: brand_path)


class _FakeClient:
    """Read client that just constructs — no real env or network."""
    def __init__(self): pass


def _stub_clients_ok(monkeypatch):
    """Every platform + GA4 instantiates without error."""
    monkeypatch.setattr(preflight, "GoogleAdsClient", _FakeClient)
    monkeypatch.setattr(preflight, "MetaAdsClient", _FakeClient)
    monkeypatch.setattr(preflight, "LinkedInAdsClient", _FakeClient)
    monkeypatch.setattr(preflight, "GoogleAnalyticsClient", _FakeClient)


def _stub_telegram_ok(monkeypatch):
    """Telegram live ping returns ok=true for both getMe and getChat."""
    def fake_get(token, method, params=None):
        if method == "getMe":
            return {"username": "test_bot", "id": 999}
        if method == "getChat":
            return {"id": -100, "title": "test chat"}
        raise AssertionError(f"unexpected telegram method: {method}")
    monkeypatch.setattr(preflight, "_telegram_get", fake_get)


def _set_telegram_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T")
    monkeypatch.setenv("ADLOOPS_TELEGRAM_CHAT_ID", "-100")


# ---- happy path ------------------------------------------------------------

def test_all_green_returns_zero(monkeypatch, tmp_path, capsys):
    _stub_loader(monkeypatch, _write_brand(tmp_path))
    _stub_clients_ok(monkeypatch)
    _set_telegram_env(monkeypatch)
    _stub_telegram_ok(monkeypatch)

    rc = preflight.run()
    assert rc == 0
    out = capsys.readouterr().out
    assert "All preflight checks passed" in out
    # Each green check should report one line
    for plat in ("google", "meta", "linkedin", "ga4", "telegram"):
        assert plat in out


# ---- brand failures --------------------------------------------------------

def test_missing_brand_fails(monkeypatch, tmp_path, capsys):
    _stub_loader(monkeypatch, tmp_path / "nope.json")
    _stub_clients_ok(monkeypatch)
    _set_telegram_env(monkeypatch)
    _stub_telegram_ok(monkeypatch)

    rc = preflight.run()
    assert rc == 1
    out = capsys.readouterr().out
    assert "brand.json" in out
    assert "FAIL" in out


def test_invalid_brand_skips_platforms_intelligently(monkeypatch, tmp_path, capsys):
    """If brand.json doesn't load, we can't tell which platforms are enabled
    — fall back to checking all three so the operator still sees the env
    state. Skipping silently here would hide the second class of error."""
    _stub_loader(monkeypatch, tmp_path / "nope.json")
    _stub_clients_ok(monkeypatch)
    _set_telegram_env(monkeypatch)
    _stub_telegram_ok(monkeypatch)

    preflight.run()
    out = capsys.readouterr().out
    # All three platform checks ran despite missing brand
    assert "google: env vars set" in out
    assert "meta: env vars set" in out
    assert "linkedin: env vars set" in out


# ---- platform failures -----------------------------------------------------

def test_missing_platform_env_fails(monkeypatch, tmp_path, capsys):
    _stub_loader(monkeypatch, _write_brand(tmp_path))

    class Boom:
        def __init__(self):
            raise MissingCredentialsError("Meta Ads: missing env ['META_ACCESS_TOKEN']")

    monkeypatch.setattr(preflight, "GoogleAdsClient", _FakeClient)
    monkeypatch.setattr(preflight, "MetaAdsClient", Boom)
    monkeypatch.setattr(preflight, "LinkedInAdsClient", _FakeClient)
    monkeypatch.setattr(preflight, "GoogleAnalyticsClient", _FakeClient)
    _set_telegram_env(monkeypatch)
    _stub_telegram_ok(monkeypatch)

    rc = preflight.run()
    assert rc == 1
    out = capsys.readouterr().out
    assert "meta" in out and "META_ACCESS_TOKEN" in out
    # The other platforms aren't penalised — only meta is in the failure list
    assert "1 check(s) failed: meta" in out


def test_disabled_platform_skipped_not_failed(monkeypatch, tmp_path, capsys):
    """A platform disabled in brand.json shouldn't require its env to be set."""
    brand = json.loads(json.dumps(VALID_BRAND))  # deep copy
    brand["guardrails"]["platforms"]["linkedin"] = False
    _stub_loader(monkeypatch, _write_brand(tmp_path, brand))

    class Boom:
        def __init__(self):
            raise MissingCredentialsError("LinkedIn: missing env")

    monkeypatch.setattr(preflight, "GoogleAdsClient", _FakeClient)
    monkeypatch.setattr(preflight, "MetaAdsClient", _FakeClient)
    monkeypatch.setattr(preflight, "LinkedInAdsClient", Boom)
    monkeypatch.setattr(preflight, "GoogleAnalyticsClient", _FakeClient)
    _set_telegram_env(monkeypatch)
    _stub_telegram_ok(monkeypatch)

    rc = preflight.run()
    assert rc == 0  # linkedin disabled, so missing env shouldn't fail
    out = capsys.readouterr().out
    assert "linkedin: disabled in brand.json" in out


# ---- GA4 is a warning, not a failure ---------------------------------------

def test_missing_ga4_warns_does_not_fail(monkeypatch, tmp_path, capsys):
    _stub_loader(monkeypatch, _write_brand(tmp_path))
    monkeypatch.setattr(preflight, "GoogleAdsClient", _FakeClient)
    monkeypatch.setattr(preflight, "MetaAdsClient", _FakeClient)
    monkeypatch.setattr(preflight, "LinkedInAdsClient", _FakeClient)

    class GA4Boom:
        def __init__(self):
            raise MissingCredentialsError("GA4: missing env ['GA4_PROPERTY_ID']")

    monkeypatch.setattr(preflight, "GoogleAnalyticsClient", GA4Boom)
    _set_telegram_env(monkeypatch)
    _stub_telegram_ok(monkeypatch)

    rc = preflight.run()
    assert rc == 0
    out = capsys.readouterr().out
    assert "warn" in out.lower() and "ga4" in out
    assert "enrichment will skip, audit still runs" in out


# ---- telegram failures -----------------------------------------------------

def test_missing_telegram_bot_token_fails(monkeypatch, tmp_path, capsys):
    _stub_loader(monkeypatch, _write_brand(tmp_path))
    _stub_clients_ok(monkeypatch)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("ADLOOPS_TELEGRAM_CHAT_ID", "-100")

    rc = preflight.run()
    assert rc == 1
    out = capsys.readouterr().out
    assert "TELEGRAM_BOT_TOKEN not set" in out


def test_missing_telegram_chat_id_fails(monkeypatch, tmp_path, capsys):
    _stub_loader(monkeypatch, _write_brand(tmp_path))
    _stub_clients_ok(monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T")
    monkeypatch.delenv("ADLOOPS_TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    rc = preflight.run()
    assert rc == 1
    out = capsys.readouterr().out
    assert "ADLOOPS_TELEGRAM_CHAT_ID" in out


def test_telegram_unreachable_chat_fails(monkeypatch, tmp_path, capsys):
    """The most common first-run mistake: token is set, chat id is set,
    but the bot isn't actually a member of the chat. getChat returns
    ok=false; preflight must catch this loudly."""
    _stub_loader(monkeypatch, _write_brand(tmp_path))
    _stub_clients_ok(monkeypatch)
    _set_telegram_env(monkeypatch)

    def fake_get(token, method, params=None):
        if method == "getMe":
            return {"username": "test_bot"}
        # Simulate bot-not-in-chat: _telegram_get raises on ok=false
        raise RuntimeError("telegram getChat: Bad Request: chat not found")

    monkeypatch.setattr(preflight, "_telegram_get", fake_get)
    rc = preflight.run()
    assert rc == 1
    out = capsys.readouterr().out
    assert "chat not found" in out
