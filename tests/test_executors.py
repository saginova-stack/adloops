from __future__ import annotations

import json

import pytest

from scripts.executors import (
    UnsupportedPlatformError,
    dispatch,
    google,
    meta,
)
from scripts.executors.google import ExecutorError as GoogleExecutorError
from scripts.executors.google import GoogleExecutor
from scripts.executors.meta import ExecutorError as MetaExecutorError
from scripts.guardrails import Mutation


# ---- shared fakes --------------------------------------------------------

class FakeSession:
    """Records tool calls and returns canned responses keyed by tool name."""

    def __init__(self, responses: dict | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.responses = responses or {}

    def call_tool(self, name, arguments=None):
        self.calls.append((name, dict(arguments or {})))
        if name in self.responses:
            r = self.responses[name]
            return r(arguments) if callable(r) else r
        raise AssertionError(f"FakeSession received unexpected call: {name}")


# ---- Google executor ----------------------------------------------------

def test_google_pause_drafts_then_confirms():
    sess = FakeSession({
        "pause_entity": {"plan_id": "P1", "preview": "..."},
        "confirm_and_apply": {"status": "APPLIED", "plan_id": "P1"},
    })
    m = Mutation(platform="google", campaign_id="42", campaign_name="Brand",
                 kind="pause",
                 before={"status": "ENABLED"}, after={"status": "PAUSED"})
    with GoogleExecutor(session=sess) as ex:
        result = ex.dispatch(m, dry_run=False)
    assert result["status"] == "APPLIED"
    # confirm we drafted with the right tool and confirmed with the right plan_id
    assert sess.calls[0] == ("pause_entity", {"entity_type": "campaign", "entity_id": "42"})
    assert sess.calls[1] == ("confirm_and_apply", {"plan_id": "P1", "dry_run": False})


def test_google_enable_uses_enable_entity():
    sess = FakeSession({
        "enable_entity": {"plan_id": "P9"},
        "confirm_and_apply": {"status": "APPLIED"},
    })
    m = Mutation(platform="google", campaign_id="42", campaign_name="x",
                 kind="enable",
                 before={"status": "PAUSED"}, after={"status": "ENABLED"})
    with GoogleExecutor(session=sess) as ex:
        ex.dispatch(m, dry_run=False)
    assert sess.calls[0][0] == "enable_entity"


def test_google_budget_change_passes_new_budget():
    sess = FakeSession({
        "update_campaign": {"plan_id": "BUDGET_PLAN"},
        "confirm_and_apply": {"status": "APPLIED"},
    })
    m = Mutation(platform="google", campaign_id="42", campaign_name="x",
                 kind="budget_change",
                 before={"daily_budget": 100.0}, after={"daily_budget": 80.0})
    with GoogleExecutor(session=sess) as ex:
        ex.dispatch(m, dry_run=False)
    args = sess.calls[0][1]
    assert args == {"campaign_id": "42", "daily_budget": 80.0}
    confirm_args = sess.calls[1][1]
    assert confirm_args == {"plan_id": "BUDGET_PLAN", "dry_run": False}


def test_google_budget_change_rejects_non_positive():
    sess = FakeSession({})
    m = Mutation(platform="google", campaign_id="42", campaign_name="x",
                 kind="budget_change",
                 before={"daily_budget": 100.0}, after={"daily_budget": 0.0})
    with GoogleExecutor(session=sess) as ex, pytest.raises(GoogleExecutorError, match="non-positive"):
        ex.dispatch(m, dry_run=False)
    assert sess.calls == []  # never sent anything


def test_google_dry_run_propagates_to_confirm():
    sess = FakeSession({
        "pause_entity": {"plan_id": "X"},
        "confirm_and_apply": {"status": "DRY_RUN_SUCCESS"},
    })
    m = Mutation(platform="google", campaign_id="42", campaign_name="x",
                 kind="pause", before={"status": "ENABLED"}, after={"status": "PAUSED"})
    with GoogleExecutor(session=sess) as ex:
        ex.dispatch(m, dry_run=True)
    assert sess.calls[1] == ("confirm_and_apply", {"plan_id": "X", "dry_run": True})


def test_google_unsupported_kind_raises():
    sess = FakeSession({})
    m = Mutation(platform="google", campaign_id="42", campaign_name="x",
                 kind="something_weird")
    with GoogleExecutor(session=sess) as ex, pytest.raises(GoogleExecutorError, match="Unsupported"):
        ex.dispatch(m, dry_run=False)


def test_google_preview_missing_plan_id_raises():
    sess = FakeSession({
        "pause_entity": {"status": "ok"},  # no plan_id!
    })
    m = Mutation(platform="google", campaign_id="42", campaign_name="x",
                 kind="pause", before={"status": "ENABLED"}, after={"status": "PAUSED"})
    with GoogleExecutor(session=sess) as ex, pytest.raises(GoogleExecutorError, match="plan_id"):
        ex.dispatch(m, dry_run=False)


def test_google_dispatch_requires_active_session():
    # Constructing without a session and dispatching without entering
    # the context manager should fail.
    ex = GoogleExecutor()
    m = Mutation(platform="google", campaign_id="42", campaign_name="x", kind="pause")
    with pytest.raises(GoogleExecutorError, match="not active"):
        ex.dispatch(m)


# ---- Meta executor ------------------------------------------------------

def test_meta_pause_builds_correct_request(monkeypatch):
    monkeypatch.setenv("META_ACCESS_TOKEN", "TOK")
    sent = []

    def fake_http(method, path, body, token):
        sent.append((method, path, body, token))
        return {"success": True, "id": path.lstrip("/")}

    monkeypatch.setattr(meta, "_http", fake_http)

    m = Mutation(platform="meta", campaign_id="123", campaign_name="x",
                 kind="pause", before={"status": "ACTIVE"}, after={"status": "PAUSED"})
    result = meta.dispatch(m, dry_run=False)
    assert result == {"success": True, "id": "123"}
    assert sent == [("POST", "/123", {"status": "PAUSED"}, "TOK")]


def test_meta_budget_change_uses_cents(monkeypatch):
    monkeypatch.setenv("META_ACCESS_TOKEN", "TOK")
    sent = []

    def fake_http(method, path, body, token):
        sent.append((method, path, body, token))
        return {"success": True}

    monkeypatch.setattr(meta, "_http", fake_http)

    m = Mutation(platform="meta", campaign_id="123", campaign_name="x",
                 kind="budget_change",
                 before={"daily_budget": 50.00}, after={"daily_budget": 40.00})
    meta.dispatch(m, dry_run=False)
    # 40.00 dollars → "4000" cents (string).
    assert sent[0][2] == {"daily_budget": "4000"}


def test_meta_dry_run_returns_planned_request_without_calling_http(monkeypatch):
    monkeypatch.setenv("META_ACCESS_TOKEN", "TOK")
    called = []
    monkeypatch.setattr(meta, "_http", lambda *a, **kw: called.append(a))

    m = Mutation(platform="meta", campaign_id="123", campaign_name="x",
                 kind="pause", before={"status": "ACTIVE"}, after={"status": "PAUSED"})
    result = meta.dispatch(m, dry_run=True)
    assert called == []
    assert result["dry_run"] is True
    assert result["method"] == "POST"
    assert result["body"] == {"status": "PAUSED"}


def test_meta_missing_token_raises(monkeypatch):
    monkeypatch.delenv("META_ACCESS_TOKEN", raising=False)
    m = Mutation(platform="meta", campaign_id="1", campaign_name="x", kind="pause",
                 before={"status": "ACTIVE"}, after={"status": "PAUSED"})
    with pytest.raises(MetaExecutorError, match="META_ACCESS_TOKEN"):
        meta.dispatch(m, dry_run=False)


def test_meta_unsupported_kind_raises(monkeypatch):
    monkeypatch.setenv("META_ACCESS_TOKEN", "TOK")
    m = Mutation(platform="meta", campaign_id="1", campaign_name="x", kind="zzz")
    with pytest.raises(MetaExecutorError, match="Unsupported"):
        meta.dispatch(m, dry_run=False)


# ---- top-level dispatcher ----------------------------------------------

def test_dispatch_routes_to_google(monkeypatch):
    called = {}
    monkeypatch.setattr(google, "dispatch", lambda m, *, dry_run: called.setdefault("g", (m, dry_run)) or {"ok": True})
    m = Mutation(platform="google", campaign_id="x", campaign_name="x", kind="pause")
    dispatch(m, dry_run=True)
    assert called["g"][1] is True


def test_dispatch_routes_to_meta(monkeypatch):
    called = {}
    monkeypatch.setattr(meta, "dispatch", lambda m, *, dry_run: called.setdefault("m", (m, dry_run)) or {"ok": True})
    m = Mutation(platform="meta", campaign_id="x", campaign_name="x", kind="pause")
    dispatch(m, dry_run=False)
    assert called["m"][1] is False


def test_dispatch_rejects_linkedin():
    m = Mutation(platform="linkedin", campaign_id="x", campaign_name="x", kind="pause")
    with pytest.raises(UnsupportedPlatformError, match="Phase 3"):
        dispatch(m)


def test_dispatch_rejects_unknown_platform():
    m = Mutation(platform="tiktok", campaign_id="x", campaign_name="x", kind="pause")
    with pytest.raises(UnsupportedPlatformError, match="tiktok"):
        dispatch(m)
