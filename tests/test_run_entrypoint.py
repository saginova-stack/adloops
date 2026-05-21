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


# ---- Phase 2: mutation pipeline integration ------------------------------

from scripts.guardrails import Decision, GuardrailResult, Mutation
from scripts.mcp_clients import CampaignPerf


def _report_with_zombie() -> AuditReport:
    """Stub report with one campaign that the proposer will pause."""
    camp = CampaignPerf(
        platform="google", campaign_id="ZOMBIE", campaign_name="Zombie Campaign",
        status="ENABLED", daily_budget=50.0,
        spend=300.0, impressions=10000, clicks=200, conversions=0.0, revenue=None,
    )
    return AuditReport(
        run_id="20260510T090000Z", generated_at="2026-05-10T09:00:00Z",
        window_start="2026-05-03", window_end="2026-05-09",
        platforms=[PlatformResult("google", True, True, None, [camp])],
        deltas=[],
        top_movers_best=[], top_movers_worst=[],
        actions_taken=[], pending_approvals=[], recommendations=[],
    )


def test_mutations_run_and_populate_actions_taken(tmp_path, monkeypatch):
    bdir = _good_brand(tmp_path)
    monkeypatch.setenv("ADLOOPS_BRAND_DIR", str(bdir))
    monkeypatch.setattr(run_mod.audit, "run_audit", lambda enabled: _report_with_zombie())
    monkeypatch.setattr(run_mod.audit, "write_snapshot", lambda r: bdir / "campaigns" / ".archive" / "x.json")
    monkeypatch.setattr(run_mod.telegram_report, "send_messages",
                        lambda msgs, dry_run=False: [])

    dispatched: list = []

    class FakeEx:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def dispatch(self, m, *, dry_run):
            dispatched.append((m.kind, m.campaign_id, dry_run))
            return {"status": "APPLIED" if not dry_run else "DRY_RUN"}

    monkeypatch.setattr(run_mod.executors.google, "GoogleExecutor", FakeEx)
    rc = run_mod.main(["--dry-run"])
    assert rc == 0
    assert dispatched == [("pause", "ZOMBIE", True)]


def test_no_mutate_flag_runs_preview_not_dispatch(tmp_path, monkeypatch):
    """Under --no-mutate the proposer DOES run (for the report's
    'Would have fired' section) but executors must never be reached.
    Verifies the observation-mode contract.
    """
    bdir = _good_brand(tmp_path)
    monkeypatch.setenv("ADLOOPS_BRAND_DIR", str(bdir))
    monkeypatch.setattr(run_mod.audit, "run_audit", lambda enabled: _report_with_zombie())
    monkeypatch.setattr(run_mod.audit, "write_snapshot", lambda r: bdir / "x.json")
    monkeypatch.setattr(run_mod.telegram_report, "send_messages",
                        lambda msgs, dry_run=False: [])

    dispatched: list = []
    monkeypatch.setattr(run_mod.executors, "dispatch",
                        lambda *a, **kw: dispatched.append(("dispatch", a, kw)) or {"ok": True})

    rc = run_mod.main(["--dry-run", "--no-mutate"])
    assert rc == 0
    # No real executor calls — that's the whole point of --no-mutate.
    assert dispatched == []


def test_no_mutate_populates_previewed_actions(tmp_path, monkeypatch):
    """The preview section is what makes --no-mutate worth running for two
    weeks instead of silently. Confirm the proposer's would-have-fired
    rows actually land on report.previewed_actions."""
    bdir = _good_brand(tmp_path)
    monkeypatch.setenv("ADLOOPS_BRAND_DIR", str(bdir))

    captured_reports: list = []
    def capture_send(msgs, dry_run=False):
        captured_reports.append(list(msgs))
        return []

    monkeypatch.setattr(run_mod.audit, "run_audit", lambda enabled: _report_with_zombie())
    monkeypatch.setattr(run_mod.audit, "write_snapshot", lambda r: bdir / "x.json")
    monkeypatch.setattr(run_mod.telegram_report, "send_messages", capture_send)
    monkeypatch.setattr(run_mod.executors, "dispatch",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not dispatch")))

    rc = run_mod.main(["--dry-run", "--no-mutate"])
    assert rc == 0
    combined = "\n".join(captured_reports[0]) if captured_reports else ""
    assert "Would have fired" in combined
    # The zombie campaign produces a pause proposal; the preview row should
    # carry the AUTO verdict from guardrails, and identify the campaign.
    assert "Zombie" in combined
    assert "[AUTO]" in combined
    assert "pause" in combined


def test_mutation_crash_does_not_crash_run(tmp_path, monkeypatch, capsys):
    bdir = _good_brand(tmp_path)
    monkeypatch.setenv("ADLOOPS_BRAND_DIR", str(bdir))
    monkeypatch.setattr(run_mod.audit, "run_audit", lambda enabled: _report_with_zombie())
    monkeypatch.setattr(run_mod.audit, "write_snapshot", lambda r: bdir / "x.json")
    monkeypatch.setattr(run_mod.telegram_report, "send_messages",
                        lambda msgs, dry_run=False: [])

    def boom(report, brand):
        raise RuntimeError("mutation proposer exploded")

    monkeypatch.setattr(run_mod.mutations, "propose", boom)
    rc = run_mod.main(["--dry-run"])
    assert rc == 0  # run still completes — audit + report still useful
    err = capsys.readouterr().err
    assert "mutation pipeline crashed" in err


def test_approval_decision_populates_pending_list(tmp_path, monkeypatch):
    """A budget change above the cap → APPROVAL → not dispatched, queued."""
    bdir = _good_brand(tmp_path)
    monkeypatch.setenv("ADLOOPS_BRAND_DIR", str(bdir))

    # Stub report has a campaign with CPA growth that would trigger budget change.
    from scripts.audit import CampaignDelta
    camp = CampaignPerf(
        platform="google", campaign_id="C1", campaign_name="C1",
        status="ENABLED", daily_budget=100.0,
        spend=300.0, impressions=1000, clicks=50, conversions=3.0, revenue=None,
    )
    delta = CampaignDelta(
        platform="google", campaign_id="C1", campaign_name="C1",
        spend_now=300.0, spend_prev=150.0, spend_change_pct=100.0,
        cpa_now=100.0, cpa_prev=30.0,
        conversions_now=3.0, conversions_prev=5.0,
    )
    report = AuditReport(
        run_id="R1", generated_at="2026-05-10T09:00:00Z",
        window_start="2026-05-03", window_end="2026-05-09",
        platforms=[PlatformResult("google", True, True, None, [camp])],
        deltas=[delta],
        top_movers_best=[], top_movers_worst=[],
        actions_taken=[], pending_approvals=[], recommendations=[],
    )
    monkeypatch.setattr(run_mod.audit, "run_audit", lambda enabled: report)
    monkeypatch.setattr(run_mod.audit, "write_snapshot", lambda r: bdir / "x.json")
    monkeypatch.setattr(run_mod.telegram_report, "send_messages",
                        lambda msgs, dry_run=False: [])

    # Force the budget change to be 30% (above the 20% cap) by stubbing the
    # proposer to emit one we control.
    def fake_propose(rep, brand):
        return [Mutation(
            platform="google", campaign_id="C1", campaign_name="C1",
            kind="budget_change",
            before={"daily_budget": 100.0}, after={"daily_budget": 70.0},  # -30%
            reason="forced for test",
        )]

    monkeypatch.setattr(run_mod.mutations, "propose", fake_propose)

    # Make sure no executor would ever fire — APPROVAL should never dispatch.
    def must_not_call(*a, **kw):
        raise AssertionError("APPROVAL must not dispatch executor")

    monkeypatch.setattr(run_mod.executors.google, "GoogleExecutor",
                        lambda: type("X", (), {"__enter__": lambda s: must_not_call(),
                                               "__exit__": lambda *a: None})())

    rc = run_mod.main(["--dry-run"])
    assert rc == 0
    assert len(report.pending_approvals) == 1
    assert report.pending_approvals[0]["decision"] == "approval"


# ---- --approve mode ------------------------------------------------------

def _seed_audit_log_with_approval(bdir, run_id="RUN_X", kind="pause", before=None, after=None):
    log = bdir / "campaigns" / ".audit.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"run_id": run_id, "decision": "auto", "platform": "google",
         "campaign_id": "A", "campaign_name": "A", "kind": "pause",
         "before": {}, "after": {}, "reason": "", "rule": "x", "applied": True},
        {"run_id": run_id, "decision": "approval", "platform": "google",
         "campaign_id": "B", "campaign_name": "B", "kind": kind,
         "before": before or {"status": "PAUSED"}, "after": after or {"status": "ENABLED"},
         "reason": "queued", "rule": "x", "applied": False},
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return log


def test_approve_invalid_spec_format(tmp_path, monkeypatch, capsys):
    bdir = _good_brand(tmp_path)
    monkeypatch.setenv("ADLOOPS_BRAND_DIR", str(bdir))
    rc = run_mod.main(["--approve", "no-colon"])
    assert rc == 8
    err = capsys.readouterr().err
    assert "RUN_ID:INDEX" in err.upper() or "run_id" in err


def test_approve_run_not_in_log(tmp_path, monkeypatch, capsys):
    bdir = _good_brand(tmp_path)
    monkeypatch.setenv("ADLOOPS_BRAND_DIR", str(bdir))
    _seed_audit_log_with_approval(bdir, run_id="RUN_X")
    rc = run_mod.main(["--approve", "OTHER_RUN:0"])
    assert rc == 9
    assert "APPROVAL rows" in capsys.readouterr().err


def test_approve_index_out_of_range(tmp_path, monkeypatch, capsys):
    bdir = _good_brand(tmp_path)
    monkeypatch.setenv("ADLOOPS_BRAND_DIR", str(bdir))
    _seed_audit_log_with_approval(bdir, run_id="RUN_X")
    rc = run_mod.main(["--approve", "RUN_X:5"])  # only one APPROVAL row
    assert rc == 9
    assert "out of range" in capsys.readouterr().err


def test_approve_recheck_still_approval_does_not_dispatch(tmp_path, monkeypatch, capsys):
    bdir = _good_brand(tmp_path)
    monkeypatch.setenv("ADLOOPS_BRAND_DIR", str(bdir))
    # Seed an "enable" approval — guardrails always returns APPROVAL for enable
    _seed_audit_log_with_approval(bdir, run_id="RUN_X", kind="enable",
                                  before={"status": "PAUSED"}, after={"status": "ENABLED"})

    called = {"flag": False}

    def must_not_call(*a, **kw):
        called["flag"] = True
        raise AssertionError("dispatch must not be called")

    monkeypatch.setattr(run_mod.executors, "dispatch", must_not_call)
    rc = run_mod.main(["--approve", "RUN_X:0"])
    assert rc == 10
    assert called["flag"] is False
    assert "approval" in capsys.readouterr().err.lower()


def test_approve_recheck_auto_dispatches(tmp_path, monkeypatch, capsys):
    bdir = _good_brand(tmp_path)
    monkeypatch.setenv("ADLOOPS_BRAND_DIR", str(bdir))
    # Seed a "pause" approval — pause is always AUTO via guardrails, so the
    # re-check will pass and dispatch should fire.
    _seed_audit_log_with_approval(bdir, run_id="RUN_X", kind="pause",
                                  before={"status": "ENABLED"}, after={"status": "PAUSED"})

    dispatched = []
    monkeypatch.setattr(run_mod.executors, "dispatch",
                        lambda m, dry_run: dispatched.append((m.campaign_id, dry_run)) or {"ok": True})

    rc = run_mod.main(["--approve", "RUN_X:0", "--dry-run"])
    assert rc == 0
    assert dispatched == [("B", True)]
    out = capsys.readouterr().out
    assert "Re-check passes" in out
