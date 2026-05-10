"""Resolved on-disk paths for the brand directory and skill resources.

The brand directory lives in Syncthing so it stays in sync between Michiel's
laptop and the OpenClaw server. We resolve it lazily and let callers fail loud
if it's missing — see brand_loader.load_or_raise().
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_BRAND_DIR = Path.home() / "Syncthing" / "adloops-brand"


def brand_dir() -> Path:
    return Path(os.environ.get("ADLOOPS_BRAND_DIR", str(DEFAULT_BRAND_DIR)))


def brand_json_path() -> Path:
    return brand_dir() / "brand.json"


def archive_dir() -> Path:
    return brand_dir() / "campaigns" / ".archive"


def audit_log_path() -> Path:
    return brand_dir() / "campaigns" / ".audit.jsonl"


def skill_root() -> Path:
    return Path(__file__).resolve().parent.parent


def references_dir() -> Path:
    return skill_root() / "references"


def mcp_servers_dir() -> Path:
    return skill_root() / "mcp-servers"
