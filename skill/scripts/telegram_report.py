"""Render an AuditReport into the spec'd Telegram message format and send it.

Reads the existing OpenClaw Telegram bot token from `TELEGRAM_BOT_TOKEN`. The
chat id can come from `ADLOOPS_TELEGRAM_CHAT_ID` (preferred — keeps adloops
isolated from other OC routing) or fall back to `TELEGRAM_CHAT_ID`.

Spec format per run:
1. Header — date, run #, platforms reached
2. Headline metrics — spend / impressions / clicks / conversions / CPA / ROAS, deltas
3. Top movers — 3 best, 3 worst with concrete numbers
4. Tracking & consent — consent gap / attribution gap flags from GA4 cross-reference (Google Ads only), plus paid landing pages with 0 conversions
5. Actions taken — every auto-mutation with $ impact (P1: empty).
   Under --no-mutate this slot is replaced by "Would have fired"
   showing every proposal and the guardrail verdict it would have
   received, so the operator can build trust during the observation
   weeks before going live.
6. Pending approvals — anything blocked by guardrails (P1: empty)
7. Recommendations — 3-5 things suggested but not auto-executed

Cap at ~3000 chars/message and split if needed.
"""

from __future__ import annotations

import json
import os
import textwrap
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterable

from .audit import AuditReport, CampaignDelta, PlatformResult

MAX_MSG = 3000  # well under Telegram's 4096 hard limit


# ---------------- formatting ----------------

def _fmt_money(v: float | None) -> str:
    if v is None:
        return "—"
    return f"${v:,.2f}" if abs(v) < 1_000_000 else f"${v:,.0f}"


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:+.1f}%"


def _fmt_int(v: int | None) -> str:
    return f"{v:,}" if v is not None else "—"


def _platform_emoji(p: str) -> str:
    return {"google": "G", "meta": "M", "linkedin": "L"}.get(p, "?")


def _platform_summary_line(pr: PlatformResult) -> str:
    if not pr.enabled:
        return f"  {_platform_emoji(pr.platform)} {pr.platform}: disabled"
    if not pr.fetched:
        return f"  {_platform_emoji(pr.platform)} {pr.platform}: ERROR — {pr.error}"
    t = pr.totals()
    cpa = _fmt_money(t["cpa"]) if t["cpa"] else "—"
    roas = f"{t['roas']:.2f}x" if t["roas"] else "—"
    return (
        f"  {_platform_emoji(pr.platform)} {pr.platform}: "
        f"{_fmt_money(t['spend'])} spend · {_fmt_int(t['impressions'])} imp · "
        f"{_fmt_int(t['clicks'])} clicks · {t['conversions']:.0f} conv · "
        f"CPA {cpa} · ROAS {roas}"
    )


def _delta_line(d: CampaignDelta) -> str:
    spend_change = _fmt_pct(d.spend_change_pct)
    conv = (
        f"{d.conversions_now:.0f} conv"
        if d.conversions_prev is None
        else f"{d.conversions_now:.0f} conv (was {d.conversions_prev:.0f})"
    )
    cpa = f"CPA {_fmt_money(d.cpa_now)}" if d.cpa_now else "CPA —"
    return (
        f"  • [{_platform_emoji(d.platform)}] {_truncate(d.campaign_name, 40)}: "
        f"{_fmt_money(d.spend_now)} ({spend_change}), {conv}, {cpa}"
    )


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _action_line(a: dict) -> str:
    """Format one row from `actions_taken`. The dict is the output of
    `guardrails.serialize_result()` augmented with `applied` / `dry_run` /
    `result` / `error` by the run.py mutation pipeline.
    """
    mut = a.get("mutation", {})
    plat = mut.get("platform", "?")
    name = mut.get("campaign_name", mut.get("campaign_id", "?"))
    kind = mut.get("kind", "?")
    if a.get("error"):
        suffix = f"FAILED — {a['error']}"
    elif a.get("dry_run"):
        suffix = "preview only (dry-run)"
    elif a.get("applied"):
        suffix = "applied"
    else:
        suffix = a.get("explanation", "")
    # Include the change body for budget_change / status flips so the operator
    # sees what actually moved.
    detail = _action_detail(mut)
    detail_str = f" — {detail}" if detail else ""
    return f"  • [{_platform_emoji(plat)}] {name}: {kind}{detail_str} ({suffix})"


def _preview_line(a: dict) -> str:
    """Format one row from `previewed_actions` (--no-mutate output).

    Same dict shape as actions_taken/pending_approvals — the output of
    `guardrails.serialize_result()`. We surface the would-be decision
    (AUTO/APPROVAL/REJECTED) so the operator can see how the proposer
    AND guardrails would have interacted, not just what got proposed.
    """
    mut = a.get("mutation", {})
    plat = mut.get("platform", "?")
    name = mut.get("campaign_name", mut.get("campaign_id", "?"))
    kind = mut.get("kind", "?")
    decision = a.get("decision", "?")
    explanation = a.get("explanation", "")
    detail = _action_detail(mut)
    detail_str = f" — {detail}" if detail else ""
    return (
        f"  • [{_platform_emoji(plat)}] {name}: {kind}{detail_str} "
        f"[{decision.upper()}] {explanation}"
    )


def _action_detail(mut: dict) -> str:
    kind = mut.get("kind")
    before = mut.get("before", {}) or {}
    after = mut.get("after", {}) or {}
    if kind == "budget_change":
        b = before.get("daily_budget")
        a = after.get("daily_budget")
        return f"daily_budget {_fmt_money(b) if b is not None else '—'} → {_fmt_money(a) if a is not None else '—'}"
    if kind in ("pause", "enable"):
        return f"{before.get('status', '?')} → {after.get('status', '?')}"
    return ""


# Thresholds for surfacing cross-reference flags. Tuned to match the kLOsk
# adloop tool defaults: consent gaps above ~30% are typically GDPR-driven
# (normal EU traffic floor), attribution gaps above 25% usually mean either
# a broken conversion event or a long attribution window.
CONSENT_GAP_FLAG_PCT = 30.0
ATTRIBUTION_GAP_FLAG_PCT = 25.0
REAL_CPA_DRIFT_FLAG_PCT = 25.0
# Landing-page flag: a paid landing page worth surfacing has enough paid
# traffic to matter but recorded no GA4 conversions in the window. Capped
# globally so a long-tail of low-volume pages can't crowd out the message.
MIN_PAID_SESSIONS_FOR_LANDING_FLAG = 20
MAX_LANDING_FLAGS = 5


def _prev_campaign_lookup(prev: dict | None) -> dict[tuple[str, str], dict]:
    """Index prior snapshot's campaign rows by (platform, campaign_id)."""
    if not prev:
        return {}
    out: dict[tuple[str, str], dict] = {}
    for plat in prev.get("platforms", []):
        for c in plat.get("campaigns", []):
            out[(c["platform"], c["campaign_id"])] = c
    return out


def _consent_gap_from_raw(clicks: int | None, sessions: int | None) -> float | None:
    """Same formula as CampaignPerf.consent_gap_pct, applied to raw dict
    fields from a snapshot. Returns None when the inputs aren't available."""
    if clicks is None or sessions is None or clicks == 0:
        return None
    gap = clicks - sessions
    if gap <= 0:
        return 0.0
    return (gap / clicks) * 100.0


def _attribution_gap_from_raw(conv: float | None, ga4_conv: float | None) -> float | None:
    if conv is None or ga4_conv is None or conv == 0:
        return None
    return (abs(conv - ga4_conv) / conv) * 100.0


def _trend_suffix(current: float | None, previous: float | None) -> str:
    """Compose ' (Xpp w/w)' for a percentage-point delta. Returns '' when
    the delta is unavailable or tiny (<1pp — noise threshold)."""
    if current is None or previous is None:
        return ""
    delta = current - previous
    if abs(delta) < 1.0:
        return ""
    sign = "+" if delta > 0 else ""
    return f" ({sign}{delta:.0f}pp w/w)"


def _tracking_flags(r: AuditReport) -> list[str]:
    """Return one human-readable line per Google Ads campaign that crossed
    a cross-reference threshold worth surfacing in the report.
    """
    flags: list[str] = []
    prev_idx = _prev_campaign_lookup(r.prev_snapshot)
    for pr in r.platforms:
        if pr.platform != "google" or not pr.fetched:
            continue
        for c in pr.campaigns:
            name = _truncate(c.campaign_name, 40)
            prev_row = prev_idx.get((c.platform, c.campaign_id))
            gap = c.consent_gap_pct
            if gap is not None and gap >= CONSENT_GAP_FLAG_PCT:
                prev_gap = _consent_gap_from_raw(
                    prev_row.get("clicks") if prev_row else None,
                    prev_row.get("ga4_sessions") if prev_row else None,
                )
                trend = _trend_suffix(gap, prev_gap)
                flags.append(
                    f"  ⚠ {name}: {gap:.0f}% consent gap{trend} "
                    f"({c.clicks} Ads clicks → {c.ga4_sessions} GA4 sessions). "
                    f"Likely GDPR consent rejection or broken tracking."
                )
            attr = c.attribution_gap_pct
            if attr is not None and attr >= ATTRIBUTION_GAP_FLAG_PCT:
                prev_attr = _attribution_gap_from_raw(
                    prev_row.get("conversions") if prev_row else None,
                    prev_row.get("ga4_conversions") if prev_row else None,
                )
                trend = _trend_suffix(attr, prev_attr)
                flags.append(
                    f"  ⚠ {name}: {attr:.0f}% attribution gap{trend} "
                    f"(Ads: {c.conversions:.0f} conv vs GA4: {c.ga4_conversions:.0f} conv). "
                    f"Check attribution window and conversion event wiring."
                )
            real = c.real_cpa
            reported = c.cpa
            if (
                real is not None and reported is not None and reported > 0
                and abs(real - reported) / reported * 100 >= REAL_CPA_DRIFT_FLAG_PCT
            ):
                direction = "higher" if real > reported else "lower"
                flags.append(
                    f"  ⚠ {name}: real CPA {_fmt_money(real)} vs reported {_fmt_money(reported)} "
                    f"({direction} via GA4). Investigate conversion events."
                )
    return flags


def _landing_page_flags(r: AuditReport) -> list[str]:
    """One line per (campaign, landing page) pair with material paid
    traffic and zero GA4 conversions. Top-N by sessions desc so the
    message stays bounded when many pages qualify.
    """
    candidates: list[tuple[int, str]] = []  # (sessions, line)
    for pr in r.platforms:
        if pr.platform != "google" or not pr.fetched:
            continue
        for c in pr.campaigns:
            if not c.ga4_landing_pages:
                continue
            name = _truncate(c.campaign_name, 40)
            for lp in c.ga4_landing_pages:
                if lp.sessions < MIN_PAID_SESSIONS_FOR_LANDING_FLAG:
                    continue
                if lp.conversions > 0:
                    continue
                page = _truncate(lp.page_path, 50)
                line = (
                    f"  ⚠ {name}: landing {page} — "
                    f"{lp.sessions} paid sessions, 0 conversions."
                )
                candidates.append((lp.sessions, line))
    candidates.sort(reverse=True)
    return [line for _, line in candidates[:MAX_LANDING_FLAGS]]


def render_report(r: AuditReport) -> list[str]:
    """Build the message body, possibly split into chunks ≤ MAX_MSG chars."""
    reached = [pr.platform for pr in r.platforms if pr.fetched]
    sections: list[str] = []

    # 1) Header
    sections.append(textwrap.dedent(f"""\
        AdLoops · {r.window_start} → {r.window_end}
        Run {r.run_id} · platforms: {', '.join(reached) or 'none'}""").strip())

    # 2) Headline metrics (per platform)
    metrics_lines = ["Headline metrics:"] + [_platform_summary_line(pr) for pr in r.platforms]
    sections.append("\n".join(metrics_lines))

    # 3) Top movers
    if r.top_movers_best or r.top_movers_worst:
        movers_lines = ["Top movers:"]
        if r.top_movers_best:
            movers_lines.append("  Best:")
            movers_lines.extend(_delta_line(d) for d in r.top_movers_best)
        if r.top_movers_worst:
            movers_lines.append("  Worst:")
            movers_lines.extend(_delta_line(d) for d in r.top_movers_worst)
        sections.append("\n".join(movers_lines))

    # 4) Tracking & consent (GA4 cross-reference)
    google_pr = next((pr for pr in r.platforms if pr.platform == "google"), None)
    flags = _tracking_flags(r)
    landing_flags = _landing_page_flags(r)
    if flags or landing_flags:
        lines = ["Tracking & consent:"] + flags
        if landing_flags:
            lines.append("  Landing pages with paid traffic, 0 conversions:")
            lines.extend(landing_flags)
        sections.append("\n".join(lines))
    elif google_pr and google_pr.fetched and google_pr.ga4_status and google_pr.ga4_status != "ok":
        # Surface why we couldn't compute cross-reference — operator visibility.
        sections.append(f"Tracking & consent: GA4 enrichment {google_pr.ga4_status}")

    # 5) Actions taken — OR previewed actions when running --no-mutate
    if r.previewed_actions:
        lines = ["Would have fired (--no-mutate observation):"]
        for a in r.previewed_actions:
            lines.append(_preview_line(a))
        sections.append("\n".join(lines))
    elif r.actions_taken:
        lines = ["Actions taken:"]
        for a in r.actions_taken:
            lines.append(_action_line(a))
        sections.append("\n".join(lines))
    else:
        sections.append("Actions taken: none (no mutations proposed this run)")

    # 6) Pending approvals
    if r.pending_approvals:
        lines = ["Pending approvals:"]
        for a in r.pending_approvals:
            mut = a.get("mutation", {})
            plat = mut.get("platform", "?")
            name = mut.get("campaign_name", mut.get("campaign_id", "?"))
            kind = mut.get("kind", "?")
            explanation = a.get("explanation", "")
            lines.append(
                f"  • [{_platform_emoji(plat)}] {name}: {kind} — {explanation} "
                f"(approve with: python -m scripts.run --approve {r.run_id}:"
                f"{r.pending_approvals.index(a)})"
            )
        sections.append("\n".join(lines))

    # 7) Recommendations
    if r.recommendations:
        lines = ["Recommendations:"] + [f"  • {rec}" for rec in r.recommendations]
        sections.append("\n".join(lines))

    return _chunk(sections, MAX_MSG)


def _chunk(sections: list[str], max_chars: int) -> list[str]:
    """Combine sections into messages ≤ max_chars. Sections are kept atomic
    when possible — split a section internally only if it alone exceeds the cap.
    """
    chunks: list[str] = []
    cur = ""
    for s in sections:
        if len(s) > max_chars:
            # split a giant section by lines
            if cur:
                chunks.append(cur)
                cur = ""
            buf = ""
            for line in s.split("\n"):
                add = (line if not buf else "\n" + line)
                if len(buf) + len(add) > max_chars:
                    chunks.append(buf)
                    buf = line
                else:
                    buf += add
            if buf:
                cur = buf
            continue
        candidate = s if not cur else cur + "\n\n" + s
        if len(candidate) > max_chars:
            chunks.append(cur)
            cur = s
        else:
            cur = candidate
    if cur:
        chunks.append(cur)
    return chunks


# ---------------- send ----------------

class TelegramConfigError(RuntimeError):
    pass


def _resolve_chat_id() -> str:
    chat = os.environ.get("ADLOOPS_TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")
    if not chat:
        raise TelegramConfigError(
            "No chat id. Set ADLOOPS_TELEGRAM_CHAT_ID (recommended) or TELEGRAM_CHAT_ID."
        )
    return chat


def send_messages(messages: Iterable[str], *, dry_run: bool = False) -> list[dict]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise TelegramConfigError("TELEGRAM_BOT_TOKEN not set — re-using OC's bot token is required.")
    chat_id = _resolve_chat_id()

    sent: list[dict] = []
    for body in messages:
        if dry_run:
            sent.append({"dry_run": True, "len": len(body), "preview": body[:200]})
            continue
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": body,
            "disable_web_page_preview": "true",
        }).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                sent.append(json.loads(resp.read().decode("utf-8")))
        except urllib.error.HTTPError as e:
            raise RuntimeError(
                f"Telegram HTTP {e.code}: {e.read().decode('utf-8', 'replace')}"
            ) from e
    return sent
