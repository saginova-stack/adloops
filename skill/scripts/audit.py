"""Audit cycle — pull 7d performance from each enabled platform, diff vs prior
snapshot, surface top movers, and write a fresh snapshot for next run."""

from __future__ import annotations

import datetime as dt
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import paths
from .mcp_clients import (
    CampaignPerf,
    GoogleAdsClient,
    GoogleAnalyticsClient,
    LinkedInAdsClient,
    MetaAdsClient,
    MissingCredentialsError,
)


@dataclass
class PlatformResult:
    platform: str
    enabled: bool
    fetched: bool
    error: str | None
    campaigns: list[CampaignPerf] = field(default_factory=list)
    # GA4 cross-reference enrichment is best-effort: when it fails (missing
    # creds, GA4 disabled on the account, API hiccup) we keep the Google Ads
    # numbers but surface the reason here so the report can flag it.
    ga4_status: str | None = None  # "ok" | "disabled" | "skipped: <reason>"
    # Landing-page secondary GA4 query is independent: the primary
    # campaign-level enrichment can succeed while this one errors. Kept on
    # its own field so a working ga4_status isn't masked by a flaky landing
    # query.
    ga4_landing_status: str | None = None

    def totals(self) -> dict[str, float]:
        spend = sum(c.spend for c in self.campaigns)
        imps = sum(c.impressions for c in self.campaigns)
        clicks = sum(c.clicks for c in self.campaigns)
        convs = sum(c.conversions for c in self.campaigns)
        revs = [c.revenue for c in self.campaigns if c.revenue is not None]
        return {
            "spend": round(spend, 2),
            "impressions": imps,
            "clicks": clicks,
            "conversions": round(convs, 2),
            "revenue": round(sum(revs), 2) if revs else 0.0,
            "cpa": round(spend / convs, 2) if convs else None,
            "roas": round(sum(revs) / spend, 2) if (revs and spend) else None,
        }


@dataclass
class CampaignDelta:
    """Day-over-day delta between this run's window and the previous run's."""
    platform: str
    campaign_id: str
    campaign_name: str
    spend_now: float
    spend_prev: float | None
    spend_change_pct: float | None
    cpa_now: float | None
    cpa_prev: float | None
    conversions_now: float
    conversions_prev: float | None


@dataclass
class AuditReport:
    run_id: str
    generated_at: str
    window_start: str
    window_end: str
    platforms: list[PlatformResult]
    deltas: list[CampaignDelta]
    top_movers_best: list[CampaignDelta]
    top_movers_worst: list[CampaignDelta]
    actions_taken: list[dict[str, Any]]            # P1: empty
    pending_approvals: list[dict[str, Any]]        # P1: empty
    recommendations: list[str]                     # P1: rule-based, no LLM yet


def _safe_pct(now: float, prev: float | None) -> float | None:
    if prev is None or prev == 0:
        return None
    return ((now - prev) / prev) * 100.0


def fetch_all(enabled_platforms: set[str]) -> list[PlatformResult]:
    results: list[PlatformResult] = []
    for plat, cls in (
        ("google", GoogleAdsClient),
        ("meta", MetaAdsClient),
        ("linkedin", LinkedInAdsClient),
    ):
        if plat not in enabled_platforms:
            results.append(PlatformResult(plat, enabled=False, fetched=False, error=None))
            continue
        try:
            client = cls()
        except MissingCredentialsError as e:
            results.append(PlatformResult(plat, enabled=True, fetched=False, error=str(e)))
            continue
        try:
            campaigns = client.fetch_perf_7d()
            results.append(PlatformResult(plat, enabled=True, fetched=True, error=None, campaigns=campaigns))
        except Exception as e:  # noqa: BLE001 — top-level audit is best-effort per platform
            results.append(PlatformResult(plat, enabled=True, fetched=False, error=f"{type(e).__name__}: {e}"))
    enrich_google_with_ga4(results)
    enrich_google_with_landing_pages(results)
    return results


def enrich_google_with_ga4(results: list[PlatformResult]) -> None:
    """Join GA4 paid-Google metrics onto each fetched Google Ads campaign.

    Best-effort: missing creds or API errors leave the Google rows
    un-enriched but populate `ga4_status` on the Google `PlatformResult`
    so the report can surface why the cross-reference signals aren't
    available.
    """
    google = next((r for r in results if r.platform == "google" and r.fetched), None)
    if not google:
        return
    try:
        ga4 = GoogleAnalyticsClient()
    except MissingCredentialsError as e:
        google.ga4_status = f"skipped: {e}"
        return
    try:
        ga4_data = ga4.fetch_paid_google_metrics_7d()
    except Exception as e:  # noqa: BLE001 — enrichment is best-effort
        google.ga4_status = f"skipped: {type(e).__name__}: {e}"
        return
    matched = 0
    for c in google.campaigns:
        m = ga4_data.get(c.campaign_id)
        if not m:
            continue
        c.ga4_sessions = m.sessions
        c.ga4_conversions = round(m.conversions, 2)
        c.ga4_revenue = round(m.revenue, 2) if m.revenue else None
        matched += 1
    if matched == 0 and google.campaigns:
        google.ga4_status = (
            "skipped: no GA4 rows matched any Google Ads campaign id — "
            "check that the GA4 property has Google Ads linked"
        )
    else:
        google.ga4_status = "ok"


def enrich_google_with_landing_pages(results: list[PlatformResult]) -> None:
    """Attach per-landing-page GA4 metrics to each Google Ads campaign.

    Depends on enrich_google_with_ga4 having succeeded: if the primary
    enrichment is anything other than "ok" we skip silently with a status
    that points at the same root cause — there's no signal worth pulling
    if we couldn't even resolve the campaign-level join.
    """
    google = next((r for r in results if r.platform == "google" and r.fetched), None)
    if not google:
        return
    if google.ga4_status != "ok":
        google.ga4_landing_status = "skipped: primary GA4 enrichment not ok"
        return
    try:
        ga4 = GoogleAnalyticsClient()
    except MissingCredentialsError as e:
        google.ga4_landing_status = f"skipped: {e}"
        return
    try:
        by_campaign = ga4.fetch_paid_landing_pages_7d()
    except Exception as e:  # noqa: BLE001 — secondary query is best-effort
        google.ga4_landing_status = f"skipped: {type(e).__name__}: {e}"
        return
    for c in google.campaigns:
        rows = by_campaign.get(c.campaign_id)
        if rows:
            c.ga4_landing_pages = rows
    google.ga4_landing_status = "ok"


def latest_archive(archive_dir: Path | None = None) -> dict[str, Any] | None:
    d = archive_dir or paths.archive_dir()
    if not d.exists():
        return None
    snaps = sorted(d.glob("*.json"))
    if not snaps:
        return None
    try:
        return json.loads(snaps[-1].read_text())
    except (OSError, json.JSONDecodeError):
        return None


def write_snapshot(report: AuditReport, archive_dir: Path | None = None) -> Path:
    d = archive_dir or paths.archive_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{report.run_id}.json"
    payload = {
        "run_id": report.run_id,
        "generated_at": report.generated_at,
        "window_start": report.window_start,
        "window_end": report.window_end,
        "platforms": [
            {
                "platform": pr.platform,
                "enabled": pr.enabled,
                "fetched": pr.fetched,
                "error": pr.error,
                "ga4_status": pr.ga4_status,
                "ga4_landing_status": pr.ga4_landing_status,
                "totals": pr.totals(),
                "campaigns": [c.to_dict() for c in pr.campaigns],
            }
            for pr in report.platforms
        ],
    }
    p.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return p


def _index_prev(prev: dict[str, Any] | None) -> dict[tuple[str, str], dict[str, Any]]:
    """Return {(platform, campaign_id): perf_dict} for the previous snapshot."""
    if not prev:
        return {}
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for plat in prev.get("platforms", []):
        for c in plat.get("campaigns", []):
            out[(c["platform"], c["campaign_id"])] = c
    return out


def compute_deltas(
    platforms: list[PlatformResult],
    prev: dict[str, Any] | None,
) -> list[CampaignDelta]:
    prev_idx = _index_prev(prev)
    out: list[CampaignDelta] = []
    for pr in platforms:
        for c in pr.campaigns:
            p = prev_idx.get((c.platform, c.campaign_id)) or {}
            spend_prev = p.get("spend") if p else None
            convs_prev = p.get("conversions") if p else None
            cpa_prev = (spend_prev / convs_prev) if (spend_prev and convs_prev) else None
            out.append(CampaignDelta(
                platform=c.platform,
                campaign_id=c.campaign_id,
                campaign_name=c.campaign_name,
                spend_now=c.spend,
                spend_prev=spend_prev,
                spend_change_pct=_safe_pct(c.spend, spend_prev),
                cpa_now=c.cpa,
                cpa_prev=cpa_prev,
                conversions_now=c.conversions,
                conversions_prev=convs_prev,
            ))
    return out


def top_movers(deltas: list[CampaignDelta], n: int = 3) -> tuple[list[CampaignDelta], list[CampaignDelta]]:
    """Best and worst movers by conversion change, with spend as tiebreaker.

    "Best" = biggest absolute conversion gain; "worst" = biggest absolute
    conversion loss (or biggest spend with zero conversions).
    """
    def score(d: CampaignDelta) -> float:
        prev = d.conversions_prev or 0
        return d.conversions_now - prev

    def waste(d: CampaignDelta) -> float:
        # Spend with zero conversions is unambiguous waste — sort by spend desc.
        return d.spend_now if d.conversions_now == 0 else 0

    sorted_by_gain = sorted(deltas, key=score, reverse=True)
    best = [d for d in sorted_by_gain if score(d) > 0][:n]

    sorted_by_loss = sorted(deltas, key=lambda d: (waste(d), -score(d)), reverse=True)
    worst = sorted_by_loss[:n]
    return best, worst


def _rule_based_recommendations(deltas: list[CampaignDelta]) -> list[str]:
    """Cheap, deterministic recommendations for Phase 1.

    Phase 2 swaps in LLM reasoning (Claude or Nemotron via OpenRouter) for
    nuanced suggestions; this baseline at least keeps the section non-empty
    while creds aren't wired.
    """
    recs: list[str] = []
    # Wasted spend
    waste = [d for d in deltas if d.conversions_now == 0 and d.spend_now > 5]
    waste.sort(key=lambda d: d.spend_now, reverse=True)
    for d in waste[:3]:
        recs.append(
            f"Pause {d.platform}:{d.campaign_name} — spent ${d.spend_now:.0f} with 0 conversions "
            f"in the last 7 days."
        )
    # Big spend drops with conversion gains → consider scaling
    for d in deltas:
        if (
            d.spend_change_pct is not None and d.spend_change_pct < -10
            and d.conversions_prev is not None and d.conversions_now > d.conversions_prev
        ):
            recs.append(
                f"Scale {d.platform}:{d.campaign_name} — spend down {d.spend_change_pct:.0f}% "
                f"and conversions up. Worth investigating bid changes."
            )
            if len(recs) >= 5:
                break
    return recs[:5]


def run_audit(
    enabled_platforms: set[str],
    archive_dir: Path | None = None,
) -> AuditReport:
    run_id = os.environ.get("ADLOOPS_RUN_ID") or dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    os.environ["ADLOOPS_RUN_ID"] = run_id  # so guardrails.log_decision picks it up

    platforms = fetch_all(enabled_platforms)
    prev = latest_archive(archive_dir)
    deltas = compute_deltas(platforms, prev)
    best, worst = top_movers(deltas)
    start, end = (
        platforms[0].campaigns[0].extra.get("window_start") if platforms and platforms[0].campaigns else None,
        platforms[0].campaigns[0].extra.get("window_end") if platforms and platforms[0].campaigns else None,
    )
    if not start or not end:
        end_d = dt.date.today() - dt.timedelta(days=1)
        start_d = end_d - dt.timedelta(days=6)
        start, end = start_d.isoformat(), end_d.isoformat()

    report = AuditReport(
        run_id=run_id,
        generated_at=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        window_start=start,
        window_end=end,
        platforms=platforms,
        deltas=deltas,
        top_movers_best=best,
        top_movers_worst=worst,
        actions_taken=[],
        pending_approvals=[],
        recommendations=_rule_based_recommendations(deltas),
    )
    return report
