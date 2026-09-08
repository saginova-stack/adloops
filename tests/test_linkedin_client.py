from __future__ import annotations

import scripts.mcp_clients as clients


def test_linkedin_campaign_search_uses_account_scoped_current_rest_endpoint(monkeypatch):
    monkeypatch.setenv("LINKEDIN_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("LINKEDIN_AD_ACCOUNT_URN", "urn:li:sponsoredAccount:539240077")
    seen = {}

    def fake_fetch(url, params, *, items_key, headers=None, max_pages=50):
        seen.update(url=url, params=params, items_key=items_key, headers=headers)
        return [{"id": 123, "name": "Campaign", "status": "ACTIVE"}]

    monkeypatch.setattr(clients, "_fetch_paged", fake_fetch)
    result = clients.LinkedInAdsClient()._list_campaigns()

    assert seen["url"] == "https://api.linkedin.com/rest/adAccounts/539240077/adCampaigns"
    assert seen["params"] == {"q": "search"}
    assert seen["headers"]["LinkedIn-Version"] == "202509"
    assert result["urn:li:sponsoredCampaign:123"]["name"] == "Campaign"


def test_linkedin_analytics_uses_restli_composite_query_without_encoded_fields(monkeypatch):
    monkeypatch.setenv("LINKEDIN_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("LINKEDIN_AD_ACCOUNT_URN", "urn:li:sponsoredAccount:539240077")
    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"elements":[{"pivotValues":["urn:li:sponsoredCampaign:123"],"clicks":2}]}'

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["headers"] = request.headers
        return Response()

    monkeypatch.setattr(clients.urllib.request, "urlopen", fake_urlopen)
    result = clients.LinkedInAdsClient()._campaign_analytics(
        "2026-09-01", "2026-09-07", ["urn:li:sponsoredCampaign:123"]
    )

    assert "accounts=List(urn%3Ali%3AsponsoredAccount%3A539240077)" in seen["url"]
    assert "fields=costInLocalCurrency,impressions,clicks,externalWebsiteConversions,pivotValues" in seen["url"]
    assert "%2C" not in seen["url"]
    assert result["urn:li:sponsoredCampaign:123"]["clicks"] == 2
