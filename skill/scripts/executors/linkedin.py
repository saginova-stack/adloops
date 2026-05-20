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

import os
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
    """One-shot dispatch — spawns the MCP, runs the mutation, tears down.

    For batches use ``LinkedInExecutor()`` as a context manager so one
    spawn serves many mutations.
    """
    with LinkedInExecutor() as ex:
        return ex.dispatch(mutation, dry_run=dry_run)


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
