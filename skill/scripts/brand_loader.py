"""Load and validate the brand.json file.

The brand file is the only source of truth for ICP, voice, and guardrails. The
audit cycle refuses to mutate any platform if this file is missing or
incomplete — fail-loud per spec.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import paths


class BrandConfigError(Exception):
    """Raised when brand.json is missing, malformed, or under-specified."""


# Defaults match the original module-constant values from mutations.py.
# Operators can override any subset of these via `guardrails.proposer`
# in brand.json; missing fields fall through to these.
DEFAULT_PAUSE_ZOMBIE_MIN_SPEND = 50.0
DEFAULT_DECREASE_CPA_SPIKE_PCT = 50.0
DEFAULT_INCREASE_CPA_DROP_PCT = -25.0
DEFAULT_MAX_PROPOSALS_PER_RUN = 10


@dataclass(frozen=True)
class ProposerThresholds:
    """Per-brand tuning knobs for the mutation proposer.

    Defaults are the original module constants — a brand.json with no
    `guardrails.proposer` block gets exactly the previous behaviour.
    """
    pause_zombie_min_spend: float = DEFAULT_PAUSE_ZOMBIE_MIN_SPEND
    decrease_cpa_spike_pct: float = DEFAULT_DECREASE_CPA_SPIKE_PCT
    increase_cpa_drop_pct: float = DEFAULT_INCREASE_CPA_DROP_PCT
    max_proposals_per_run: int = DEFAULT_MAX_PROPOSALS_PER_RUN


# Meta ODAX outcome objectives. Validated at load time so a typo fails loud
# in brand.json instead of erroring at dispatch. Update if Meta revises ODAX.
VALID_META_OBJECTIVES = frozenset({
    "OUTCOME_AWARENESS",
    "OUTCOME_TRAFFIC",
    "OUTCOME_ENGAGEMENT",
    "OUTCOME_LEADS",
    "OUTCOME_APP_PROMOTION",
    "OUTCOME_SALES",
})


@dataclass(frozen=True)
class DesiredCampaign:
    """A campaign the operator declares should exist, for the create_campaign
    reconciler. Only `meta` is supported today (the only executor that can
    create). `status` is intentionally absent — the proposer always forces
    PAUSED, which the guardrails require anyway.

    `ad_set`, when present, is a normalized (snake_case) Meta ad-set spec the
    executor creates under the new campaign so it launches as something
    populated rather than an empty shell. Also always PAUSED."""
    platform: str
    name: str
    objective: str
    daily_budget: float | None = None
    special_ad_categories: list[str] = field(default_factory=list)
    ad_set: dict[str, Any] | None = None


def _normalize_ad_set(a: dict[str, Any] | None) -> dict[str, Any] | None:
    """Map the camelCase brand.json ad-set block to the snake_case shape the
    mutation/executor layer uses. Only known fields pass through."""
    if a is None:
        return None
    out: dict[str, Any] = {
        "name": a["name"],
        "optimization_goal": a["optimizationGoal"],
        "billing_event": a["billingEvent"],
    }
    # Either explicit targeting or a marker to derive it from the brand's ICP
    # at propose time (resolved via Meta Targeting Search).
    if a.get("targetingFromIcp"):
        out["targeting_from_icp"] = True
    elif a.get("targeting") is not None:
        out["targeting"] = a["targeting"]
    if a.get("dailyBudget") is not None:
        out["daily_budget"] = float(a["dailyBudget"])
    if a.get("bidStrategy"):
        out["bid_strategy"] = a["bidStrategy"]
    if a.get("bidAmount") is not None:
        out["bid_amount"] = float(a["bidAmount"])
    if a.get("promotedObject"):
        out["promoted_object"] = a["promotedObject"]
    return out


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

    @property
    def thresholds(self) -> ProposerThresholds:
        p = self.raw["guardrails"].get("proposer") or {}
        return ProposerThresholds(
            pause_zombie_min_spend=float(
                p.get("pauseZombieMinSpend", DEFAULT_PAUSE_ZOMBIE_MIN_SPEND)
            ),
            decrease_cpa_spike_pct=float(
                p.get("decreaseCpaSpikePct", DEFAULT_DECREASE_CPA_SPIKE_PCT)
            ),
            increase_cpa_drop_pct=float(
                p.get("increaseCpaDropPct", DEFAULT_INCREASE_CPA_DROP_PCT)
            ),
            max_proposals_per_run=int(
                p.get("maxProposalsPerRun", DEFAULT_MAX_PROPOSALS_PER_RUN)
            ),
        )

    @property
    def desired_campaigns(self) -> list[DesiredCampaign]:
        """Campaigns declared for the create_campaign reconciler. Empty when
        `guardrails.proposer.desiredCampaigns` is absent — the default."""
        specs = (self.raw["guardrails"].get("proposer") or {}).get("desiredCampaigns") or []
        return [
            DesiredCampaign(
                platform=s["platform"],
                name=s["name"],
                objective=s["objective"],
                daily_budget=(float(s["dailyBudget"]) if s.get("dailyBudget") is not None else None),
                special_ad_categories=list(s.get("specialAdCategories", [])),
                ad_set=_normalize_ad_set(s.get("adSet")),
            )
            for s in specs
        ]


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

    prop = g.get("proposer")
    if prop is not None:
        if not isinstance(prop, dict):
            raise BrandConfigError("brand.json: guardrails.proposer must be an object")
        if "pauseZombieMinSpend" in prop:
            v = prop["pauseZombieMinSpend"]
            if not isinstance(v, (int, float)) or v < 0:
                raise BrandConfigError(
                    "brand.json: guardrails.proposer.pauseZombieMinSpend must be a non-negative number"
                )
        if "decreaseCpaSpikePct" in prop:
            v = prop["decreaseCpaSpikePct"]
            if not isinstance(v, (int, float)) or v < 0:
                raise BrandConfigError(
                    "brand.json: guardrails.proposer.decreaseCpaSpikePct must be a non-negative number "
                    "(percent CPA growth that triggers a budget cut)"
                )
        if "increaseCpaDropPct" in prop:
            v = prop["increaseCpaDropPct"]
            if not isinstance(v, (int, float)) or v > 0:
                raise BrandConfigError(
                    "brand.json: guardrails.proposer.increaseCpaDropPct must be a non-positive number "
                    "(e.g. -25 for a 25% CPA drop)"
                )
        if "maxProposalsPerRun" in prop:
            v = prop["maxProposalsPerRun"]
            if not isinstance(v, int) or v < 1:
                raise BrandConfigError(
                    "brand.json: guardrails.proposer.maxProposalsPerRun must be a positive integer"
                )
        if "desiredCampaigns" in prop:
            _validate_desired_campaigns(prop["desiredCampaigns"], raw["icp"])


def _validate_desired_campaigns(dc: Any, icp: dict) -> None:
    if not isinstance(dc, list):
        raise BrandConfigError("brand.json: guardrails.proposer.desiredCampaigns must be an array")
    for i, s in enumerate(dc):
        where = f"guardrails.proposer.desiredCampaigns[{i}]"
        if not isinstance(s, dict):
            raise BrandConfigError(f"brand.json: {where} must be an object")
        if s.get("platform") != "meta":
            raise BrandConfigError(
                f'brand.json: {where}.platform must be "meta" — the only platform '
                "whose executor can create campaigns today."
            )
        if not isinstance(s.get("name"), str) or not s["name"].strip():
            raise BrandConfigError(f"brand.json: {where}.name is required and must be a non-empty string")
        if not isinstance(s.get("objective"), str) or not s["objective"].strip():
            raise BrandConfigError(
                f"brand.json: {where}.objective is required and must be a non-empty string "
                "(a Meta campaign objective, e.g. OUTCOME_LEADS)"
            )
        if s["objective"] not in VALID_META_OBJECTIVES:
            raise BrandConfigError(
                f"brand.json: {where}.objective {s['objective']!r} is not a valid Meta "
                f"objective. Use one of: {', '.join(sorted(VALID_META_OBJECTIVES))}."
            )
        if s.get("dailyBudget") is not None:
            v = s["dailyBudget"]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
                raise BrandConfigError(f"brand.json: {where}.dailyBudget must be a positive number")
        if "specialAdCategories" in s:
            v = s["specialAdCategories"]
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                raise BrandConfigError(f"brand.json: {where}.specialAdCategories must be an array of strings")
        if "adSet" in s:
            _validate_ad_set(s["adSet"], f"{where}.adSet", icp)


def _validate_ad_set(a: Any, where: str, icp: dict) -> None:
    if not isinstance(a, dict):
        raise BrandConfigError(f"brand.json: {where} must be an object")
    for req in ("name", "optimizationGoal", "billingEvent"):
        if not isinstance(a.get(req), str) or not a[req].strip():
            raise BrandConfigError(f"brand.json: {where}.{req} is required and must be a non-empty string")
    has_explicit = isinstance(a.get("targeting"), dict) and bool(a["targeting"])
    derive = a.get("targetingFromIcp")
    if derive is not None and not isinstance(derive, bool):
        raise BrandConfigError(f"brand.json: {where}.targetingFromIcp must be a boolean")
    if bool(derive) == has_explicit:
        raise BrandConfigError(
            f"brand.json: {where} must set exactly one of `targeting` (a non-empty object, "
            'e.g. {"geo_locations": {"countries": ["US"]}}) or `targetingFromIcp: true`.'
        )
    if derive and not (icp.get("geo") or []):
        raise BrandConfigError(
            f"brand.json: {where}.targetingFromIcp needs at least one icp.geo entry — "
            "Meta requires a geo location and there's nothing to resolve one from."
        )
    if a.get("dailyBudget") is not None:
        v = a["dailyBudget"]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            raise BrandConfigError(f"brand.json: {where}.dailyBudget must be a positive number")
    if a.get("bidAmount") is not None:
        v = a["bidAmount"]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            raise BrandConfigError(f"brand.json: {where}.bidAmount must be a positive number")
    if "promotedObject" in a and not isinstance(a["promotedObject"], dict):
        raise BrandConfigError(f"brand.json: {where}.promotedObject must be an object")


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
