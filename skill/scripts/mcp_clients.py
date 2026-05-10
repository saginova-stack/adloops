"""Thin Python adapters that talk to each ad-platform MCP / API.

Phase 1 contract (read-only): every adapter exposes one method,
`fetch_perf_7d() -> list[CampaignPerf]`, returning a normalized shape
across platforms. Mutation methods will be added in Phase 2 and routed
through scripts/guardrails.py.

Why hand-rolled clients instead of MCP-over-stdio for read calls?
The MCP servers are designed for an LLM client (fastmcp/Node MCP). For a
headless cron run we don't need an LLM in the loop to pull a single
report — direct API access is simpler and deterministic. The MCP servers
will still be the canonical client for *interactive* OC sessions and for
mutations in Phase 2 (where the preview/confirm pattern of the kLOsk
adloop server matches our guardrail flow exactly).

Auth env (Phase 1):
- Google: GOOGLE_ADS_DEVELOPER_TOKEN, GOOGLE_ADS_CLIENT_ID,
  GOOGLE_ADS_CLIENT_SECRET, GOOGLE_ADS_REFRESH_TOKEN, GOOGLE_ADS_LOGIN_CUSTOMER_ID
- Meta: META_ACCESS_TOKEN, META_AD_ACCOUNT_ID (e.g. "act_1234567890")
- LinkedIn: LINKEDIN_ACCESS_TOKEN, LINKEDIN_AD_ACCOUNT_URN
"""

from __future__ import annotations

import datetime as dt
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class CampaignPerf:
    platform: str
    campaign_id: str
    campaign_name: str
    status: str
    daily_budget: float | None     # native currency, None if budget unknown / shared
    spend: float                    # 7-day sum
    impressions: int
    clicks: int
    conversions: float
    revenue: float | None           # 7-day sum, if available
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def cpa(self) -> float | None:
        return self.spend / self.conversions if self.conversions else None

    @property
    def roas(self) -> float | None:
        return self.revenue / self.spend if (self.revenue is not None and self.spend) else None

    @property
    def ctr(self) -> float | None:
        return self.clicks / self.impressions if self.impressions else None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MissingCredentialsError(RuntimeError):
    pass


def _last_7_days() -> tuple[str, str]:
    end = dt.date.today() - dt.timedelta(days=1)
    start = end - dt.timedelta(days=6)
    return start.isoformat(), end.isoformat()


# ---------------- Google Ads ----------------

class GoogleAdsClient:
    """Reads via the official google-ads Python SDK, same auth shape as kLOsk/adloop.

    We don't import google-ads at module load time — only when fetch_perf_7d() is
    called — so the rest of the skill (and the test suite) can import this file
    without the SDK being installed yet.
    """

    REQUIRED_ENV = (
        "GOOGLE_ADS_DEVELOPER_TOKEN",
        "GOOGLE_ADS_CLIENT_ID",
        "GOOGLE_ADS_CLIENT_SECRET",
        "GOOGLE_ADS_REFRESH_TOKEN",
        "GOOGLE_ADS_LOGIN_CUSTOMER_ID",
    )

    def __init__(self, customer_id: str | None = None):
        self.customer_id = customer_id or os.environ.get("GOOGLE_ADS_CUSTOMER_ID") \
            or os.environ.get("GOOGLE_ADS_LOGIN_CUSTOMER_ID")
        missing = [k for k in self.REQUIRED_ENV if not os.environ.get(k)]
        if missing:
            raise MissingCredentialsError(f"Google Ads: missing env {missing}")
        if not self.customer_id:
            raise MissingCredentialsError("Google Ads: customer_id (login or operating) required")

    def fetch_perf_7d(self) -> list[CampaignPerf]:
        from google.ads.googleads.client import GoogleAdsClient as SDK  # type: ignore

        cfg = {
            "developer_token": os.environ["GOOGLE_ADS_DEVELOPER_TOKEN"],
            "client_id": os.environ["GOOGLE_ADS_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_ADS_CLIENT_SECRET"],
            "refresh_token": os.environ["GOOGLE_ADS_REFRESH_TOKEN"],
            "login_customer_id": os.environ["GOOGLE_ADS_LOGIN_CUSTOMER_ID"],
            "use_proto_plus": True,
        }
        client = SDK.load_from_dict(cfg)
        ga_service = client.get_service("GoogleAdsService")

        start, end = _last_7_days()
        gaql = f"""
            SELECT
              campaign.id, campaign.name, campaign.status,
              campaign_budget.amount_micros,
              metrics.cost_micros, metrics.impressions, metrics.clicks,
              metrics.conversions, metrics.conversions_value
            FROM campaign
            WHERE segments.date BETWEEN '{start}' AND '{end}'
        """

        out: list[CampaignPerf] = []
        rows = ga_service.search(customer_id=str(self.customer_id), query=gaql)
        # Aggregate by campaign — search returns one row per campaign per day.
        agg: dict[str, dict[str, Any]] = {}
        for row in rows:
            cid = str(row.campaign.id)
            cur = agg.setdefault(cid, {
                "name": row.campaign.name,
                "status": row.campaign.status.name,
                "daily_budget": (row.campaign_budget.amount_micros or 0) / 1_000_000,
                "spend": 0.0,
                "impressions": 0,
                "clicks": 0,
                "conversions": 0.0,
                "revenue": 0.0,
            })
            cur["spend"] += row.metrics.cost_micros / 1_000_000
            cur["impressions"] += int(row.metrics.impressions)
            cur["clicks"] += int(row.metrics.clicks)
            cur["conversions"] += float(row.metrics.conversions)
            cur["revenue"] += float(row.metrics.conversions_value)

        for cid, c in agg.items():
            out.append(CampaignPerf(
                platform="google",
                campaign_id=cid,
                campaign_name=c["name"],
                status=c["status"],
                daily_budget=c["daily_budget"],
                spend=round(c["spend"], 2),
                impressions=c["impressions"],
                clicks=c["clicks"],
                conversions=round(c["conversions"], 2),
                revenue=round(c["revenue"], 2) if c["revenue"] else None,
            ))
        return out


# ---------------- Meta Ads ----------------

class MetaAdsClient:
    """Direct Marketing API call. The official Meta Ads CLI (Apr 2026) wraps
    this same Insights endpoint; we wrap it ourselves to avoid taking a
    dependency on a tool that may move. When/if the CLI ships an MCP shim
    we'll switch this to an MCP client.
    """

    REQUIRED_ENV = ("META_ACCESS_TOKEN", "META_AD_ACCOUNT_ID")
    GRAPH_VERSION = "v22.0"

    def __init__(self):
        missing = [k for k in self.REQUIRED_ENV if not os.environ.get(k)]
        if missing:
            raise MissingCredentialsError(f"Meta Ads: missing env {missing}")
        self.token = os.environ["META_ACCESS_TOKEN"]
        self.account_id = os.environ["META_AD_ACCOUNT_ID"]

    def fetch_perf_7d(self) -> list[CampaignPerf]:
        start, end = _last_7_days()
        # Insights at the campaign level, summed across the window.
        url = (
            f"https://graph.facebook.com/{self.GRAPH_VERSION}/"
            f"{self.account_id}/insights"
        )
        params = {
            "fields": ",".join([
                "campaign_id", "campaign_name", "spend",
                "impressions", "clicks", "actions", "action_values",
            ]),
            "level": "campaign",
            "time_range": json.dumps({"since": start, "until": end}),
            "limit": "500",
            "access_token": self.token,
        }
        rows = _fetch_paged(url, params, items_key="data")

        # Pull campaign metadata (status + budget) in a second call —
        # insights doesn't return them.
        meta = self._fetch_campaign_meta()

        out: list[CampaignPerf] = []
        for r in rows:
            cid = r["campaign_id"]
            m = meta.get(cid, {})
            conv, rev = _meta_actions_to_conv_rev(r.get("actions"), r.get("action_values"))
            out.append(CampaignPerf(
                platform="meta",
                campaign_id=cid,
                campaign_name=r.get("campaign_name") or m.get("name", cid),
                status=m.get("status", "UNKNOWN"),
                daily_budget=m.get("daily_budget"),
                spend=float(r.get("spend") or 0),
                impressions=int(r.get("impressions") or 0),
                clicks=int(r.get("clicks") or 0),
                conversions=conv,
                revenue=rev,
            ))
        return out

    def _fetch_campaign_meta(self) -> dict[str, dict[str, Any]]:
        url = (
            f"https://graph.facebook.com/{self.GRAPH_VERSION}/"
            f"{self.account_id}/campaigns"
        )
        params = {
            "fields": "id,name,status,daily_budget",
            "limit": "500",
            "access_token": self.token,
        }
        rows = _fetch_paged(url, params, items_key="data")
        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            db = r.get("daily_budget")
            # Meta returns daily_budget as cents (string). Normalize to float
            # in account currency major units.
            db_f = float(db) / 100 if db is not None else None
            out[r["id"]] = {
                "name": r.get("name"),
                "status": r.get("status", "UNKNOWN"),
                "daily_budget": db_f,
            }
        return out


def _meta_actions_to_conv_rev(actions: list[dict] | None,
                              action_values: list[dict] | None) -> tuple[float, float | None]:
    """Sum standard purchase / lead actions into (conversions, revenue).

    Meta returns actions as a list of {action_type, value}. We count
    `purchase`, `lead`, `complete_registration`, and `submit_application`
    as conversions. Revenue comes from `action_values.purchase`.
    """
    if not actions:
        return 0.0, None
    countable = {"purchase", "lead", "complete_registration", "submit_application", "offsite_conversion.fb_pixel_purchase", "onsite_conversion.lead_grouped"}
    conv = 0.0
    for a in actions:
        if a.get("action_type") in countable:
            conv += float(a.get("value") or 0)
    rev: float | None = None
    if action_values:
        for av in action_values:
            if av.get("action_type") in {"purchase", "offsite_conversion.fb_pixel_purchase"}:
                rev = (rev or 0) + float(av.get("value") or 0)
    return conv, rev


# ---------------- LinkedIn Ads ----------------

class LinkedInAdsClient:
    """Reads via the LinkedIn Marketing API. Same auth contract as
    danielpopamd/linkedin-ads-mcp uses; we hit the API directly here
    rather than spawn the MCP server for a one-shot read.
    """

    REQUIRED_ENV = ("LINKEDIN_ACCESS_TOKEN", "LINKEDIN_AD_ACCOUNT_URN")
    API_VERSION = "202504"

    def __init__(self):
        missing = [k for k in self.REQUIRED_ENV if not os.environ.get(k)]
        if missing:
            raise MissingCredentialsError(f"LinkedIn Ads: missing env {missing}")
        self.token = os.environ["LINKEDIN_ACCESS_TOKEN"]
        self.account_urn = os.environ["LINKEDIN_AD_ACCOUNT_URN"]
        # Account URN format: "urn:li:sponsoredAccount:1234567890"
        self.account_id = self.account_urn.rsplit(":", 1)[-1]

    def fetch_perf_7d(self) -> list[CampaignPerf]:
        start, end = _last_7_days()
        # 1) list campaigns in the account
        campaigns = self._list_campaigns()
        # 2) pull analytics for the window, grouped by campaign
        analytics = self._campaign_analytics(start, end, list(campaigns.keys()))

        out: list[CampaignPerf] = []
        for urn, c in campaigns.items():
            a = analytics.get(urn, {})
            db = c.get("dailyBudget", {}).get("amount")
            db_f = float(db) if db is not None else None
            spend = float(a.get("costInLocalCurrency") or 0)
            clicks = int(a.get("clicks") or 0)
            imps = int(a.get("impressions") or 0)
            # Conversions on LinkedIn: externalWebsiteConversions + oneClickLeads
            conv = float(a.get("externalWebsiteConversions") or 0) \
                + float(a.get("oneClickLeads") or 0)
            out.append(CampaignPerf(
                platform="linkedin",
                campaign_id=urn,
                campaign_name=c.get("name") or urn,
                status=c.get("status") or "UNKNOWN",
                daily_budget=db_f,
                spend=round(spend, 2),
                impressions=imps,
                clicks=clicks,
                conversions=round(conv, 2),
                revenue=None,  # LinkedIn doesn't report revenue at campaign level by default
            ))
        return out

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "X-Restli-Protocol-Version": "2.0.0",
            "LinkedIn-Version": self.API_VERSION,
        }

    def _list_campaigns(self) -> dict[str, dict[str, Any]]:
        url = "https://api.linkedin.com/rest/adCampaigns"
        params = {
            "q": "search",
            "search.account.values[0]": self.account_urn,
            "count": "100",
        }
        rows = _fetch_paged(url, params, items_key="elements", headers=self._headers())
        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            urn = f"urn:li:sponsoredCampaign:{r['id']}"
            out[urn] = r
        return out

    def _campaign_analytics(self, start: str, end: str, urns: list[str]) -> dict[str, dict[str, Any]]:
        if not urns:
            return {}
        s = dt.date.fromisoformat(start)
        e = dt.date.fromisoformat(end)
        url = "https://api.linkedin.com/rest/adAnalytics"
        params = {
            "q": "analytics",
            "pivot": "CAMPAIGN",
            "timeGranularity": "ALL",
            f"dateRange.start.year": str(s.year),
            f"dateRange.start.month": str(s.month),
            f"dateRange.start.day": str(s.day),
            f"dateRange.end.year": str(e.year),
            f"dateRange.end.month": str(e.month),
            f"dateRange.end.day": str(e.day),
            "fields": "costInLocalCurrency,impressions,clicks,externalWebsiteConversions,oneClickLeads,pivotValue",
        }
        for i, urn in enumerate(urns):
            params[f"campaigns[{i}]"] = urn
        rows = _fetch_paged(url, params, items_key="elements", headers=self._headers())
        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            urn = r.get("pivotValue") or r.get("pivotValues", [None])[0]
            if urn:
                out[urn] = r
        return out


# ---------------- HTTP helper ----------------

def _fetch_paged(
    url: str,
    params: dict[str, str],
    *,
    items_key: str,
    headers: dict[str, str] | None = None,
    max_pages: int = 50,
) -> list[dict[str, Any]]:
    """Fetch a paged JSON endpoint, returning the concatenated `items_key` list.

    Handles Meta's `paging.next` URLs and LinkedIn's `paging.start/count`.
    """
    out: list[dict[str, Any]] = []
    next_url: str | None = url + "?" + urllib.parse.urlencode(params, doseq=True)
    pages = 0
    while next_url and pages < max_pages:
        req = urllib.request.Request(next_url, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code} from {next_url}: {e.read().decode('utf-8', 'replace')}") from e
        out.extend(body.get(items_key, []))
        # Meta-style cursor pagination
        next_url = (body.get("paging") or {}).get("next")
        # LinkedIn-style: presence of paging.links.next
        if not next_url:
            for link in (body.get("paging") or {}).get("links", []):
                if link.get("rel") == "next" and link.get("href"):
                    next_url = "https://api.linkedin.com" + link["href"]
                    break
            else:
                next_url = None
        pages += 1
    return out
