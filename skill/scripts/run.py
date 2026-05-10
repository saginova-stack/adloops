"""AdLoops entrypoint.

Usage:
  python -m scripts.run [--scaffold] [--dry-run] [--no-send]

Behavior:
1. If --scaffold: create the brand directory + placeholder brand.json, exit.
2. Otherwise: load brand.json (fail-loud if missing/incomplete — Telegram + nonzero exit).
3. Run the audit cycle for enabled platforms.
4. Write the new snapshot.
5. Render and send the Telegram report.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import traceback
from pathlib import Path

# Allow `python skill/scripts/run.py` (script-style) as well as
# `python -m scripts.run` (module-style) by re-rooting sys.path.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scripts import audit, brand_loader, paths, telegram_report  # type: ignore
    from scripts.brand_loader import BrandConfigError  # type: ignore
else:
    from . import audit, brand_loader, paths, telegram_report
    from .brand_loader import BrandConfigError


def _emit_failure_to_telegram(reason: str, *, dry_run: bool) -> None:
    """Best-effort: tell the user via Telegram that the run aborted, and why.
    Never raises — falls back to stderr if Telegram isn't configured."""
    try:
        telegram_report.send_messages([f"AdLoops run aborted:\n{reason}"], dry_run=dry_run)
    except Exception as e:
        print(f"[adloops] failed to send Telegram failure notice: {e}", file=sys.stderr)
        print(f"[adloops] original reason: {reason}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="adloops", description="Run an AdLoops audit cycle.")
    p.add_argument("--scaffold", action="store_true",
                   help="Create the brand directory + placeholder brand.json and exit.")
    p.add_argument("--dry-run", action="store_true",
                   help="Skip Telegram send; print the report to stdout.")
    p.add_argument("--no-send", action="store_true",
                   help="Same as --dry-run; alias.")
    args = p.parse_args(argv)
    dry = args.dry_run or args.no_send

    # 1) Scaffold mode
    if args.scaffold:
        bjson = brand_loader.ensure_scaffold()
        print(f"Scaffolded brand directory at {bjson.parent}")
        print(f"Edit {bjson} and replace the example values with your own.")
        return 0

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

    # 4) Snapshot — only persist if at least one platform actually fetched, so
    # a totally creds-less run doesn't poison the diff baseline.
    fetched_any = any(pr.fetched for pr in report.platforms)
    snapshot_path: Path | None = None
    if fetched_any:
        snapshot_path = audit.write_snapshot(report)

    # 5) Telegram
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
