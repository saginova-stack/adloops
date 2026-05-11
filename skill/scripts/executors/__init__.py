"""Platform mutation executors.

`executors.dispatch(mutation, dry_run=False)` routes a single Mutation
to its platform-specific executor (Google → adloop MCP, Meta → Graph
API). LinkedIn lands in Phase 3 once Marketing Developer Platform
approval comes through.

For batches of Google mutations, use `executors.google.GoogleExecutor()`
as a context manager so one MCP session serves multiple dispatches —
otherwise the dispatcher will spawn the MCP per call (uv warmup + auth
refresh each time, a few seconds of overhead per mutation).
"""

from __future__ import annotations

from typing import Any

from ..guardrails import Mutation
from . import google, meta


class UnsupportedPlatformError(ValueError):
    """Raised when a Mutation's platform has no executor wired."""


def dispatch(mutation: Mutation, *, dry_run: bool = False) -> dict[str, Any]:
    if mutation.platform == "google":
        return google.dispatch(mutation, dry_run=dry_run)
    if mutation.platform == "meta":
        return meta.dispatch(mutation, dry_run=dry_run)
    if mutation.platform == "linkedin":
        raise UnsupportedPlatformError(
            "LinkedIn mutations are Phase 3 — blocked on Marketing "
            "Developer Platform approval. See setup.md."
        )
    raise UnsupportedPlatformError(
        f"No executor for platform {mutation.platform!r}"
    )
