"""Platform mutation executors.

`executors.dispatch(mutation, dry_run=False)` routes a single Mutation
to its platform-specific executor (Google → adloop MCP, Meta → Graph
API, LinkedIn → linkedin-ads MCP).

For batches, use the platform's context-manager executor so one MCP
spawn / token refresh serves multiple dispatches —
`executors.google.GoogleExecutor()` and
`executors.linkedin.LinkedInExecutor()`. The Meta executor talks to the
Graph API directly so there's no batching cost there.

LinkedIn dispatch additionally requires Marketing Developer Platform
approval on the LinkedIn app before live calls succeed; the executor
itself doesn't enforce that — the MCP will return an auth error from
the LinkedIn API if the token isn't authorised. Run
``node dist/auth-cli.js`` once before relying on this.
"""

from __future__ import annotations

from typing import Any

from ..guardrails import Mutation
from . import google, linkedin, meta


class UnsupportedPlatformError(ValueError):
    """Raised when a Mutation's platform has no executor wired."""


def dispatch(mutation: Mutation, *, dry_run: bool = False) -> dict[str, Any]:
    if mutation.platform == "google":
        return google.dispatch(mutation, dry_run=dry_run)
    if mutation.platform == "meta":
        return meta.dispatch(mutation, dry_run=dry_run)
    if mutation.platform == "linkedin":
        return linkedin.dispatch(mutation, dry_run=dry_run)
    raise UnsupportedPlatformError(
        f"No executor for platform {mutation.platform!r}"
    )
