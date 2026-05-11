"""AdLoops entrypoint.

Usage:
  python -m scripts.run [--scaffold] [--dry-run] [--no-send] [--no-mutate]
  python -m scripts.run --approve <run_id>:<index>

Behavior:
1. If --scaffold: create the brand directory + placeholder brand.json, exit.
2. If --approve: look up the named APPROVAL row in the audit log, re-run
   guardrails against current brand config, execute if it's now AUTO. Used
   by the operator to approve a previously-queued mutation.
3. Otherwise (normal run):
   a. Load brand.json (fail-loud if missing/incomplete — Telegram + nonzero exit).
   b. Run the audit cycle for enabled platforms.
   c. Unless --no-mutate: propose mutations, run each through guardrails,
      dispatch AUTO via the platform executors, queue APPROVAL to the report,
      log REJECTED. --dry-run forces executors into preview mode.
   d. Write the new snapshot.
   e. Render and send the Telegram report (skipped if --dry-run; printed to stdout instead).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any

# Allow `python skill/scripts/run.py` (script-style) as well as
# `python -m scripts.run` (module-style) by re-rooting sys.path.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scripts import (  # type: ignore
        audit, brand_loader, executors, guardrails, mutations, paths, telegram_report,
    )
    from scripts.brand_loader import BrandConfigError  # type: ignore
    from scripts.guardrails import Decision  # type: ignore
else:
    from . import audit, brand_loader, executors, guardrails, mutations, paths, telegram_report
    from .brand_loader import BrandConfigError
    from .guardrails import Decision


def _emit_failure_to_telegram(reason: str, *, dry_run: bool) -> None:
    """Best-effort: tell the user via Telegram that the run aborted, and why.
    Never raises — falls back to stderr if Telegram isn't configured."""
    try:
        telegram_report.send_messages([f"AdLoops run aborted:\n{reason}"], dry_run=dry_run)
    except Exception as e:
        print(f"[adloops] failed to send Telegram failure notice: {e}", file=sys.stderr)
        print(f"[adloops] original reason: {reason}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Mutation pipeline (Phase 2)
# ---------------------------------------------------------------------------

def _run_mutations(report: audit.AuditReport, brand, *, dry_run: bool) -> None:
    """Propose → guardrails → dispatch. Mutates `report` in place to populate
    `actions_taken` and `pending_approvals`. Logs every decision to the audit log.
    """
    proposals = mutations.propose(report, brand)
    if not proposals:
        return

    cfg = guardrails.config_from_brand(brand)
    decisions: list[guardrails.GuardrailResult] = []
    for m in proposals:
        # For ceiling rule: project total spend assuming THIS mutation is applied
        # alongside all prior AUTO decisions. We use the simple per-mutation
        # projection here — sufficient because the cap-per-mutation rule
        # almost always fires before the cross-platform ceiling does.
        projected = mutations.projected_total_spend(report, [m])
        decisions.append(
            guardrails.check_mutation(m, cfg, projected_active_daily_spend=projected)
        )

    # Group AUTO Google mutations so we can reuse one MCP session.
    google_auto = [
        d for d in decisions
        if d.decision is Decision.AUTO and d.mutation.platform == "google"
    ]
    other_auto = [
        d for d in decisions
        if d.decision is Decision.AUTO and d.mutation.platform != "google"
    ]

    if google_auto:
        _dispatch_google_batch(google_auto, report, dry_run=dry_run)
    for d in other_auto:
        _dispatch_one(d, report, dry_run=dry_run)

    # Record non-AUTO decisions (APPROVAL → report surface; REJECTED → audit only).
    for d in decisions:
        if d.decision is Decision.AUTO:
            continue
        guardrails.log_decision(d, applied=False)
        if d.decision is Decision.APPROVAL:
            report.pending_approvals.append(guardrails.serialize_result(d))


def _dispatch_google_batch(
    decisions: list[guardrails.GuardrailResult],
    report: audit.AuditReport,
    *,
    dry_run: bool,
) -> None:
    """Run all AUTO Google mutations over one MCP session.

    If the MCP can't be spawned at all (e.g. uv missing, auth broken),
    every Google mutation in the batch is logged as failed and surfaced
    in the report — we don't want a single MCP-spawn failure to crash the
    whole run.
    """
    try:
        ex = executors.google.GoogleExecutor()
        with ex:
            for d in decisions:
                _safe_dispatch(d, report, dry_run=dry_run, ex=ex)
    except Exception as e:  # noqa: BLE001 — couldn't even open the session
        spawn_err = f"Google MCP could not be started: {type(e).__name__}: {e}"
        for d in decisions:
            guardrails.log_decision(d, applied=False)
            report.actions_taken.append({
                **guardrails.serialize_result(d),
                "applied": False,
                "error": spawn_err,
            })


def _dispatch_one(
    d: guardrails.GuardrailResult,
    report: audit.AuditReport,
    *,
    dry_run: bool,
) -> None:
    """Dispatch a single non-Google AUTO mutation."""
    try:
        result = executors.dispatch(d.mutation, dry_run=dry_run)
        guardrails.log_decision(d, applied=not dry_run)
        report.actions_taken.append({
            **guardrails.serialize_result(d),
            "applied": not dry_run,
            "dry_run": dry_run,
            "result": result,
        })
    except Exception as e:  # noqa: BLE001
        guardrails.log_decision(d, applied=False)
        report.actions_taken.append({
            **guardrails.serialize_result(d),
            "applied": False,
            "error": f"{type(e).__name__}: {e}",
        })


def _safe_dispatch(
    d: guardrails.GuardrailResult,
    report: audit.AuditReport,
    *,
    dry_run: bool,
    ex: executors.google.GoogleExecutor,
) -> None:
    """Dispatch one Google mutation via a shared GoogleExecutor."""
    try:
        result = ex.dispatch(d.mutation, dry_run=dry_run)
        guardrails.log_decision(d, applied=not dry_run)
        report.actions_taken.append({
            **guardrails.serialize_result(d),
            "applied": not dry_run,
            "dry_run": dry_run,
            "result": result,
        })
    except Exception as e:  # noqa: BLE001
        guardrails.log_decision(d, applied=False)
        report.actions_taken.append({
            **guardrails.serialize_result(d),
            "applied": False,
            "error": f"{type(e).__name__}: {e}",
        })


# ---------------------------------------------------------------------------
# --approve mode
# ---------------------------------------------------------------------------

def _run_approve(spec: str, *, dry_run: bool) -> int:
    """Replay an APPROVAL row from the audit log.

    `spec` is "<run_id>:<index>" — e.g. "20260512T090000Z:0" picks the first
    APPROVAL row from that run.

    We re-run `check_mutation()` against the current brand config so the
    operator gets fresh guardrail evaluation (a 30% change on Tuesday may
    only be 18% on Friday, in which case the rule should pass on its own).
    Only executes if the re-check returns AUTO; otherwise reports why
    and exits nonzero.
    """
    try:
        run_id, idx_s = spec.split(":", 1)
        idx = int(idx_s)
    except (ValueError, AttributeError):
        print(f"--approve expects <run_id>:<index>, got {spec!r}", file=sys.stderr)
        return 8

    try:
        brand = brand_loader.load_or_raise()
    except BrandConfigError as e:
        print(f"brand.json check failed: {e}", file=sys.stderr)
        return 2

    log = paths.audit_log_path()
    if not log.exists():
        print(f"Audit log not found at {log}", file=sys.stderr)
        return 9

    approval_rows: list[dict[str, Any]] = []
    for line in log.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("run_id") == run_id and entry.get("decision") == Decision.APPROVAL.value:
            approval_rows.append(entry)

    if not approval_rows:
        print(f"No APPROVAL rows found for run {run_id!r}", file=sys.stderr)
        return 9
    if idx < 0 or idx >= len(approval_rows):
        print(
            f"Index {idx} out of range — run {run_id} has {len(approval_rows)} APPROVAL rows",
            file=sys.stderr,
        )
        return 9

    row = approval_rows[idx]
    m = guardrails.Mutation(
        platform=row["platform"],
        campaign_id=row["campaign_id"],
        campaign_name=row["campaign_name"],
        kind=row["kind"],
        before=row.get("before", {}),
        after=row.get("after", {}),
        reason=row.get("reason", "") + f" (approved replay of {run_id}:{idx})",
    )
    cfg = guardrails.config_from_brand(brand)
    # Re-check without the cross-platform ceiling — operators using --approve
    # are explicitly opting in; ceiling enforcement requires the full audit
    # context which we don't reconstruct here.
    rechecked = guardrails.check_mutation(m, cfg)

    if rechecked.decision is not Decision.AUTO:
        print(
            f"Re-check is still {rechecked.decision.value}: {rechecked.explanation}",
            file=sys.stderr,
        )
        guardrails.log_decision(rechecked, applied=False)
        return 10

    print(f"Re-check passes ({rechecked.rule}). Dispatching {m.kind} for {m.campaign_name}...")
    try:
        result = executors.dispatch(m, dry_run=dry_run)
        guardrails.log_decision(rechecked, applied=not dry_run)
        print(json.dumps(result, indent=2, default=str))
        return 0
    except Exception as e:  # noqa: BLE001
        guardrails.log_decision(rechecked, applied=False)
        print(f"Dispatch failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 11


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="adloops", description="Run an AdLoops audit cycle.")
    p.add_argument("--scaffold", action="store_true",
                   help="Create the brand directory + placeholder brand.json and exit.")
    p.add_argument("--dry-run", action="store_true",
                   help="Skip Telegram send; pass dry_run=True to all executors so no real "
                        "mutations are made. Prints the rendered report to stdout.")
    p.add_argument("--no-send", action="store_true",
                   help="Same as --dry-run; alias.")
    p.add_argument("--no-mutate", action="store_true",
                   help="Skip the Phase 2 mutation step entirely (run audit + report only). "
                        "Use during ops trust-building if you want to see what would happen "
                        "without dispatching anything, even in dry_run mode.")
    p.add_argument("--approve", metavar="RUN_ID:INDEX",
                   help="Replay one APPROVAL row from a prior audit run.")
    args = p.parse_args(argv)
    dry = args.dry_run or args.no_send

    # 1) Scaffold mode
    if args.scaffold:
        bjson = brand_loader.ensure_scaffold()
        print(f"Scaffolded brand directory at {bjson.parent}")
        print(f"Edit {bjson} and replace the example values with your own.")
        return 0

    # 1b) Approve mode
    if args.approve:
        return _run_approve(args.approve, dry_run=dry)

    # 2) Load brand
    try:
        brand = brand_loader.load_or_raise()
    except BrandConfigError as e:
        msg = f"brand.json check failed: {e}"
        print(msg, file=sys.stderr)
        _emit_failure_to_telegram(msg, dry_run=dry)
        return 2

    enabled = brand.enabled_platforms
    if not enabled:
        msg = "No platforms are enabled in brand.json (guardrails.platforms). Aborting."
        print(msg, file=sys.stderr)
        _emit_failure_to_telegram(msg, dry_run=dry)
        return 3

    # 3) Audit
    os.environ["ADLOOPS_RUN_ID"] = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    try:
        report = audit.run_audit(enabled)
    except Exception as e:  # noqa: BLE001 — top-level catch so Telegram still gets a heads-up
        tb = traceback.format_exc()
        msg = f"Audit cycle crashed: {e}\n\n{tb[:1500]}"
        print(msg, file=sys.stderr)
        _emit_failure_to_telegram(msg, dry_run=dry)
        return 4

    # 4) Mutations (Phase 2)
    if not args.no_mutate:
        try:
            _run_mutations(report, brand, dry_run=dry)
        except Exception as e:  # noqa: BLE001 — never let mutations crash the run
            tb = traceback.format_exc()
            print(f"[adloops] mutation pipeline crashed: {e}\n{tb[:1500]}", file=sys.stderr)

    # 5) Snapshot — only persist if at least one platform actually fetched, so
    # a totally creds-less run doesn't poison the diff baseline.
    fetched_any = any(pr.fetched for pr in report.platforms)
    snapshot_path: Path | None = None
    if fetched_any:
        snapshot_path = audit.write_snapshot(report)

    # 6) Telegram
    chunks = telegram_report.render_report(report)
    try:
        telegram_report.send_messages(chunks, dry_run=dry)
    except telegram_report.TelegramConfigError as e:
        # Surface to stderr; the report is still printed below if dry.
        print(f"[adloops] {e}", file=sys.stderr)
        if not dry:
            return 5
    except Exception as e:  # noqa: BLE001
        print(f"[adloops] Telegram send failed: {e}", file=sys.stderr)
        return 6

    if dry:
        print("\n\n".join(chunks))

    if snapshot_path:
        print(f"snapshot: {snapshot_path}")

    # If every enabled platform errored, exit nonzero so the cron operator sees red.
    if not fetched_any and enabled:
        return 7
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
