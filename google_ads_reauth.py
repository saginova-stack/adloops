#!/usr/bin/env python3
"""Small helper for refreshing AdLoops Google Ads OAuth token.

Usage:
  source <openclaw snapshot with GOOGLE_ADS_CLIENT_ID/SECRET>
  python google_ads_reauth.py start
  python google_ads_reauth.py finish 'http://localhost:8080/?state=...&code=...&scope=...'
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PENDING = Path.home() / ".adloops" / "google_ads_oauth_pending.json"
SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/analytics.edit",
    "https://www.googleapis.com/auth/adwords",
    # Required by Data Manager API for Google Ads offline booking uploads.
    "https://www.googleapis.com/auth/datamanager",
]
REDIRECT_URI = "http://localhost:8080/"
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"


def required_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise SystemExit(f"Missing {name}; source the AdLoops shell snapshot first")
    return val


def latest_snapshot() -> Path:
    base = Path("/home/ubuntu/.openclaw/agents/main/agent/codex-home/shell_snapshots")
    files = sorted(base.glob("*.sh"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        raise SystemExit("No OpenClaw shell snapshots found")
    return files[0]


def start() -> None:
    client_id = required_env("GOOGLE_ADS_CLIENT_ID")
    state = secrets.token_urlsafe(24)
    PENDING.parent.mkdir(parents=True, exist_ok=True)
    PENDING.write_text(json.dumps({"state": state, "redirect_uri": REDIRECT_URI}, indent=2))
    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "include_granted_scopes": "true",
    }
    print(AUTH_URL + "?" + urllib.parse.urlencode(params))


def finish(redirect_response: str) -> None:
    client_id = required_env("GOOGLE_ADS_CLIENT_ID")
    client_secret = required_env("GOOGLE_ADS_CLIENT_SECRET")
    if not PENDING.exists():
        raise SystemExit(f"Missing pending auth file: {PENDING}; run start first")
    pending = json.loads(PENDING.read_text())
    parsed = urllib.parse.urlparse(redirect_response.strip())
    qs = urllib.parse.parse_qs(parsed.query)
    if "error" in qs:
        raise SystemExit(f"OAuth error: {qs.get('error')} {qs.get('error_description')}")
    code = qs.get("code", [""])[0]
    state = qs.get("state", [""])[0]
    if not code:
        # Allow pasting just the code as a fallback.
        code = redirect_response.strip()
    elif state != pending.get("state"):
        raise SystemExit("OAuth state mismatch; run start again and use the newest URL")

    data = urllib.parse.urlencode({
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": pending.get("redirect_uri", REDIRECT_URI),
        "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            token = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"Token exchange failed HTTP {exc.code}: {body}") from exc
    refresh = token.get("refresh_token")
    if not refresh:
        raise SystemExit(f"Token exchange succeeded but no refresh_token returned. Re-run start; ensure prompt=consent. Response keys: {sorted(token)}")

    snap = latest_snapshot()
    text = snap.read_text()
    lines = text.splitlines()
    out = []
    replaced = False
    for line in lines:
        if line.startswith("export GOOGLE_ADS_REFRESH_TOKEN="):
            out.append("export GOOGLE_ADS_REFRESH_TOKEN=" + repr(refresh))
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append("export GOOGLE_ADS_REFRESH_TOKEN=" + repr(refresh))
    snap.write_text("\n".join(out) + "\n")
    PENDING.unlink(missing_ok=True)
    print(f"Updated GOOGLE_ADS_REFRESH_TOKEN in {snap}")


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in {"start", "finish"}:
        print((__doc__ or "").strip())
        return 2
    if sys.argv[1] == "start":
        start()
    else:
        if len(sys.argv) < 3:
            raise SystemExit("Usage: google_ads_reauth.py finish '<full redirected URL>'")
        finish(sys.argv[2])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
