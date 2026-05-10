from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.brand_loader import BrandConfigError, ensure_scaffold, load_or_raise


def _write(p: Path, raw: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(raw))


VALID = {
    "company": {"name": "Acme"},
    "icp": {"personas": ["Head of Growth"]},
    "valueProps": ["Saves time"],
    "brandVoice": {"tone": "direct"},
    "guardrails": {
        "maxDailyBudgetChangePct": 20,
        "platforms": {"google": True, "meta": True, "linkedin": False},
    },
}


def test_load_valid_brand(tmp_path: Path):
    p = tmp_path / "brand.json"
    _write(p, VALID)
    b = load_or_raise(p)
    assert b.company_name == "Acme"
    assert b.personas == ["Head of Growth"]
    assert b.max_daily_budget_change_pct == 20
    assert b.enabled_platforms == {"google", "meta"}


def test_missing_file_raises(tmp_path: Path):
    with pytest.raises(BrandConfigError, match="not found"):
        load_or_raise(tmp_path / "nope.json")


def test_invalid_json_raises(tmp_path: Path):
    p = tmp_path / "brand.json"
    p.write_text("{not json")
    with pytest.raises(BrandConfigError, match="not valid JSON"):
        load_or_raise(p)


def test_empty_personas_raises(tmp_path: Path):
    raw = {**VALID, "icp": {"personas": []}}
    p = tmp_path / "brand.json"
    _write(p, raw)
    with pytest.raises(BrandConfigError, match="personas is empty"):
        load_or_raise(p)


def test_missing_company_name(tmp_path: Path):
    raw = {**VALID, "company": {}}
    p = tmp_path / "brand.json"
    _write(p, raw)
    with pytest.raises(BrandConfigError, match="company.name"):
        load_or_raise(p)


def test_bad_pct_value(tmp_path: Path):
    raw = {**VALID, "guardrails": {**VALID["guardrails"], "maxDailyBudgetChangePct": 500}}
    p = tmp_path / "brand.json"
    _write(p, raw)
    with pytest.raises(BrandConfigError, match="maxDailyBudgetChangePct"):
        load_or_raise(p)


def test_missing_platform_keys(tmp_path: Path):
    raw = {**VALID, "guardrails": {**VALID["guardrails"], "platforms": {"google": True}}}
    p = tmp_path / "brand.json"
    _write(p, raw)
    with pytest.raises(BrandConfigError, match="platforms"):
        load_or_raise(p)


def test_ensure_scaffold_creates_layout(tmp_path: Path):
    bjson = ensure_scaffold(tmp_path)
    assert bjson.exists()
    assert (tmp_path / "assets" / "logo").is_dir()
    assert (tmp_path / "assets" / "linkedin").is_dir()
    assert (tmp_path / "assets" / "screenshots").is_dir()
    assert (tmp_path / "campaigns" / ".archive").is_dir()
    # scaffold uses the example file → loadable
    raw = json.loads(bjson.read_text())
    assert "company" in raw and raw["company"]["name"]


def test_ensure_scaffold_idempotent(tmp_path: Path):
    bjson = ensure_scaffold(tmp_path)
    bjson.write_text(json.dumps({**VALID, "company": {"name": "Edited"}}))
    bjson2 = ensure_scaffold(tmp_path)
    raw = json.loads(bjson2.read_text())
    assert raw["company"]["name"] == "Edited"
