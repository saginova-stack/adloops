#!/usr/bin/env bash
set -euo pipefail

# Scan Google Calendar appointment bookings and upload new bookings to the
# Google Ads "Calendar booking - offline upload" conversion action via the
# Data Manager API. Safe to run frequently: uploaded calendar event IDs are
# deduped in $ADLOOPS_BRAND_DIR/conversions/calendar-booking-uploads.json.

SNAPSHOT="$(python3 - <<'PY'
from pathlib import Path
base = Path('/home/ubuntu/.openclaw/agents/main/agent/codex-home/shell_snapshots')
files = sorted(base.glob('*.sh'), key=lambda p: p.stat().st_mtime, reverse=True)
if not files:
    raise SystemExit('no OpenClaw shell snapshots found')
print(files[0])
PY
)"

# shellcheck disable=SC1090
source "$SNAPSHOT"

export ADLOOPS_BRAND_DIR="${ADLOOPS_BRAND_DIR:-/home/ubuntu/Syncthing/adloops-brand}"
export ADLOOPS_CALENDAR_BOOKING_QUERY="${ADLOOPS_CALENDAR_BOOKING_QUERY:-AI Numbers Game Demo}"
export ADLOOPS_BOOKING_CONVERSION_ACTION="${ADLOOPS_BOOKING_CONVERSION_ACTION:-Calendar booking - offline upload}"
export ADLOOPS_BOOKING_CONVERSION_ACTION_ID="${ADLOOPS_BOOKING_CONVERSION_ACTION_ID:-7691226922}"

cd /home/ubuntu/adloops/skill
exec ../.venv/bin/python -m scripts.calendar_booking_upload "$@"
