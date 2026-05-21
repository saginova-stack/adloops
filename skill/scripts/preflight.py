"""Operator preflight — `python -m scripts.run --check`.

Catches the credential / config mistakes that would otherwise blow up on
the first cron run. Each check prints one line; the run exits zero if
everything's green and nonzero if anything's red so this can be wired
into a deploy gate.

What this checks:
  - brand.json loads and validates
  - Each enabled platform's read-client can instantiate (env-var only;
    a live API call would cost real quota and time)
  - GA4 enrichment client instantiates (downgrade: warning, not failure
    — enrichment is optional, the audit still runs without it)
  - Telegram: bot token + chat id are set AND the bot can actually
    reach the chat. This is the single most common first-run mistake
    (wrong chat_id, or bot not added to the chat), so it's worth a
    live GET — getMe + getChat, no message sent.

What this deliberately does NOT do:
  - Live API calls to Google Ads / Meta / LinkedIn. Each would need
    real quota; if the operator wants live validation they can run
    `python -m scripts.run --dry-run`, which exercises every read
    client end-to-end.
  - Validate the LLM keys (OPENROUTER_API_KEY / ANTHROPIC_API_KEY)
    individually — they're optional and the recommender already
    degrades silently.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from . import brand_loader
from .brand_loader import BrandConfigError
from .mcp_clients import (
    GoogleAdsClient,
    GoogleAnalyticsClient,
    LinkedInAdsClient,
    MetaAdsClient,
    MissingCredentialsError,
)


def _ok(line: str) -> None:
    print(f"  \033[32m[ok]\033[0m   {line}")


def _warn(line: str) -> None:
    print(f"  \033[33m[warn]\033[0m {line}")


def _fail(line: str) -> None:
    print(f"  \033[31m[FAIL]\033[0m {line}")


def _telegram_get(token: str, method: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    """One bot-API call. Raises on HTTP error or `ok: false` response.

    Pulled inline rather than reusing telegram_report.py's helper so
    preflight has zero risk of accidentally sending a message (this
    module only uses GET-style endpoints).
    """
    url = f"https://api.telegram.org/bot{token}/{method}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if not body.get("ok"):
        raise RuntimeError(f"telegram {method}: {body.get('description', body)}")
    return body.get("result") or {}


def run() -> int:
    """Return 0 if all checks pass, 1 otherwise."""
    print("AdLoops preflight\n")
    failures: list[str] = []

    # 1) brand.json
    brand = None
    try:
        brand = brand_loader.load_or_raise()
        _ok(
            f"brand.json: {brand.company_name} · "
            f"platforms enabled: {sorted(brand.enabled_platforms) or '(none)'}"
        )
    except BrandConfigError as e:
        _fail(f"brand.json: {e}")
        failures.append("brand.json")

    # If brand failed entirely, skip platform checks — they're conditional
    # on `guardrails.platforms` and we can't read it.
    enabled = brand.enabled_platforms if brand else {"google", "meta", "linkedin"}

    # 2) Per-platform env checks
    platform_checks = [
        ("google", GoogleAdsClient),
        ("meta", MetaAdsClient),
        ("linkedin", LinkedInAdsClient),
    ]
    for plat, cls in platform_checks:
        if plat not in enabled:
            _warn(f"{plat}: disabled in brand.json — skipped")
            continue
        try:
            cls()
            _ok(f"{plat}: env vars set (live API not tested — use --dry-run for that)")
        except MissingCredentialsError as e:
            _fail(f"{plat}: {e}")
            failures.append(plat)

    # 3) GA4 — optional enrichment, surface as warning not failure
    try:
        GoogleAnalyticsClient()
        _ok("ga4: env vars set (enrichment will run during the audit)")
    except MissingCredentialsError as e:
        _warn(f"ga4: {e} — enrichment will skip, audit still runs")

    # 4) Telegram — env + LIVE getMe + LIVE getChat
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("ADLOOPS_TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")
    if not token:
        _fail("telegram: TELEGRAM_BOT_TOKEN not set")
        failures.append("telegram")
    elif not chat_id:
        _fail("telegram: ADLOOPS_TELEGRAM_CHAT_ID (preferred) or TELEGRAM_CHAT_ID not set")
        failures.append("telegram")
    else:
        try:
            me = _telegram_get(token, "getMe")
            _telegram_get(token, "getChat", {"chat_id": chat_id})
            _ok(
                f"telegram: bot @{me.get('username', '?')} reachable, "
                f"chat {chat_id} accessible"
            )
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            _fail(f"telegram: HTTP {e.code} — {body}")
            failures.append("telegram")
        except urllib.error.URLError as e:
            _fail(f"telegram: transport error — {e.reason}")
            failures.append("telegram")
        except Exception as e:  # noqa: BLE001 — surface anything else explicitly
            _fail(f"telegram: {e}")
            failures.append("telegram")

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        print("Fix the [FAIL] lines above before running for real.")
        return 1
    print("All preflight checks passed. You're cleared to run.")
    return 0
