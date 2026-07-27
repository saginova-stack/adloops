"""Meta Ads mutation executor via the Marketing Graph API.

No public MCP shim exists for Meta yet (the official Meta Ads CLI is
CLI-only as of 2026-04). We hit the Marketing Graph API directly using
the same env contract (`META_ACCESS_TOKEN`, `META_AD_ACCOUNT_ID`) that
the read client uses, and require `ads_management` scope on the token.

Unlike the adloop MCP, the Graph API has no native preview/confirm
two-step — we compute the diff client-side, log it (caller's
responsibility via guardrails.log_decision), then POST. Dry-run mode
returns the would-be HTTP call(s) instead of executing them.

Mutation kinds:
  - pause          → POST /{campaign_id} {"status": "PAUSED"}
  - enable         → POST /{campaign_id} {"status": "ACTIVE"}
  - budget_change  → scale whichever budget actually governs the campaign
                     by the intended ratio (after/before). Meta only keeps
                     a budget on the campaign when Campaign Budget
                     Optimization (Advantage Campaign Budget) is on; on the
                     common non-CBO setup the budget lives on each ad set.
                     We GET the live budget location and scale it — one
                     code path covers CBO daily, CBO lifetime, and per-ad-set
                     daily/lifetime, and multi-ad-set campaigns keep their
                     relative allocation because every ad set gets the same
                     ratio.
  - create_campaign → POST /act_<account>/campaigns. Guardrails force
                     status=PAUSED before this executor ever sees it.

`budget_change` carries an absolute `after.daily_budget` (computed by the
proposer from the effective daily budget the read client reported), but we
apply it as a *ratio* against the live budget rather than writing the
absolute number: that's what lets one mutation fan out across N ad sets,
and it re-reads the live budget so a manual change between audit and
dispatch doesn't get clobbered with a stale figure.
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
    unsupported kind, malformed mutation, un-locatable budget, or a non-2xx
    response from the Graph API."""


def dispatch(mutation: Mutation, *, dry_run: bool = False) -> dict[str, Any]:
    """Apply a Mutation via the Marketing Graph API.

    Returns the parsed response body. In dry_run mode returns the would-be
    request shape (never POSTs; `budget_change` still GETs to resolve where
    the budget lives so the plan is accurate).
    """
    token = os.environ.get("META_ACCESS_TOKEN")
    if not token:
        raise ExecutorError(
            "META_ACCESS_TOKEN not set. Apply for a Marketing API system-user "
            "token with ads_management scope and export it before running."
        )

    if mutation.kind == "budget_change":
        return _dispatch_budget_change(mutation, token, dry_run=dry_run)
    if mutation.kind == "create_campaign":
        return _dispatch_create_campaign(mutation, token, dry_run=dry_run)

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
    """Build the single request for a pause/enable mutation.

    Pause/enable act on the campaign object — Meta cascades campaign status
    to every child ad set, so this is correct for CBO and non-CBO alike.
    """
    if m.kind == "pause":
        return "POST", f"/{m.campaign_id}", {"status": "PAUSED"}
    if m.kind == "enable":
        return "POST", f"/{m.campaign_id}", {"status": "ACTIVE"}
    raise ExecutorError(f"Unsupported mutation kind for Meta: {m.kind!r}")


def _account_id() -> str:
    account = os.environ.get("META_AD_ACCOUNT_ID")
    if not account:
        raise ExecutorError(
            "create_campaign needs META_AD_ACCOUNT_ID (the act_* form) to know "
            "which ad account to create under."
        )
    return account


def _dispatch_create_campaign(m: Mutation, token: str, *, dry_run: bool) -> dict[str, Any]:
    """Create the campaign, then (if declared) scaffold a default ad set under it.

    The ad set needs the campaign id, which only exists after the create
    returns, so this can't be two independent mutations — it's one chained
    dispatch. Both objects launch PAUSED.
    """
    method, path, body = _create_campaign_request(m)
    ad_set_cfg = m.after.get("ad_set")
    if dry_run:
        plan: dict[str, Any] = {
            "dry_run": True, "method": method, "path": path, "body": body,
            "campaign_id": "",
        }
        if ad_set_cfg:
            plan["ad_set"] = {
                "method": "POST",
                "path": f"/{_account_id()}/adsets",
                "body": _ad_set_body(ad_set_cfg, campaign_id="<new-campaign-id>"),
            }
        return plan
    campaign = _http(method, path, body, token)
    if not ad_set_cfg:
        return campaign
    new_id = campaign.get("id")
    if not new_id:
        raise ExecutorError(
            f"Campaign created but the Graph API returned no id ({campaign!r}); "
            "cannot scaffold the ad set."
        )
    ad_set = _http(
        "POST", f"/{_account_id()}/adsets",
        _ad_set_body(ad_set_cfg, campaign_id=str(new_id)), token,
    )
    return {"campaign": campaign, "ad_set": ad_set}


def _ad_set_body(cfg: dict[str, Any], *, campaign_id: str) -> dict[str, Any]:
    """Build the /adsets POST body from a normalized (snake_case) ad-set spec.

    Forces status=PAUSED and injects the parent campaign id. Budgets convert to
    minor units; targeting/promoted_object are JSON-encoded as the Graph API
    expects."""
    body: dict[str, Any] = {
        "name": cfg["name"],
        "campaign_id": campaign_id,
        "optimization_goal": cfg["optimization_goal"],
        "billing_event": cfg["billing_event"],
        "status": "PAUSED",
        "targeting": json.dumps(cfg["targeting"]),
    }
    if cfg.get("daily_budget") is not None:
        body["daily_budget"] = str(_to_cents(cfg["daily_budget"]))
    if cfg.get("bid_strategy"):
        body["bid_strategy"] = cfg["bid_strategy"]
    if cfg.get("bid_amount") is not None:
        body["bid_amount"] = str(_to_cents(cfg["bid_amount"]))
    if cfg.get("promoted_object"):
        body["promoted_object"] = json.dumps(cfg["promoted_object"])
    return body


def _create_campaign_request(m: Mutation) -> tuple[str, str, dict[str, Any]]:
    account = _account_id()
    name = m.after.get("name")
    objective = m.after.get("objective")
    if not name or not objective:
        raise ExecutorError(
            "create_campaign requires after.name and after.objective "
            f"(got name={name!r}, objective={objective!r})."
        )
    # Guardrails already reject anything that isn't PAUSED; default defensively.
    status = str(m.after.get("status") or "PAUSED").upper()
    body: dict[str, Any] = {
        "name": name,
        "objective": objective,
        "status": status,
        # Required by the Graph API on every campaign create; [] = none.
        "special_ad_categories": json.dumps(m.after.get("special_ad_categories", [])),
    }
    if m.after.get("daily_budget"):
        body["daily_budget"] = str(_to_cents(m.after["daily_budget"]))
    if m.after.get("lifetime_budget"):
        body["lifetime_budget"] = str(_to_cents(m.after["lifetime_budget"]))
    return "POST", f"/{account}/campaigns", body


def _dispatch_budget_change(m: Mutation, token: str, *, dry_run: bool) -> dict[str, Any]:
    before = float(m.before.get("daily_budget", 0))
    after = float(m.after.get("daily_budget", 0))
    if after <= 0:
        raise ExecutorError(
            f"budget_change has non-positive after.daily_budget: {after}"
        )
    if before <= 0:
        raise ExecutorError(
            "budget_change needs a positive before.daily_budget to scale from; "
            f"got {before}. (Meta budgets are scaled by ratio, not overwritten.)"
        )
    ratio = after / before
    requests = _resolve_budget_requests(m.campaign_id, ratio, token)
    if dry_run:
        return {
            "dry_run": True,
            "campaign_id": m.campaign_id,
            "requests": [
                {"method": method, "path": path, "body": body}
                for method, path, body in requests
            ],
        }
    results = [_http(method, path, body, token) for method, path, body in requests]
    return {"campaign_id": m.campaign_id, "results": results}


def _resolve_budget_requests(
    campaign_id: str, ratio: float, token: str
) -> list[tuple[str, str, dict[str, Any]]]:
    """Find where the campaign's budget lives and build one scaled POST per target.

    Order mirrors Meta's own precedence: a campaign-level budget (CBO) wins;
    otherwise the budget is on the ad sets (non-CBO) and every budgeted ad set
    is scaled by the same ratio.
    """
    campaign = _get(f"/{campaign_id}", {"fields": "daily_budget,lifetime_budget"}, token)
    for field in ("daily_budget", "lifetime_budget"):
        if campaign.get(field):
            return [_scale_request(f"/{campaign_id}", field, campaign[field], ratio)]

    adsets = _get(
        f"/{campaign_id}/adsets",
        {"fields": "id,daily_budget,lifetime_budget", "limit": "500"},
        token,
    ).get("data", [])
    requests: list[tuple[str, str, dict[str, Any]]] = []
    for a in adsets:
        for field in ("daily_budget", "lifetime_budget"):
            if a.get(field):
                requests.append(_scale_request(f"/{a['id']}", field, a[field], ratio))
                break
    if not requests:
        raise ExecutorError(
            f"Could not locate a daily or lifetime budget on campaign {campaign_id} "
            "or any of its ad sets; nothing to change."
        )
    return requests


def _scale_request(
    path: str, field: str, current_minor: Any, ratio: float
) -> tuple[str, str, dict[str, Any]]:
    """Scale a budget already expressed in Meta's minor units (cents) by ratio."""
    new_minor = int(round(int(current_minor) * ratio))
    if new_minor <= 0:
        raise ExecutorError(
            f"Scaled budget for {path} is non-positive ({new_minor}); refusing."
        )
    return "POST", path, {field: str(new_minor)}


def _to_cents(major: Any) -> int:
    """Account-currency major units → Meta minor units (cents)."""
    return int(round(float(major) * 100))


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


def _get(path: str, params: dict[str, str], token: str) -> dict[str, Any]:
    """GET a Graph API node/edge and return the parsed JSON object.

    Read-only — used to resolve where a campaign's budget lives before a
    budget_change. Runs in dry_run too (resolving the plan needs the live
    budget shape); only the mutating POSTs are suppressed under dry_run.
    """
    url = f"https://graph.facebook.com/{GRAPH_VERSION}{path}"
    query = urllib.parse.urlencode({**params, "access_token": token})
    req = urllib.request.Request(f"{url}?{query}", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise ExecutorError(
            f"Meta Graph API HTTP {e.code} for GET {url}: {body}"
        ) from e
    except urllib.error.URLError as e:
        raise ExecutorError(f"Meta Graph API transport error: {e.reason}") from e
