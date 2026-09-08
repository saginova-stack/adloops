from __future__ import annotations

import json

import scripts.executors.linkedin as linkedin
from scripts.guardrails import Mutation


def _env(monkeypatch):
    monkeypatch.setenv("LINKEDIN_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("LINKEDIN_AD_ACCOUNT_URN", "urn:li:sponsoredAccount:539240077")


def _pause() -> Mutation:
    return Mutation(
        platform="linkedin",
        campaign_id="urn:li:sponsoredCampaign:892480164",
        campaign_name="A | Firm Owners",
        kind="pause",
        before={"status": "ACTIVE"},
        after={"status": "PAUSED"},
    )


def test_linkedin_rest_dry_run_has_no_network_io(monkeypatch):
    _env(monkeypatch)

    def fail_urlopen(*args, **kwargs):
        raise AssertionError("dry-run must not perform HTTP")

    monkeypatch.setattr(linkedin.urllib.request, "urlopen", fail_urlopen)
    result = linkedin.dispatch(_pause(), dry_run=True)

    assert result["dry_run"] is True
    assert result["method"] == "POST"
    assert result["body"] == {"patch": {"$set": {"status": "PAUSED"}}}
    assert result["url"].endswith("/adAccounts/539240077/adCampaigns/892480164")


def test_linkedin_rest_pause_reads_live_state_writes_then_verifies(monkeypatch):
    _env(monkeypatch)
    requests = []
    responses = iter([
        {"account": "urn:li:sponsoredAccount:539240077", "status": "ACTIVE"},
        {},
        {"account": "urn:li:sponsoredAccount:539240077", "status": "PAUSED"},
    ])

    class Response:
        def __init__(self, payload):
            self.payload = payload
            self.status = 204 if payload == {} else 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(self.payload).encode()

    def fake_urlopen(request, timeout=30):
        requests.append(request)
        return Response(next(responses))

    monkeypatch.setattr(linkedin.urllib.request, "urlopen", fake_urlopen)
    result = linkedin.dispatch(_pause(), dry_run=False)

    assert [r.method for r in requests] == ["GET", "POST", "GET"]
    assert requests[1].get_header("X-restli-method") == "PARTIAL_UPDATE"
    assert json.loads(requests[1].data) == {"patch": {"$set": {"status": "PAUSED"}}}
    assert result["verified"] is True


def test_linkedin_rest_budget_requires_explicit_currency_in_dry_run(monkeypatch):
    _env(monkeypatch)
    m = Mutation(
        platform="linkedin",
        campaign_id="urn:li:sponsoredCampaign:892480164",
        campaign_name="A | Firm Owners",
        kind="budget_change",
        before={"daily_budget": 10},
        after={"daily_budget": 9},
    )
    try:
        linkedin.dispatch(m, dry_run=True)
    except linkedin.ExecutorError as exc:
        assert "currency" in str(exc).lower()
    else:
        raise AssertionError("dry-run budget mutation must require an explicit currency")
