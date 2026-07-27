from __future__ import annotations

import json

import pytest

from scripts.executors import (
    UnsupportedPlatformError,
    dispatch,
    google,
    linkedin,
    meta,
)
from scripts.executors.google import ExecutorError as GoogleExecutorError
from scripts.executors.google import GoogleExecutor
from scripts.executors.linkedin import ExecutorError as LinkedInExecutorError
from scripts.executors.linkedin import LinkedInExecutor
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


def _meta_http_recorder(monkeypatch):
    sent = []

    def fake_http(method, path, body, token):
        sent.append((method, path, body))
        return {"success": True, "id": path.lstrip("/")}

    monkeypatch.setattr(meta, "_http", fake_http)
    return sent


def test_meta_budget_change_scales_campaign_level_budget(monkeypatch):
    """CBO campaign: budget lives on the campaign. A 50→40 change is a 0.8
    ratio, applied to the *live* budget we GET — not the stale 40 from the
    audit — so a manual bump between audit and dispatch isn't clobbered."""
    monkeypatch.setenv("META_ACCESS_TOKEN", "TOK")
    sent = _meta_http_recorder(monkeypatch)
    # Live budget drifted up to $60 (6000 cents) since the audit read $50.
    monkeypatch.setattr(meta, "_get", lambda path, params, token: {"daily_budget": "6000"})

    m = Mutation(platform="meta", campaign_id="123", campaign_name="x",
                 kind="budget_change",
                 before={"daily_budget": 50.00}, after={"daily_budget": 40.00})
    meta.dispatch(m, dry_run=False)
    # 0.8 ratio against the live 6000 cents → 4800, not the stale 4000.
    assert sent == [("POST", "/123", {"daily_budget": "4800"})]


def test_meta_budget_change_scales_campaign_lifetime_budget(monkeypatch):
    """CBO campaign on a lifetime budget: scale lifetime_budget, not daily."""
    monkeypatch.setenv("META_ACCESS_TOKEN", "TOK")
    sent = _meta_http_recorder(monkeypatch)
    monkeypatch.setattr(
        meta, "_get",
        lambda path, params, token: {"daily_budget": None, "lifetime_budget": "100000"},
    )

    m = Mutation(platform="meta", campaign_id="123", campaign_name="x",
                 kind="budget_change",
                 before={"daily_budget": 50.0}, after={"daily_budget": 40.0})
    meta.dispatch(m, dry_run=False)
    assert sent == [("POST", "/123", {"lifetime_budget": "80000"})]


def test_meta_budget_change_fans_out_across_adsets_non_cbo(monkeypatch):
    """Non-CBO campaign: no campaign budget, so every budgeted ad set is scaled
    by the same ratio. This preserves the relative allocation across ad sets."""
    monkeypatch.setenv("META_ACCESS_TOKEN", "TOK")
    sent = _meta_http_recorder(monkeypatch)

    def fake_get(path, params, token):
        if path.endswith("/adsets"):
            return {"data": [
                {"id": "AS1", "daily_budget": "3000"},
                {"id": "AS2", "daily_budget": "2000"},
            ]}
        return {}  # campaign has neither daily nor lifetime → non-CBO

    monkeypatch.setattr(meta, "_get", fake_get)

    m = Mutation(platform="meta", campaign_id="C1", campaign_name="x",
                 kind="budget_change",
                 before={"daily_budget": 50.0}, after={"daily_budget": 40.0})
    meta.dispatch(m, dry_run=False)
    # 0.8 ratio: AS1 3000→2400, AS2 2000→1600. Each ad set, not the campaign.
    assert sent == [
        ("POST", "/AS1", {"daily_budget": "2400"}),
        ("POST", "/AS2", {"daily_budget": "1600"}),
    ]


def test_meta_budget_change_raises_when_no_budget_found(monkeypatch):
    monkeypatch.setenv("META_ACCESS_TOKEN", "TOK")
    _meta_http_recorder(monkeypatch)
    monkeypatch.setattr(
        meta, "_get",
        lambda path, params, token: {"data": []} if path.endswith("/adsets") else {},
    )
    m = Mutation(platform="meta", campaign_id="C1", campaign_name="x",
                 kind="budget_change",
                 before={"daily_budget": 50.0}, after={"daily_budget": 40.0})
    with pytest.raises(MetaExecutorError, match="Could not locate"):
        meta.dispatch(m, dry_run=False)


def test_meta_budget_change_dry_run_resolves_but_does_not_post(monkeypatch):
    monkeypatch.setenv("META_ACCESS_TOKEN", "TOK")
    posted = []
    monkeypatch.setattr(meta, "_http", lambda *a, **kw: posted.append(a))
    monkeypatch.setattr(meta, "_get", lambda path, params, token: {"daily_budget": "5000"})

    m = Mutation(platform="meta", campaign_id="123", campaign_name="x",
                 kind="budget_change",
                 before={"daily_budget": 50.0}, after={"daily_budget": 40.0})
    result = meta.dispatch(m, dry_run=True)
    assert posted == []  # no mutating POST
    assert result["dry_run"] is True
    assert result["requests"] == [
        {"method": "POST", "path": "/123", "body": {"daily_budget": "4000"}}
    ]


def test_meta_budget_change_rejects_non_positive_before(monkeypatch):
    monkeypatch.setenv("META_ACCESS_TOKEN", "TOK")
    m = Mutation(platform="meta", campaign_id="123", campaign_name="x",
                 kind="budget_change",
                 before={"daily_budget": 0.0}, after={"daily_budget": 40.0})
    with pytest.raises(MetaExecutorError, match="before.daily_budget"):
        meta.dispatch(m, dry_run=False)


# ---- Meta create_campaign -----------------------------------------------

def test_meta_create_campaign_posts_to_account(monkeypatch):
    monkeypatch.setenv("META_ACCESS_TOKEN", "TOK")
    monkeypatch.setenv("META_AD_ACCOUNT_ID", "act_42")
    sent = _meta_http_recorder(monkeypatch)

    m = Mutation(platform="meta", campaign_id="", campaign_name="Q3 Launch",
                 kind="create_campaign",
                 after={"name": "Q3 Launch", "objective": "OUTCOME_LEADS",
                        "status": "PAUSED", "daily_budget": 25.0})
    meta.dispatch(m, dry_run=False)
    method, path, body = sent[0]
    assert (method, path) == ("POST", "/act_42/campaigns")
    assert body["name"] == "Q3 Launch"
    assert body["objective"] == "OUTCOME_LEADS"
    assert body["status"] == "PAUSED"
    assert body["daily_budget"] == "2500"
    # special_ad_categories is required by the Graph API — [] serialized to JSON.
    assert body["special_ad_categories"] == "[]"


def test_meta_create_campaign_requires_account(monkeypatch):
    monkeypatch.setenv("META_ACCESS_TOKEN", "TOK")
    monkeypatch.delenv("META_AD_ACCOUNT_ID", raising=False)
    m = Mutation(platform="meta", campaign_id="", campaign_name="x",
                 kind="create_campaign",
                 after={"name": "x", "objective": "OUTCOME_LEADS", "status": "PAUSED"})
    with pytest.raises(MetaExecutorError, match="META_AD_ACCOUNT_ID"):
        meta.dispatch(m, dry_run=False)


def test_meta_create_campaign_requires_name_and_objective(monkeypatch):
    monkeypatch.setenv("META_ACCESS_TOKEN", "TOK")
    monkeypatch.setenv("META_AD_ACCOUNT_ID", "act_42")
    m = Mutation(platform="meta", campaign_id="", campaign_name="x",
                 kind="create_campaign", after={"status": "PAUSED"})
    with pytest.raises(MetaExecutorError, match="after.name and after.objective"):
        meta.dispatch(m, dry_run=False)


def _adset_cfg(**kw):
    base = {"name": "US", "optimization_goal": "LEAD_GENERATION",
            "billing_event": "IMPRESSIONS",
            "targeting": {"geo_locations": {"countries": ["US"]}}}
    base.update(kw)
    return base


def test_meta_create_campaign_scaffolds_ad_set_under_returned_id(monkeypatch):
    """The ad set needs the campaign id, which only exists after the create
    returns — so it must be one chained dispatch, and the ad set must carry
    the new id, not a placeholder."""
    monkeypatch.setenv("META_ACCESS_TOKEN", "TOK")
    monkeypatch.setenv("META_AD_ACCOUNT_ID", "act_42")
    sent = []

    def fake_http(method, path, body, token):
        sent.append((method, path, body))
        return {"id": "C99"} if path.endswith("/campaigns") else {"id": "AS1"}

    monkeypatch.setattr(meta, "_http", fake_http)

    m = Mutation(platform="meta", campaign_id="", campaign_name="Q3",
                 kind="create_campaign",
                 after={"name": "Q3", "objective": "OUTCOME_LEADS", "status": "PAUSED",
                        "ad_set": _adset_cfg(daily_budget=25.0)})
    result = meta.dispatch(m, dry_run=False)

    assert [s[1] for s in sent] == ["/act_42/campaigns", "/act_42/adsets"]
    adset_body = sent[1][2]
    assert adset_body["campaign_id"] == "C99"     # the returned id, not a placeholder
    assert adset_body["status"] == "PAUSED"
    assert adset_body["optimization_goal"] == "LEAD_GENERATION"
    assert adset_body["daily_budget"] == "2500"   # cents
    assert json.loads(adset_body["targeting"]) == {"geo_locations": {"countries": ["US"]}}
    assert result == {"campaign": {"id": "C99"}, "ad_set": {"id": "AS1"}}


def _create_with_ad_set(ad_set):
    return Mutation(platform="meta", campaign_id="", campaign_name="Q3",
                    kind="create_campaign",
                    after={"name": "Q3", "objective": "OUTCOME_LEADS", "status": "PAUSED",
                           "ad_set": ad_set})


def _adset_targeting(**extra):
    base = _adset_cfg()
    base["targeting"] = {"geo_locations": {"countries": ["US"]}}
    base.update(extra)
    return base


def test_meta_ad_set_attaches_custom_audience_by_id(monkeypatch):
    monkeypatch.setenv("META_ACCESS_TOKEN", "TOK")
    monkeypatch.setenv("META_AD_ACCOUNT_ID", "act_42")
    sent = []

    def fake_http(method, path, body, token):
        sent.append((method, path, body))
        return {"id": "C99"} if path.endswith("/campaigns") else {"id": "AS1"}

    monkeypatch.setattr(meta, "_http", fake_http)
    # By-id only → the account audience list must NOT be fetched.
    monkeypatch.setattr(meta, "_get", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("_get should not be called for by-id audiences")))

    meta.dispatch(_create_with_ad_set(_adset_targeting(custom_audiences=[{"id": "AUD1"}])),
                  dry_run=False)
    adset_body = [s for s in sent if s[1].endswith("/adsets")][0][2]
    assert json.loads(adset_body["targeting"])["custom_audiences"] == [{"id": "AUD1"}]


def test_meta_ad_set_resolves_custom_audience_by_name(monkeypatch):
    monkeypatch.setenv("META_ACCESS_TOKEN", "TOK")
    monkeypatch.setenv("META_AD_ACCOUNT_ID", "act_42")
    sent = []
    monkeypatch.setattr(meta, "_http", lambda method, path, body, token: (
        sent.append((method, path, body)) or
        ({"id": "C99"} if path.endswith("/campaigns") else {"id": "AS1"})))
    monkeypatch.setattr(meta, "_get", lambda path, params, token: {
        "data": [{"id": "9001", "name": "SMB 50-200 employees"}]})

    meta.dispatch(_create_with_ad_set(_adset_targeting(
        custom_audiences=[{"name": "smb 50-200 EMPLOYEES"}])), dry_run=False)  # case-insensitive
    adset_body = [s for s in sent if s[1].endswith("/adsets")][0][2]
    assert json.loads(adset_body["targeting"])["custom_audiences"] == [{"id": "9001"}]


def test_meta_ad_set_custom_audience_name_not_found_raises(monkeypatch):
    monkeypatch.setenv("META_ACCESS_TOKEN", "TOK")
    monkeypatch.setenv("META_AD_ACCOUNT_ID", "act_42")
    monkeypatch.setattr(meta, "_http", lambda *a, **k: {"id": "C99"})
    monkeypatch.setattr(meta, "_get", lambda *a, **k: {"data": []})
    with pytest.raises(MetaExecutorError, match="not found in the ad account"):
        meta.dispatch(_create_with_ad_set(_adset_targeting(
            custom_audiences=[{"name": "Nonexistent"}])), dry_run=False)


def test_meta_lookalike_reused_when_already_exists(monkeypatch):
    """Idempotent: a lookalike already named as declared is reused, not recreated
    — otherwise every run spawns a duplicate audience object."""
    monkeypatch.setenv("META_ACCESS_TOKEN", "TOK")
    monkeypatch.setenv("META_AD_ACCOUNT_ID", "act_42")
    posts = []
    monkeypatch.setattr(meta, "_http", lambda method, path, body, token: (
        posts.append((method, path, body)) or
        ({"id": "C99"} if path.endswith("/campaigns") else {"id": "AS1"})))
    monkeypatch.setattr(meta, "_get", lambda *a, **k: {
        "data": [{"id": "LAL7", "name": "LAL - SMB"}]})

    lal = {"name": "LAL - SMB", "seed": {"name": "Seed"}, "country": "US", "ratio": 0.03}
    meta.dispatch(_create_with_ad_set(_adset_targeting(lookalike_audiences=[lal])), dry_run=False)
    # No customaudiences POST — only campaign + adset.
    assert [p[1] for p in posts] == ["/act_42/campaigns", "/act_42/adsets"]
    adset_body = [p for p in posts if p[1].endswith("/adsets")][0][2]
    assert json.loads(adset_body["targeting"])["custom_audiences"] == [{"id": "LAL7"}]


def test_meta_lookalike_created_from_seed_when_missing(monkeypatch):
    monkeypatch.setenv("META_ACCESS_TOKEN", "TOK")
    monkeypatch.setenv("META_AD_ACCOUNT_ID", "act_42")
    posts = []

    def fake_http(method, path, body, token):
        posts.append((method, path, body))
        if path.endswith("/campaigns"):
            return {"id": "C99"}
        if path.endswith("/customaudiences"):
            return {"id": "LAL_NEW"}
        return {"id": "AS1"}

    monkeypatch.setattr(meta, "_http", fake_http)
    # Seed exists, lookalike does not.
    monkeypatch.setattr(meta, "_get", lambda *a, **k: {
        "data": [{"id": "SEED1", "name": "Closed Won"}]})

    lal = {"name": "LAL - fit", "seed": {"name": "Closed Won"}, "country": "US", "ratio": 0.03}
    meta.dispatch(_create_with_ad_set(_adset_targeting(lookalike_audiences=[lal])), dry_run=False)

    create = [p for p in posts if p[1].endswith("/customaudiences")][0][2]
    assert create["subtype"] == "LOOKALIKE"
    assert create["origin_audience_id"] == "SEED1"
    assert json.loads(create["lookalike_spec"]) == {"ratio": 0.03, "country": "US"}
    adset_body = [p for p in posts if p[1].endswith("/adsets")][0][2]
    assert json.loads(adset_body["targeting"])["custom_audiences"] == [{"id": "LAL_NEW"}]


def test_meta_create_campaign_with_ad_set_dry_run_posts_nothing(monkeypatch):
    monkeypatch.setenv("META_ACCESS_TOKEN", "TOK")
    monkeypatch.setenv("META_AD_ACCOUNT_ID", "act_42")
    posted = []
    monkeypatch.setattr(meta, "_http", lambda *a, **k: posted.append(a))

    m = Mutation(platform="meta", campaign_id="", campaign_name="Q3",
                 kind="create_campaign",
                 after={"name": "Q3", "objective": "OUTCOME_LEADS", "status": "PAUSED",
                        "ad_set": _adset_cfg()})
    result = meta.dispatch(m, dry_run=True)
    assert posted == []
    assert result["dry_run"] is True
    assert result["path"] == "/act_42/campaigns"
    assert result["ad_set"]["path"] == "/act_42/adsets"
    assert result["ad_set"]["body"]["campaign_id"] == "<new-campaign-id>"


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


# ---- LinkedIn executor --------------------------------------------------

def _li_env(monkeypatch):
    monkeypatch.setenv("LINKEDIN_AD_ACCOUNT_URN", "urn:li:sponsoredAccount:5550001")


def test_linkedin_pause_calls_update_campaign_with_status_paused(monkeypatch):
    _li_env(monkeypatch)
    sess = FakeSession({
        "update_campaign": {"success": True, "campaignId": "12345"},
    })
    m = Mutation(
        platform="linkedin",
        campaign_id="urn:li:sponsoredAdCampaign:12345",
        campaign_name="Brand",
        kind="pause",
        before={"status": "ACTIVE"}, after={"status": "PAUSED"},
    )
    with LinkedInExecutor(session=sess) as ex:
        result = ex.dispatch(m, dry_run=False)
    assert result == {"success": True, "campaignId": "12345"}
    assert sess.calls == [(
        "update_campaign",
        {"accountId": "5550001", "campaignId": "12345", "status": "PAUSED"},
    )]


def test_linkedin_enable_uses_status_active(monkeypatch):
    _li_env(monkeypatch)
    sess = FakeSession({"update_campaign": {"success": True}})
    m = Mutation(
        platform="linkedin",
        campaign_id="urn:li:sponsoredAdCampaign:42",
        campaign_name="x",
        kind="enable",
        before={"status": "PAUSED"}, after={"status": "ACTIVE"},
    )
    with LinkedInExecutor(session=sess) as ex:
        ex.dispatch(m, dry_run=False)
    assert sess.calls[0][1]["status"] == "ACTIVE"


def test_linkedin_budget_change_passes_daily_budget_amount_as_string(monkeypatch):
    _li_env(monkeypatch)
    sess = FakeSession({"update_campaign": {"success": True}})
    m = Mutation(
        platform="linkedin",
        campaign_id="urn:li:sponsoredAdCampaign:42",
        campaign_name="x",
        kind="budget_change",
        before={"daily_budget": 100.0}, after={"daily_budget": 80.5},
    )
    with LinkedInExecutor(session=sess) as ex:
        ex.dispatch(m, dry_run=False)
    args = sess.calls[0][1]
    assert args == {
        "accountId": "5550001",
        "campaignId": "42",
        "dailyBudgetAmount": "80.50",
    }


def test_linkedin_budget_change_honors_explicit_currency(monkeypatch):
    _li_env(monkeypatch)
    sess = FakeSession({"update_campaign": {"success": True}})
    m = Mutation(
        platform="linkedin",
        campaign_id="urn:li:sponsoredAdCampaign:42",
        campaign_name="x",
        kind="budget_change",
        before={"daily_budget": 100.0},
        after={"daily_budget": 80.0, "currency": "EUR"},
    )
    with LinkedInExecutor(session=sess) as ex:
        ex.dispatch(m, dry_run=False)
    assert sess.calls[0][1]["dailyBudgetCurrency"] == "EUR"


def test_linkedin_budget_change_rejects_non_positive(monkeypatch):
    _li_env(monkeypatch)
    sess = FakeSession({})
    m = Mutation(
        platform="linkedin",
        campaign_id="urn:li:sponsoredAdCampaign:42",
        campaign_name="x",
        kind="budget_change",
        before={"daily_budget": 100.0}, after={"daily_budget": 0.0},
    )
    with LinkedInExecutor(session=sess) as ex, pytest.raises(LinkedInExecutorError, match="non-positive"):
        ex.dispatch(m, dry_run=False)
    assert sess.calls == []


def test_linkedin_dry_run_does_not_call_tool(monkeypatch):
    _li_env(monkeypatch)
    sess = FakeSession({})  # any call would raise — proves we didn't call
    m = Mutation(
        platform="linkedin",
        campaign_id="urn:li:sponsoredAdCampaign:42",
        campaign_name="x",
        kind="pause",
        before={"status": "ACTIVE"}, after={"status": "PAUSED"},
    )
    with LinkedInExecutor(session=sess) as ex:
        result = ex.dispatch(m, dry_run=True)
    assert sess.calls == []
    assert result["dry_run"] is True
    assert result["tool"] == "update_campaign"
    assert result["arguments"]["status"] == "PAUSED"
    # raw URN preserved in the audit shape so logs match the audit's campaign_id
    assert result["campaign_id"] == "urn:li:sponsoredAdCampaign:42"


def test_linkedin_unsupported_kind_raises(monkeypatch):
    _li_env(monkeypatch)
    sess = FakeSession({})
    m = Mutation(
        platform="linkedin",
        campaign_id="urn:li:sponsoredAdCampaign:1",
        campaign_name="x",
        kind="something_weird",
    )
    with LinkedInExecutor(session=sess) as ex, pytest.raises(LinkedInExecutorError, match="Unsupported"):
        ex.dispatch(m, dry_run=False)


def test_linkedin_missing_account_urn_raises(monkeypatch):
    monkeypatch.delenv("LINKEDIN_AD_ACCOUNT_URN", raising=False)
    sess = FakeSession({})
    m = Mutation(
        platform="linkedin",
        campaign_id="urn:li:sponsoredAdCampaign:1",
        campaign_name="x",
        kind="pause",
        before={"status": "ACTIVE"}, after={"status": "PAUSED"},
    )
    with LinkedInExecutor(session=sess) as ex, pytest.raises(LinkedInExecutorError, match="LINKEDIN_AD_ACCOUNT_URN"):
        ex.dispatch(m, dry_run=False)


def test_linkedin_dispatch_requires_active_session():
    ex = LinkedInExecutor()
    m = Mutation(
        platform="linkedin",
        campaign_id="urn:li:sponsoredAdCampaign:1",
        campaign_name="x",
        kind="pause",
    )
    with pytest.raises(LinkedInExecutorError, match="not active"):
        ex.dispatch(m)


def test_linkedin_strips_urn_to_numeric_id(monkeypatch):
    """Audit stores campaign_id as the URN; MCP expects the numeric tail.

    Guard against drift: if either side changes encoding, this catches it."""
    _li_env(monkeypatch)
    sess = FakeSession({"update_campaign": {"success": True}})
    m = Mutation(
        platform="linkedin",
        campaign_id="urn:li:sponsoredAdCampaign:9876543210",
        campaign_name="x",
        kind="pause",
        before={"status": "ACTIVE"}, after={"status": "PAUSED"},
    )
    with LinkedInExecutor(session=sess) as ex:
        ex.dispatch(m, dry_run=False)
    assert sess.calls[0][1]["campaignId"] == "9876543210"


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


def test_dispatch_routes_to_linkedin(monkeypatch):
    called = {}
    monkeypatch.setattr(linkedin, "dispatch", lambda m, *, dry_run: called.setdefault("l", (m, dry_run)) or {"ok": True})
    m = Mutation(platform="linkedin", campaign_id="x", campaign_name="x", kind="pause")
    dispatch(m, dry_run=True)
    assert called["l"][1] is True


def test_dispatch_rejects_unknown_platform():
    m = Mutation(platform="tiktok", campaign_id="x", campaign_name="x", kind="pause")
    with pytest.raises(UnsupportedPlatformError, match="tiktok"):
        dispatch(m)
