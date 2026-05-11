"""Meta Ads mutation executor via the Marketing Graph API.

No public MCP shim exists for Meta yet (the official Meta Ads CLI is
CLI-only as of 2026-04). We hit the Marketing Graph API directly using
the same env contract (`META_ACCESS_TOKEN`, `META_AD_ACCOUNT_ID`) that
the read client uses, and require `ads_management` scope on the token.

Unlike the adloop MCP, the Graph API has no native preview/confirm
two-step — we compute the diff client-side, log it (caller's
responsibility via guardrails.log_decision), then POST. Dry-run mode
returns the would-be HTTP call instead of executing it.

Mutation kinds:
  - pause          → POST /{campaign_id} {"status": "PAUSED"}
  - enable         → POST /{campaign_id} {"status": "ACTIVE"}
  - budget_change  → POST /{campaign_id} {"daily_budget": <cents>}
  - create_campaign → not yet supported (Phase 2 proposer doesn't emit it;
                      add when the proposer does)
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ..guardrails import Mutation


GRAPH_VERSION = "v22.0"


class ExecutorError(RuntimeError):
    """Raised when a Meta mutation cannot be dispatched — missing creds,
    unsupported kind, malformed mutation, or non-2xx response from the
    Graph API."""


def dispatch(mutation: Mutation, *, dry_run: bool = False) -> dict[str, Any]:
    """Apply a Mutation via the Marketing Graph API.

    Returns the parsed response body. In dry_run mode returns the
    would-be request shape — same dict shape `{"dry_run": True, ...}`
    we want the audit log to capture.
    """
    token = os.environ.get("META_ACCESS_TOKEN")
    if not token:
        raise ExecutorError(
            "META_ACCESS_TOKEN not set. Apply for a Marketing API system-user "
            "token with ads_management scope and export it before running."
        )
    method, path, body = _to_http(mutation)
    if dry_run:
        return {
            "dry_run": True,
            "method": method,
            "path": path,
            "body": body,
            "campaign_id": mutation.campaign_id,
        }
    return _http(method, path, body, token)


def _to_http(m: Mutation) -> tuple[str, str, dict[str, Any]]:
    if m.kind == "pause":
        return "POST", f"/{m.campaign_id}", {"status": "PAUSED"}
    if m.kind == "enable":
        return "POST", f"/{m.campaign_id}", {"status": "ACTIVE"}
    if m.kind == "budget_change":
        new_budget = float(m.after.get("daily_budget", 0))
        if new_budget <= 0:
            raise ExecutorError(
                f"budget_change has non-positive after.daily_budget: {new_budget}"
            )
        # Meta wants daily_budget in account-currency cents, as a string.
        return "POST", f"/{m.campaign_id}", {
            "daily_budget": str(int(round(new_budget * 100))),
        }
    raise ExecutorError(f"Unsupported mutation kind for Meta: {m.kind!r}")


def _http(method: str, path: str, body: dict[str, Any], token: str) -> dict[str, Any]:
    url = f"https://graph.facebook.com/{GRAPH_VERSION}{path}"
    data = urllib.parse.urlencode({**body, "access_token": token}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            try:
                return json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return {"raw": raw}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise ExecutorError(
            f"Meta Graph API HTTP {e.code} for {method} {url}: {body}"
        ) from e
    except urllib.error.URLError as e:
        raise ExecutorError(f"Meta Graph API transport error: {e.reason}") from e
