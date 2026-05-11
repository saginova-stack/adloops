"""Recommendations chain for the Telegram report.

Picks the best available recommender at call time:

  1. OpenRouter with Nemotron 3 Super (free tier) — if `OPENROUTER_API_KEY`
     is set. The free-tier model is plenty for this prompt size.
  2. Anthropic Claude Haiku — if `ANTHROPIC_API_KEY` is set. Cheap and
     fast, fits in our cron budget.
  3. The deterministic rule-based fallback in `audit._rule_based_recommendations`.
     Always available; used when no LLM key is present *or* when the LLM
     call errors (we'd rather degrade than blank the section).

The prompt is intentionally token-conservative: we pass only the audit
deltas, the cross-reference flags, the brand voice, and 7 metric totals.
A whole report's worth of campaigns + GA4 rows would balloon to multi-k
tokens; this stays under ~1k input on a typical run.

Output is a list of plain-text recommendation lines (no markdown), capped
at 5 — the report renderer prefixes each with a bullet itself.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .audit import AuditReport
    from .brand_loader import Brand


MAX_RECS = 5
LLM_TIMEOUT_S = 30


# ---- public entry point ---------------------------------------------------

def recommendations(report: "AuditReport", brand: "Brand") -> list[str]:
    """Return up to MAX_RECS plain-text recommendations.

    Wraps every LLM call in a broad try/except so a 5xx from OpenRouter
    or a rate-limit on Anthropic doesn't drop the report's recommendation
    section — we fall through to the deterministic rules.
    """
    if os.environ.get("OPENROUTER_API_KEY"):
        try:
            recs = _via_openrouter(report, brand)
            if recs:
                return recs[:MAX_RECS]
        except Exception:  # noqa: BLE001 — never crash the run for an LLM hiccup
            pass
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            recs = _via_anthropic(report, brand)
            if recs:
                return recs[:MAX_RECS]
        except Exception:  # noqa: BLE001
            pass
    return _fallback(report)


# ---- LLM clients ----------------------------------------------------------

def _via_openrouter(report: "AuditReport", brand: "Brand") -> list[str]:
    prompt = _build_prompt(report, brand)
    body = json.dumps({
        "model": "nvidia/nemotron-3-super-120b-a12b:free",
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 800,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/saginova/adloops",
            "X-Title": "AdLoops",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=LLM_TIMEOUT_S) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    text = data["choices"][0]["message"]["content"]
    return _parse_recs(text)


def _via_anthropic(report: "AuditReport", brand: "Brand") -> list[str]:
    prompt = _build_prompt(report, brand)
    body = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 800,
        "system": _SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=LLM_TIMEOUT_S) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    text = data["content"][0]["text"]
    return _parse_recs(text)


def _fallback(report: "AuditReport") -> list[str]:
    # Import here to avoid a circular import at module load time
    # (audit imports mcp_clients; recommender is imported from run.py).
    from .audit import _rule_based_recommendations
    return _rule_based_recommendations(report.deltas)


# ---- prompt construction -------------------------------------------------

_SYSTEM_PROMPT = (
    "You are an expert paid-ads optimizer reviewing a 7-day campaign audit. "
    "Output 3 to 5 concrete, action-oriented recommendations the operator "
    "can take in the next 48 hours. Each recommendation is one line, no "
    "preamble, no markdown bullets — just the action and the data that "
    "justifies it. Examples: "
    "'Pause google:Brand-EU — $300 spend with 0 conversions in last 7d.' "
    "'Investigate landing page for meta:US-Retargeting — 41% consent gap.' "
    "Skip empty pleasantries."
)


def _build_prompt(report: "AuditReport", brand: "Brand") -> str:
    """Render a compact, LLM-friendly view of the audit. ~600 tokens budget.

    Includes:
      - brand voice for tone matching
      - per-platform totals (one line each)
      - top movers (best 3, worst 3) with concrete numbers
      - tracking & consent flags (Google only)
      - top wasted-spend campaigns
    """
    lines: list[str] = []
    lines.append(f"# Brand: {brand.company_name}")
    voice = brand.raw.get("brandVoice", {})
    if isinstance(voice, dict):
        tone = voice.get("tone")
        if tone:
            lines.append(f"# Voice: {tone}")
    lines.append(f"# Window: {report.window_start} → {report.window_end}")
    lines.append("")

    lines.append("## Platform totals")
    for pr in report.platforms:
        if not pr.fetched:
            continue
        t = pr.totals()
        lines.append(
            f"- {pr.platform}: spend ${t['spend']:.0f}, conv {t['conversions']:.0f}, "
            f"CPA {('$%.2f' % t['cpa']) if t['cpa'] else '—'}, "
            f"ROAS {('%.2fx' % t['roas']) if t['roas'] else '—'}"
        )
    lines.append("")

    if report.top_movers_best:
        lines.append("## Top movers (best)")
        for d in report.top_movers_best:
            lines.append(
                f"- [{d.platform}] {d.campaign_name}: spend ${d.spend_now:.0f} "
                f"({(d.spend_change_pct or 0):+.0f}%), conv {d.conversions_now:.0f} "
                f"(was {d.conversions_prev or 0:.0f})"
            )
        lines.append("")
    if report.top_movers_worst:
        lines.append("## Top movers (worst)")
        for d in report.top_movers_worst:
            lines.append(
                f"- [{d.platform}] {d.campaign_name}: spend ${d.spend_now:.0f}, "
                f"conv {d.conversions_now:.0f}"
            )
        lines.append("")

    # Tracking & consent — surface the cross-reference flags
    flagged_lines: list[str] = []
    for pr in report.platforms:
        if pr.platform != "google" or not pr.fetched:
            continue
        for c in pr.campaigns:
            gap = c.consent_gap_pct
            attr = c.attribution_gap_pct
            issues = []
            if gap is not None and gap >= 30:
                issues.append(f"consent gap {gap:.0f}%")
            if attr is not None and attr >= 25:
                issues.append(f"attribution gap {attr:.0f}%")
            if issues:
                flagged_lines.append(
                    f"- [google] {c.campaign_name}: {', '.join(issues)}"
                )
    if flagged_lines:
        lines.append("## Tracking & consent flags")
        lines.extend(flagged_lines)
        lines.append("")

    lines.append(
        "Return 3-5 recommendations, one per line, each grounded in the numbers above."
    )
    return "\n".join(lines)


def _parse_recs(text: str) -> list[str]:
    """Split LLM output into a list of plain recommendation lines.

    Strips markdown bullets (`-`, `*`, `1.`), trims, drops empty lines and
    obvious meta lines ("Here are 5 recommendations:"), and caps at MAX_RECS.
    """
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        # Strip common bullet prefixes
        for prefix in ("- ", "* ", "• "):
            if line.startswith(prefix):
                line = line[len(prefix):]
                break
        # numbered list: "1. " / "1) "
        if len(line) >= 3 and line[0].isdigit() and line[1] in ".)" and line[2] == " ":
            line = line[3:]
        elif len(line) >= 4 and line[:2].isdigit() and line[2] in ".)" and line[3] == " ":
            line = line[4:]
        # Skip headers / meta intros
        low = line.lower()
        if low.endswith(":") and len(low) < 60:
            continue
        if low.startswith("here are") or low.startswith("recommendations"):
            continue
        out.append(line)
        if len(out) >= MAX_RECS:
            break
    return out
