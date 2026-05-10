"""Render an AuditReport into the spec'd Telegram message format and send it.

Reads the existing OpenClaw Telegram bot token from `TELEGRAM_BOT_TOKEN`. The
chat id can come from `ADLOOPS_TELEGRAM_CHAT_ID` (preferred — keeps adloops
isolated from other OC routing) or fall back to `TELEGRAM_CHAT_ID`.

Spec format per run:
1. Header — date, run #, platforms reached
2. Headline metrics — spend / impressions / clicks / conversions / CPA / ROAS, deltas
3. Top movers — 3 best, 3 worst with concrete numbers
4. Actions taken — every auto-mutation with $ impact (P1: empty)
5. Pending approvals — anything blocked by guardrails (P1: empty)
6. Recommendations — 3-5 things suggested but not auto-executed

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

    # 4) Actions taken
    if r.actions_taken:
        lines = ["Actions taken:"]
        for a in r.actions_taken:
            lines.append(
                f"  • [{_platform_emoji(a['platform'])}] {a['campaign_name']}: "
                f"{a['kind']} — {a.get('explanation', '')}"
            )
        sections.append("\n".join(lines))
    else:
        sections.append("Actions taken: none (read-only run)")

    # 5) Pending approvals
    if r.pending_approvals:
        lines = ["Pending approvals:"]
        for a in r.pending_approvals:
            lines.append(
                f"  • [{_platform_emoji(a['platform'])}] {a['campaign_name']}: "
                f"{a['kind']} — {a.get('explanation', '')} (run {r.run_id})"
            )
        sections.append("\n".join(lines))

    # 6) Recommendations
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
