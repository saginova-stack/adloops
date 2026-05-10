"""Load and validate the brand.json file.

The brand file is the only source of truth for ICP, voice, and guardrails. The
audit cycle refuses to mutate any platform if this file is missing or
incomplete — fail-loud per spec.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import paths


class BrandConfigError(Exception):
    """Raised when brand.json is missing, malformed, or under-specified."""


@dataclass(frozen=True)
class Brand:
    raw: dict[str, Any]

    @property
    def company_name(self) -> str:
        return self.raw["company"]["name"]

    @property
    def personas(self) -> list[str]:
        return list(self.raw["icp"].get("personas", []))

    @property
    def max_daily_budget_change_pct(self) -> float:
        return float(self.raw["guardrails"]["maxDailyBudgetChangePct"])

    @property
    def new_campaign_requires_approval(self) -> bool:
        return bool(self.raw["guardrails"].get("newCampaignRequiresApproval", False))

    @property
    def never_increase_above(self) -> float | None:
        v = self.raw["guardrails"].get("neverIncreaseBudgetAbove")
        return None if v is None else float(v)

    @property
    def enabled_platforms(self) -> set[str]:
        p = self.raw["guardrails"]["platforms"]
        return {k for k, v in p.items() if v}


REQUIRED_TOP_LEVEL = ("company", "icp", "valueProps", "brandVoice", "guardrails")


def _scaffold_default(target: Path, example_path: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(example_path, target)


def ensure_scaffold(brand_dir: Path | None = None) -> Path:
    """Create the brand directory + placeholder brand.json if missing.

    Returns the path to brand.json. The placeholder is the example file with
    a comment-style note that personas must be filled in. Callers that want
    fail-loud behavior should call load_or_raise() instead — that one does
    *not* scaffold; this is for first-time setup.
    """
    bdir = brand_dir or paths.brand_dir()
    bjson = bdir / "brand.json"
    (bdir / "assets" / "logo").mkdir(parents=True, exist_ok=True)
    (bdir / "assets" / "linkedin").mkdir(parents=True, exist_ok=True)
    (bdir / "assets" / "screenshots").mkdir(parents=True, exist_ok=True)
    (bdir / "campaigns" / ".archive").mkdir(parents=True, exist_ok=True)

    if not bjson.exists():
        example = paths.references_dir() / "brand.example.json"
        _scaffold_default(bjson, example)
    return bjson


def _validate(raw: Any) -> None:
    if not isinstance(raw, dict):
        raise BrandConfigError("brand.json must be a JSON object at the top level")

    missing = [k for k in REQUIRED_TOP_LEVEL if k not in raw]
    if missing:
        raise BrandConfigError(f"brand.json missing required keys: {missing}")

    if not isinstance(raw["company"], dict) or not raw["company"].get("name"):
        raise BrandConfigError("brand.json: company.name is required and must be non-empty")

    icp = raw["icp"]
    if not isinstance(icp, dict):
        raise BrandConfigError("brand.json: icp must be an object")
    personas = icp.get("personas") or []
    if not isinstance(personas, list) or len(personas) == 0:
        raise BrandConfigError(
            "brand.json: icp.personas is empty. Fill in at least one persona before "
            "AdLoops will run an audit. See references/brand.example.json for the shape."
        )

    g = raw["guardrails"]
    if not isinstance(g, dict):
        raise BrandConfigError("brand.json: guardrails must be an object")
    if "maxDailyBudgetChangePct" not in g:
        raise BrandConfigError("brand.json: guardrails.maxDailyBudgetChangePct is required")
    pct = g["maxDailyBudgetChangePct"]
    if not isinstance(pct, (int, float)) or pct < 0 or pct > 100:
        raise BrandConfigError(
            "brand.json: guardrails.maxDailyBudgetChangePct must be a number in [0, 100]"
        )

    p = g.get("platforms")
    if not isinstance(p, dict) or not all(k in p for k in ("google", "meta", "linkedin")):
        raise BrandConfigError(
            "brand.json: guardrails.platforms must include google, meta, linkedin booleans"
        )


def load_or_raise(brand_json: Path | None = None) -> Brand:
    """Load brand.json. Raises BrandConfigError on any problem.

    The caller is responsible for catching and reporting (e.g. via Telegram)
    so the user gets a loud signal that the audit refused to run.
    """
    bjson = brand_json or paths.brand_json_path()
    if not bjson.exists():
        raise BrandConfigError(
            f"brand.json not found at {bjson}. Run `python -m scripts.run --scaffold` "
            f"or copy references/brand.example.json into place and edit it."
        )

    try:
        raw = json.loads(bjson.read_text())
    except json.JSONDecodeError as e:
        raise BrandConfigError(f"brand.json is not valid JSON: {e}") from e

    _validate(raw)
    return Brand(raw=raw)
