"""Smoke tests for the entrypoint — verifies the high-level flow exits with the
right code under the conditions we care about.

We don't exercise the live API paths here (those need credentials) — instead
we monkeypatch audit.run_audit / fetch_all so the script's plumbing is
validated end-to-end without leaving the box.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import run as run_mod
from scripts.audit import AuditReport, PlatformResult


def _good_brand(tmp_path: Path) -> Path:
    bdir = tmp_path / "brand"
    (bdir / "campaigns" / ".archive").mkdir(parents=True)
    (bdir / "brand.json").write_text(json.dumps({
        "company": {"name": "Acme"},
        "icp": {"personas": ["x"]},
        "valueProps": ["y"],
        "brandVoice": {"tone": "z"},
        "guardrails": {
            "maxDailyBudgetChangePct": 20,
            "platforms": {"google": True, "meta": False, "linkedin": False},
        },
    }))
    return bdir


def _empty_personas_brand(tmp_path: Path) -> Path:
    bdir = tmp_path / "brand"
    bdir.mkdir(parents=True)
    (bdir / "brand.json").write_text(json.dumps({
        "company": {"name": "Acme"},
        "icp": {"personas": []},
        "valueProps": [],
        "brandVoice": {},
        "guardrails": {
            "maxDailyBudgetChangePct": 20,
            "platforms": {"google": True, "meta": False, "linkedin": False},
        },
    }))
    return bdir


def _stub_report(*, fetched: bool) -> AuditReport:
    return AuditReport(
        run_id="20260510T090000Z",
        generated_at="2026-05-10T09:00:00Z",
        window_start="2026-05-03",
        window_end="2026-05-09",
        platforms=[PlatformResult("google", True, fetched, None if fetched else "no creds", [])],
        deltas=[],
        top_movers_best=[],
        top_movers_worst=[],
        actions_taken=[],
        pending_approvals=[],
        recommendations=[],
    )


def test_scaffold_exits_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ADLOOPS_BRAND_DIR", str(tmp_path / "brand"))
    rc = run_mod.main(["--scaffold"])
    assert rc == 0
    assert (tmp_path / "brand" / "brand.json").exists()
    out = capsys.readouterr().out
    assert "Scaffolded" in out


def test_brand_missing_returns_2_and_attempts_telegram_notice(tmp_path, monkeypatch):
    monkeypatch.setenv("ADLOOPS_BRAND_DIR", str(tmp_path / "nope"))
    sent: list[str] = []
    monkeypatch.setattr(run_mod.telegram_report, "send_messages",
                        lambda msgs, dry_run=False: sent.extend(msgs) or [])
    rc = run_mod.main(["--dry-run"])
    assert rc == 2
    assert any("brand.json" in m for m in sent)


def test_empty_personas_returns_2(tmp_path, monkeypatch):
    bdir = _empty_personas_brand(tmp_path)
    monkeypatch.setenv("ADLOOPS_BRAND_DIR", str(bdir))
    monkeypatch.setattr(run_mod.telegram_report, "send_messages",
                        lambda msgs, dry_run=False: [])
    rc = run_mod.main(["--dry-run"])
    assert rc == 2


def test_no_enabled_platforms_returns_3(tmp_path, monkeypatch):
    bdir = tmp_path / "brand"
    (bdir / "campaigns" / ".archive").mkdir(parents=True)
    (bdir / "brand.json").write_text(json.dumps({
        "company": {"name": "Acme"},
        "icp": {"personas": ["x"]},
        "valueProps": [],
        "brandVoice": {},
        "guardrails": {
            "maxDailyBudgetChangePct": 20,
            "platforms": {"google": False, "meta": False, "linkedin": False},
        },
    }))
    monkeypatch.setenv("ADLOOPS_BRAND_DIR", str(bdir))
    monkeypatch.setattr(run_mod.telegram_report, "send_messages",
                        lambda msgs, dry_run=False: [])
    rc = run_mod.main(["--dry-run"])
    assert rc == 3


def test_happy_path_dry_run(tmp_path, monkeypatch, capsys):
    bdir = _good_brand(tmp_path)
    monkeypatch.setenv("ADLOOPS_BRAND_DIR", str(bdir))
    monkeypatch.setattr(run_mod.audit, "run_audit", lambda enabled: _stub_report(fetched=True))
    monkeypatch.setattr(run_mod.audit, "write_snapshot", lambda r: bdir / "campaigns" / ".archive" / "stub.json")
    monkeypatch.setattr(run_mod.telegram_report, "send_messages",
                        lambda msgs, dry_run=False: [{"dry_run": True} for _ in msgs])
    rc = run_mod.main(["--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "AdLoops" in out and "snapshot" in out


def test_audit_crash_returns_4_and_notifies(tmp_path, monkeypatch):
    bdir = _good_brand(tmp_path)
    monkeypatch.setenv("ADLOOPS_BRAND_DIR", str(bdir))
    sent: list[str] = []
    monkeypatch.setattr(run_mod.audit, "run_audit", lambda enabled: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(run_mod.telegram_report, "send_messages",
                        lambda msgs, dry_run=False: sent.extend(msgs) or [])
    rc = run_mod.main(["--dry-run"])
    assert rc == 4
    assert any("crashed" in m and "boom" in m for m in sent)


def test_all_platforms_failed_returns_7(tmp_path, monkeypatch):
    bdir = _good_brand(tmp_path)
    monkeypatch.setenv("ADLOOPS_BRAND_DIR", str(bdir))
    monkeypatch.setattr(run_mod.audit, "run_audit", lambda enabled: _stub_report(fetched=False))
    monkeypatch.setattr(run_mod.audit, "write_snapshot", lambda r: bdir / "stub.json")
    monkeypatch.setattr(run_mod.telegram_report, "send_messages",
                        lambda msgs, dry_run=False: [])
    rc = run_mod.main(["--dry-run"])
    assert rc == 7
