"""LinkedIn Ads mutation executor via the vendored linkedin-ads-mcp.

The vendored MCP (`danielpopamd/linkedin-ads-mcp`, Node) exposes a single
partial-update tool, `update_campaign`, that handles pause, enable, and
budget changes via different fields in one payload. There is no
preview/confirm two-step (unlike Google's adloop MCP) — semantically
this executor is closer to Meta. The MCP transport is stdio, so we still
use `mcp_runner` to spawn it, and we keep a context manager so a batch
of LinkedIn mutations shares one spawn.

The LinkedIn audit stores `campaign_id` as the campaign URN (e.g.
``urn:li:sponsoredAdCampaign:12345``); the MCP tool wants the numeric ID.
Same for the ad account: env var `LINKEDIN_AD_ACCOUNT_URN` is the URN,
the MCP wants the numeric account ID. Both are extracted by trailing-
colon split.

The MCP reads OAuth tokens from disk (managed by ``node dist/auth-cli.js``)
and refreshes them using ``LINKEDIN_CLIENT_ID`` / ``LINKEDIN_CLIENT_SECRET``
from the inherited environment. This executor doesn't touch tokens.

Currency: ``update_campaign``'s MCP defaults ``dailyBudgetCurrency`` to
USD when omitted. We pass through ``mutation.after.get('currency')``
when set; otherwise the MCP default applies. Operators on non-USD
accounts should set ``currency`` in the mutation payload or extend the
proposer to read it from the audit.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import os
import urllib.error
import urllib.request
from typing import Any

from .. import mcp_runner, paths
from ..guardrails import Mutation


class ExecutorError(RuntimeError):
    """The executor can't dispatch the mutation — missing env, unsupported
    kind, malformed mutation. Distinct from MCPToolError, which the MCP
    runner raises when the server itself rejects the call.
    """


def _linkedin_spec() -> mcp_runner.ServerSpec:
    """Server spec for the vendored linkedin-ads MCP.

    ``node dist/index.js`` runs the MCP in stdio mode. cwd is the
    submodule directory so the token-store and any dotenv land in the
    right place.
    """
    return mcp_runner.ServerSpec(
        command="node",
        args=["dist/index.js"],
        cwd=str(paths.mcp_servers_dir() / "linkedin-ads"),
    )


def _account_id() -> str:
    urn = os.environ.get("LINKEDIN_AD_ACCOUNT_URN")
    if not urn:
        raise ExecutorError(
            "LINKEDIN_AD_ACCOUNT_URN not set. Export it as the sponsored "
            "account URN (e.g. urn:li:sponsoredAccount:1234567890)."
        )
    return urn.rsplit(":", 1)[-1]


def _campaign_id(mutation: Mutation) -> str:
    """Campaign IDs in the LinkedIn audit are URNs; the MCP wants numeric."""
    raw = mutation.campaign_id
    return raw.rsplit(":", 1)[-1] if ":" in raw else raw


class LinkedInExecutor:
    """Holds an open MCP session to the linkedin-ads server.

    Use as a context manager. Tests can inject a pre-built session by
    passing it to the constructor, in which case the executor doesn't
    spawn or terminate any process.
    """

    def __init__(self, session: mcp_runner.MCPSession | None = None):
        self._session = session
        self._proc = None
        self._owns_proc = session is None

    def __enter__(self) -> "LinkedInExecutor":
        if self._session is None:
            self._proc, self._session = mcp_runner.spawn(_linkedin_spec())
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._owns_proc and self._proc is not None:
            mcp_runner._terminate(self._proc)
            self._proc = None

    def dispatch(self, mutation: Mutation, *, dry_run: bool = False) -> dict[str, Any]:
        """Apply one Mutation via ``update_campaign``.

        In dry_run mode, returns the would-be tool args without invoking
        the MCP. The returned dict carries ``dry_run: True`` so the audit
        log can record what was planned.
        """
        if self._session is None:
            raise ExecutorError(
                "LinkedInExecutor is not active. Use it as a context manager "
                "or pass a session to the constructor."
            )
        args = _to_tool_args(mutation)
        if dry_run:
            return {
                "dry_run": True,
                "tool": "update_campaign",
                "arguments": args,
                "campaign_id": mutation.campaign_id,
            }
        return self._session.call_tool("update_campaign", args)


def dispatch(mutation: Mutation, *, dry_run: bool = False) -> dict[str, Any]:
    """Dispatch through the verified versioned LinkedIn REST API.

    The legacy MCP-backed ``LinkedInExecutor`` remains available for callers
    that explicitly use it, but normal AdLoops dispatch uses the same OAuth
    token path as the live reader. Every non-dry-run write reads the campaign
    first and verifies the exact resource after LinkedIn accepts the update.
    """
    token = os.environ.get("LINKEDIN_ACCESS_TOKEN")
    if not token:
        raise ExecutorError("LINKEDIN_ACCESS_TOKEN not set")
    account_urn = os.environ.get("LINKEDIN_AD_ACCOUNT_URN")
    if not account_urn:
        raise ExecutorError("LINKEDIN_AD_ACCOUNT_URN not set")
    account_id = account_urn.rsplit(":", 1)[-1]
    campaign_id = _campaign_id(mutation)
    if not account_id.isdigit() or not campaign_id.isdigit():
        raise ExecutorError("LinkedIn account and campaign IDs must have numeric URN tails")

    url = f"https://api.linkedin.com/rest/adAccounts/{account_id}/adCampaigns/{campaign_id}"
    changes = _rest_changes(mutation, dry_run=dry_run)
    body = {"patch": {"$set": changes}}
    if dry_run:
        return {
            "dry_run": True,
            "method": "POST",
            "url": url,
            "body": body,
            "campaign_id": mutation.campaign_id,
        }

    live = _get_campaign(url, token)
    _assert_expected_live_state(live, mutation, account_urn)
    if mutation.kind == "budget_change":
        currency = (live.get("dailyBudget") or {}).get("currencyCode")
        if not currency:
            raise ExecutorError("LinkedIn campaign has no dailyBudget currency")
        changes["dailyBudget"]["currencyCode"] = currency
    _partial_update(url, token, body)
    verified = _get_campaign(url, token)
    _assert_applied(verified, mutation, changes)
    return {"applied": True, "verified": True, "campaign_id": mutation.campaign_id}


API_VERSION = "202608"


def _rest_changes(mutation: Mutation, *, dry_run: bool) -> dict[str, Any]:
    if mutation.kind == "pause":
        return {"status": "PAUSED"}
    if mutation.kind == "enable":
        return {"status": "ACTIVE"}
    if mutation.kind != "budget_change":
        raise ExecutorError(f"Unsupported mutation kind for LinkedIn: {mutation.kind!r}")
    try:
        amount = Decimal(str(mutation.after.get("daily_budget")))
    except (InvalidOperation, ValueError) as e:
        raise ExecutorError("budget_change has invalid after.daily_budget") from e
    if amount <= 0:
        raise ExecutorError(f"budget_change has non-positive after.daily_budget: {amount}")
    currency = mutation.after.get("currency")
    if dry_run and not currency:
        raise ExecutorError("budget_change dry-run requires explicit after.currency")
    budget: dict[str, str] = {"amount": format(amount, "f")}
    if currency:
        budget["currencyCode"] = str(currency)
    return {"dailyBudget": budget}


def _headers(token: str, *, partial_update: bool = False) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Restli-Protocol-Version": "2.0.0",
        "Linkedin-Version": API_VERSION,
    }
    if partial_update:
        headers.update({"Content-Type": "application/json", "X-RestLi-Method": "PARTIAL_UPDATE"})
    return headers


def _get_campaign(url: str, token: str) -> dict[str, Any]:
    req = urllib.request.Request(url, headers=_headers(token), method="GET")
    return _request_json(req)


def _partial_update(url: str, token: str, body: dict[str, Any]) -> None:
    req = urllib.request.Request(
        url,
        data=json.dumps(body, separators=(",", ":")).encode(),
        headers=_headers(token, partial_update=True),
        method="POST",
    )
    _request_json(req)


def _request_json(req: urllib.request.Request) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")[:500]
        raise ExecutorError(f"LinkedIn API HTTP {e.code}: {text}") from e
    return json.loads(raw) if raw else {}


def _assert_expected_live_state(live: dict[str, Any], mutation: Mutation, account_urn: str) -> None:
    if live.get("account") != account_urn:
        raise ExecutorError("LinkedIn campaign belongs to a different ad account")
    if mutation.kind in {"pause", "enable"}:
        expected = mutation.before.get("status")
        if expected and live.get("status") != expected:
            raise ExecutorError(f"stale LinkedIn campaign status: expected {expected}, found {live.get('status')}")


def _assert_applied(live: dict[str, Any], mutation: Mutation, changes: dict[str, Any]) -> None:
    if mutation.kind in {"pause", "enable"}:
        target = changes["status"]
        if live.get("status") != target:
            raise ExecutorError(f"LinkedIn status verification failed: expected {target}, found {live.get('status')}")
        return
    expected = changes["dailyBudget"]
    actual = live.get("dailyBudget") or {}
    if actual.get("currencyCode") != expected.get("currencyCode") or str(actual.get("amount")) != expected.get("amount"):
        raise ExecutorError("LinkedIn daily budget verification failed")


def _to_tool_args(m: Mutation) -> dict[str, Any]:
    base = {"accountId": _account_id(), "campaignId": _campaign_id(m)}
    if m.kind == "pause":
        return {**base, "status": "PAUSED"}
    if m.kind == "enable":
        return {**base, "status": "ACTIVE"}
    if m.kind == "budget_change":
        new_budget = float(m.after.get("daily_budget", 0))
        if new_budget <= 0:
            raise ExecutorError(
                f"budget_change has non-positive after.daily_budget: {new_budget}"
            )
        args: dict[str, Any] = {
            **base,
            # LinkedIn API wants the amount as a string in account currency.
            "dailyBudgetAmount": f"{new_budget:.2f}",
        }
        currency = m.after.get("currency")
        if currency:
            args["dailyBudgetCurrency"] = currency
        return args
    raise ExecutorError(f"Unsupported mutation kind for LinkedIn: {m.kind!r}")
