"""Google Ads mutation executor via the vendored kLOsk/adloop MCP.

The adloop MCP uses a strict two-step pattern: every write tool returns
a *preview* with a `plan_id`, and `confirm_and_apply(plan_id)` actually
executes. That matches our guardrail flow exactly — we run the preview,
log the plan, then apply only when guardrails say AUTO.

Use `GoogleExecutor()` as a context manager to keep one MCP session open
across multiple mutations per audit run (avoids paying the uv warmup +
OAuth refresh cost per call). For one-off use, call the module-level
`dispatch()` helper.

Tool surface used:
- `pause_entity(entity_type="campaign", entity_id=...)`
- `enable_entity(entity_type="campaign", entity_id=...)`
- `update_campaign(campaign_id, daily_budget=...)`
- `confirm_and_apply(plan_id, dry_run=...)`

The adloop MCP's `confirm_and_apply` defaults to `dry_run=True` — we
must explicitly pass `dry_run=False` for real mutations. AdLoop's
upstream config can force-override `dry_run=False` back to True for
test accounts (`safety.require_dry_run: true`); that's intentional and
preserved.
"""

from __future__ import annotations

from typing import Any

from .. import mcp_runner, paths
from ..guardrails import Mutation


class ExecutorError(RuntimeError):
    """The executor can't dispatch the mutation (unsupported kind, malformed
    preview response, etc.). Distinct from MCPToolError, which the MCP
    runner raises when the server itself rejects the call.
    """


def _adloop_spec() -> mcp_runner.ServerSpec:
    """Server spec for the vendored adloop MCP.

    `uv run adloop` runs the MCP in stdio mode (fastmcp's default). The cwd
    is the submodule directory so `uv` picks up the right pyproject.toml.
    """
    return mcp_runner.ServerSpec(
        command="uv",
        args=["run", "adloop"],
        cwd=str(paths.mcp_servers_dir() / "adloop"),
    )


class GoogleExecutor:
    """Holds an open MCP session to the adloop server.

    Use as a context manager. Tests can inject a pre-built session by
    passing it to the constructor, in which case the executor doesn't
    spawn or terminate any process.
    """

    def __init__(self, session: mcp_runner.MCPSession | None = None):
        self._session = session
        self._proc = None
        self._owns_proc = session is None

    def __enter__(self) -> "GoogleExecutor":
        if self._session is None:
            self._proc, self._session = mcp_runner.spawn(_adloop_spec())
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._owns_proc and self._proc is not None:
            mcp_runner._terminate(self._proc)
            self._proc = None

    def dispatch(self, mutation: Mutation, *, dry_run: bool = False) -> dict[str, Any]:
        """Run draft → confirm_and_apply for one mutation.

        Returns the apply result dict from confirm_and_apply. The dict
        will include `dry_run_forced_by` / `remediation` fields when the
        adloop server's `safety.require_dry_run` overrode our dry_run=False
        — callers should surface that to the operator without retrying.
        """
        if self._session is None:
            raise ExecutorError(
                "GoogleExecutor is not active. Use it as a context manager "
                "or pass a session to the constructor."
            )
        preview = self._draft(mutation)
        plan_id = _extract_plan_id(preview)
        return self._session.call_tool(
            "confirm_and_apply",
            {"plan_id": plan_id, "dry_run": dry_run},
        )

    def _draft(self, m: Mutation) -> Any:
        if m.kind == "pause":
            return self._session.call_tool("pause_entity", {
                "entity_type": "campaign",
                "entity_id": m.campaign_id,
            })
        if m.kind == "enable":
            return self._session.call_tool("enable_entity", {
                "entity_type": "campaign",
                "entity_id": m.campaign_id,
            })
        if m.kind == "budget_change":
            new_budget = float(m.after.get("daily_budget", 0))
            if new_budget <= 0:
                raise ExecutorError(
                    f"budget_change has non-positive after.daily_budget: {new_budget}"
                )
            return self._session.call_tool("update_campaign", {
                "campaign_id": m.campaign_id,
                "daily_budget": new_budget,
            })
        # create_campaign goes through draft_campaign — not yet in the
        # Phase 2 proposer, so it's not wired here. When we add it the
        # required-fields list (geo_target_ids, language_ids, etc.) will
        # need to come from brand.json.
        raise ExecutorError(f"Unsupported mutation kind for Google: {m.kind!r}")


def dispatch(mutation: Mutation, *, dry_run: bool = False) -> dict[str, Any]:
    """One-shot dispatch — spawns the MCP, runs the mutation, tears down.

    For batches use `GoogleExecutor()` as a context manager so one spawn
    serves many mutations.
    """
    with GoogleExecutor() as ex:
        return ex.dispatch(mutation, dry_run=dry_run)


def _extract_plan_id(preview: Any) -> str:
    if not isinstance(preview, dict):
        raise ExecutorError(f"Preview is not a JSON object: {preview!r}")
    plan_id = preview.get("plan_id")
    if not plan_id:
        raise ExecutorError(f"Preview missing plan_id: {preview!r}")
    return str(plan_id)
