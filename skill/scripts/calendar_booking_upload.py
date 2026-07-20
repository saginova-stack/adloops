"""Upload Google Calendar appointment bookings as Google Ads offline conversions.

This is intentionally separate from the budget-mutation audit loop. Calendar
bookings are the high-intent conversion signal; budget changes should depend on
that signal only after this uploader has been verified.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Allow script-style execution: python skill/scripts/calendar_booking_upload.py
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scripts import paths  # type: ignore
else:
    from . import paths

DEFAULT_QUERY = "AI Numbers Game Demo"
DEFAULT_CONVERSION_NAME = "Calendar booking - offline upload"
DEFAULT_TOKEN_JSON = Path.home() / ".hermes" / "google_token.json"
STATE_RELATIVE_PATH = Path("conversions") / "calendar-booking-uploads.json"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
CALENDAR_API = "https://www.googleapis.com/calendar/v3"
DATAMANAGER_INGEST_URL = "https://datamanager.googleapis.com/v1/events:ingest"

_CLICK_ID_RE = re.compile(r"\b(gclid|gbraid|wbraid)=([^\s&#?]+)", re.IGNORECASE)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass
class Booking:
    calendar_id: str
    event_id: str
    summary: str
    created: str
    start: str
    html_link: str | None = None
    attendee_email: str | None = None
    gclid: str | None = None
    gbraid: str | None = None
    wbraid: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def state_key(self) -> str:
        return f"{self.calendar_id}:{self.event_id}"

    @property
    def has_upload_identifier(self) -> bool:
        return bool(self.gclid or self.gbraid or self.wbraid or self.attendee_email)


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse_rfc3339(value: str) -> dt.datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return dt.datetime.fromisoformat(value)


def _google_ads_datetime(value: str) -> str:
    parsed = _parse_rfc3339(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    # Google Ads expects "yyyy-mm-dd hh:mm:ss+|-hh:mm".
    return parsed.isoformat(timespec="seconds").replace("T", " ")


def _hash_email(email: str) -> str:
    normalized = email.strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class CalendarApi:
    def __init__(self, token_path: Path = DEFAULT_TOKEN_JSON):
        self.token_path = token_path
        self.access_token = self._refresh_access_token()

    def _refresh_access_token(self) -> str:
        token = _load_json(self.token_path)
        missing = [k for k in ("client_id", "client_secret", "refresh_token") if not token.get(k)]
        if missing:
            raise RuntimeError(f"Calendar token {self.token_path} missing {missing}")
        data = urllib.parse.urlencode({
            "client_id": token["client_id"],
            "client_secret": token["client_secret"],
            "refresh_token": token["refresh_token"],
            "grant_type": "refresh_token",
        }).encode("utf-8")
        try:
            with urllib.request.urlopen(GOOGLE_TOKEN_URL, data=data, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:  # pragma: no cover - defensive live error text
            raise RuntimeError(e.read().decode("utf-8", "replace")) from e
        return payload["access_token"]

    def get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        query = urllib.parse.urlencode(params or {})
        url = f"{CALENDAR_API}{path}"
        if query:
            url += "?" + query
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.access_token}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def calendar_ids(self) -> list[str]:
        out: list[str] = []
        page_token: str | None = None
        while True:
            params = {"maxResults": "250"}
            if page_token:
                params["pageToken"] = page_token
            payload = self.get("/users/me/calendarList", params)
            for item in payload.get("items", []):
                cal_id = item.get("id")
                summary = (item.get("summary") or "").lower()
                if not cal_id:
                    continue
                if cal_id.endswith("#holiday@group.v.calendar.google.com") or "holiday" in summary:
                    continue
                out.append(cal_id)
            page_token = payload.get("nextPageToken")
            if not page_token:
                return out

    def events(self, calendar_id: str, *, query: str, since: dt.datetime) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            params = {
                "timeMin": since.isoformat().replace("+00:00", "Z"),
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": "250",
                "q": query,
            }
            if page_token:
                params["pageToken"] = page_token
            payload = self.get(f"/calendars/{urllib.parse.quote(calendar_id, safe='')}/events", params)
            out.extend(payload.get("items", []))
            page_token = payload.get("nextPageToken")
            if not page_token:
                return out


def _extract_click_ids(event: dict[str, Any]) -> dict[str, str]:
    text_parts = [
        event.get("description") or "",
        event.get("location") or "",
        event.get("htmlLink") or "",
    ]
    source = event.get("source") or {}
    text_parts.append(source.get("url") or "")
    found: dict[str, str] = {}
    for text in text_parts:
        for key, value in _CLICK_ID_RE.findall(text):
            found[key.lower()] = urllib.parse.unquote(value)
    return found


def _attendee_email(event: dict[str, Any]) -> str | None:
    organizer = ((event.get("organizer") or {}).get("email") or "").lower()
    creator = ((event.get("creator") or {}).get("email") or "").lower()
    for attendee in event.get("attendees") or []:
        email = (attendee.get("email") or "").strip().lower()
        if not email or attendee.get("resource"):
            continue
        if email in {organizer, creator} or attendee.get("self"):
            continue
        if _EMAIL_RE.match(email):
            return email
    for fallback in (creator, organizer):
        if fallback and _EMAIL_RE.match(fallback) and not fallback.endswith("gserviceaccount.com"):
            return fallback
    return None


def booking_from_event(calendar_id: str, event: dict[str, Any], *, query: str) -> Booking | None:
    if event.get("status") == "cancelled":
        return None
    summary = event.get("summary") or ""
    if query.lower() not in summary.lower():
        return None
    created = event.get("created") or event.get("updated")
    start_obj = event.get("start") or {}
    start = start_obj.get("dateTime") or start_obj.get("date") or ""
    if not created or not start:
        return None
    click_ids = _extract_click_ids(event)
    return Booking(
        calendar_id=calendar_id,
        event_id=event["id"],
        summary=summary,
        created=created,
        start=start,
        html_link=event.get("htmlLink"),
        attendee_email=_attendee_email(event),
        gclid=click_ids.get("gclid"),
        gbraid=click_ids.get("gbraid"),
        wbraid=click_ids.get("wbraid"),
        raw=event,
    )


def find_bookings(
    api: CalendarApi,
    *,
    query: str,
    lookback_days: int,
    calendar_ids: list[str] | None = None,
) -> list[Booking]:
    since = _utc_now() - dt.timedelta(days=lookback_days)
    calendars = calendar_ids or api.calendar_ids()
    bookings: list[Booking] = []
    for calendar_id in calendars:
        try:
            events = api.events(calendar_id, query=query, since=since)
        except Exception as e:  # noqa: BLE001 - one inaccessible calendar should not abort all
            print(f"[calendar-bookings] skipped calendar {calendar_id}: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        for event in events:
            booking = booking_from_event(calendar_id, event, query=query)
            if booking:
                bookings.append(booking)
    bookings.sort(key=lambda b: b.created)
    return bookings


class GoogleAdsOfflineUploader:
    """Uploads bookings via the Data Manager API.

    Google Ads API v24 rejects new UploadClickConversions integrations with:
    "New integrations ... should use the Data Manager API." The Data Manager
    request still targets the Google Ads UPLOAD_CLICKS conversion action, but
    auth and payload shape are different.
    """

    REQUIRED_ENV = (
        "GOOGLE_ADS_DEVELOPER_TOKEN",
        "GOOGLE_ADS_CLIENT_ID",
        "GOOGLE_ADS_CLIENT_SECRET",
        "GOOGLE_ADS_REFRESH_TOKEN",
        "GOOGLE_ADS_LOGIN_CUSTOMER_ID",
        "GOOGLE_ADS_CUSTOMER_ID",
    )

    def __init__(self, *, conversion_action_name: str = DEFAULT_CONVERSION_NAME):
        missing = [k for k in self.REQUIRED_ENV if not os.environ.get(k)]
        if missing:
            raise RuntimeError(f"Google Ads missing env {missing}")
        from google.ads.googleads.client import GoogleAdsClient  # type: ignore

        cfg = {
            "developer_token": os.environ["GOOGLE_ADS_DEVELOPER_TOKEN"],
            "client_id": os.environ["GOOGLE_ADS_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_ADS_CLIENT_SECRET"],
            "refresh_token": os.environ["GOOGLE_ADS_REFRESH_TOKEN"],
            "login_customer_id": os.environ["GOOGLE_ADS_LOGIN_CUSTOMER_ID"],
            "use_proto_plus": True,
        }
        self.client = GoogleAdsClient.load_from_dict(cfg)
        self.customer_id = os.environ["GOOGLE_ADS_CUSTOMER_ID"]
        self.login_customer_id = os.environ["GOOGLE_ADS_LOGIN_CUSTOMER_ID"]
        self.conversion_action_name = conversion_action_name
        self.conversion_action_id = os.environ.get("ADLOOPS_BOOKING_CONVERSION_ACTION_ID") \
            or self._find_conversion_action_id(conversion_action_name)
        self.access_token = self._refresh_datamanager_token()

    def _find_conversion_action_id(self, name: str) -> str:
        ga = self.client.get_service("GoogleAdsService")
        safe_name = name.replace("'", "\\'")
        query = (
            "SELECT conversion_action.id, conversion_action.name "
            "FROM conversion_action "
            f"WHERE conversion_action.name = '{safe_name}' "
            "LIMIT 1"
        )
        rows = list(ga.search(customer_id=self.customer_id, query=query))
        if not rows:
            raise RuntimeError(f"Google Ads conversion action not found: {name!r}")
        return str(rows[0].conversion_action.id)

    def _refresh_datamanager_token(self) -> str:
        refresh_token = os.environ.get("GOOGLE_DATAMANAGER_REFRESH_TOKEN") or os.environ.get("GOOGLE_ADS_REFRESH_TOKEN")
        data = urllib.parse.urlencode({
            "client_id": os.environ["GOOGLE_ADS_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_ADS_CLIENT_SECRET"],
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
            "scope": "https://www.googleapis.com/auth/datamanager",
        }).encode("utf-8")
        try:
            with urllib.request.urlopen(GOOGLE_TOKEN_URL, data=data, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))["access_token"]
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            raise RuntimeError(
                "Data Manager OAuth token unavailable. Re-authorize the AdLoops Google OAuth client "
                "with https://www.googleapis.com/auth/datamanager, then set GOOGLE_DATAMANAGER_REFRESH_TOKEN "
                f"or update GOOGLE_ADS_REFRESH_TOKEN. Google returned: {body}"
            ) from e

    def _event(self, booking: Booking) -> dict[str, Any]:
        event: dict[str, Any] = {
            "destinationReferences": ["calendar-booking"],
            "transactionId": booking.state_key[:100],
            "eventTimestamp": _parse_rfc3339(booking.created).isoformat().replace("+00:00", "Z"),
            "currency": "EUR",
            "conversionValue": 1.0,
            "conversionCount": 1.0,
            "eventSource": "WEB",
        }
        if booking.attendee_email:
            event["userData"] = {
                "userIdentifiers": [
                    {"emailAddress": _hash_email(booking.attendee_email)}
                ]
            }
        ad_ids = {k: v for k, v in {
            "gclid": booking.gclid,
            "gbraid": booking.gbraid,
            "wbraid": booking.wbraid,
        }.items() if v}
        if ad_ids:
            event["adIdentifiers"] = ad_ids
        return event

    def upload(self, bookings: list[Booking], *, validate_only: bool = False) -> dict[str, Any]:
        if not bookings:
            return {"uploaded": 0, "results": []}
        body = {
            "destinations": [
                {
                    "reference": "calendar-booking",
                    "operatingAccount": {"accountId": self.customer_id, "accountType": "GOOGLE_ADS"},
                    "loginAccount": {"accountId": self.login_customer_id, "accountType": "GOOGLE_ADS"},
                    "productDestinationId": self.conversion_action_id,
                }
            ],
            "events": [self._event(b) for b in bookings],
            "encoding": "HEX",
            "validateOnly": validate_only,
        }
        req = urllib.request.Request(
            DATAMANAGER_INGEST_URL,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
                "login-account": f"accountTypes/GOOGLE_ADS/accounts/{self.login_customer_id}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            return {
                "uploaded": 0,
                "validate_only": validate_only,
                "partial_failure_error": f"HTTP {e.code}: {body}",
                "results": [],
            }
        return {
            "uploaded": len(bookings),
            "validate_only": validate_only,
            "request_id": payload.get("requestId"),
            "partial_failure_error": None,
            "results": payload,
        }


def run(
    *,
    dry_run: bool,
    validate_only: bool,
    query: str,
    lookback_days: int,
    calendar_ids: list[str] | None,
    token_path: Path,
    state_path: Path,
    conversion_action_name: str,
) -> int:
    state = _load_json(state_path)
    uploaded_keys = set(state.get("uploaded", {}).keys())
    api = CalendarApi(token_path)
    bookings = find_bookings(api, query=query, lookback_days=lookback_days, calendar_ids=calendar_ids)
    candidates = [b for b in bookings if b.state_key not in uploaded_keys]
    uploadable = [b for b in candidates if b.has_upload_identifier]
    skipped = [b for b in candidates if not b.has_upload_identifier]

    print(json.dumps({
        "query": query,
        "lookback_days": lookback_days,
        "calendars": calendar_ids or "auto",
        "found_bookings": len(bookings),
        "new_candidates": len(candidates),
        "uploadable": len(uploadable),
        "skipped_no_identifier": len(skipped),
        "dry_run": dry_run,
        "validate_only": validate_only,
    }, indent=2))

    for booking in skipped:
        print(f"SKIP no upload identifier: {booking.created} {booking.summary} {booking.state_key}", file=sys.stderr)

    if dry_run:
        for booking in uploadable:
            print(f"DRY would upload: {booking.created} {booking.attendee_email or '(click-id)'} {booking.summary}")
        return 0

    uploader = GoogleAdsOfflineUploader(conversion_action_name=conversion_action_name)
    result = uploader.upload(uploadable, validate_only=validate_only)
    print(json.dumps(result, indent=2, default=str))

    if not validate_only and uploadable and not result.get("partial_failure_error"):
        uploaded = state.setdefault("uploaded", {})
        now = _utc_now().isoformat(timespec="seconds")
        for booking in uploadable:
            uploaded[booking.state_key] = {
                "uploaded_at": now,
                "created": booking.created,
                "start": booking.start,
                "summary": booking.summary,
                "attendee_email_sha256": _hash_email(booking.attendee_email) if booking.attendee_email else None,
                "had_click_id": bool(booking.gclid or booking.gbraid or booking.wbraid),
            }
        _write_json(state_path, state)
    return 0 if not result.get("partial_failure_error") else 12


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Upload Google Calendar bookings to Google Ads offline conversions.")
    parser.add_argument("--dry-run", action="store_true", help="Scan calendars and print candidates; do not call Google Ads upload.")
    parser.add_argument("--validate-only", action="store_true", help="Call Google Ads upload with validate_only=True; do not mark state uploaded.")
    parser.add_argument("--query", default=os.environ.get("ADLOOPS_CALENDAR_BOOKING_QUERY", DEFAULT_QUERY))
    parser.add_argument("--lookback-days", type=int, default=int(os.environ.get("ADLOOPS_CALENDAR_LOOKBACK_DAYS", "30")))
    parser.add_argument("--calendar-id", action="append", dest="calendar_ids", help="Calendar ID to scan; repeatable. Defaults to accessible non-holiday calendars.")
    parser.add_argument("--token-json", type=Path, default=Path(os.environ.get("GOOGLE_CALENDAR_TOKEN_JSON", str(DEFAULT_TOKEN_JSON))))
    parser.add_argument("--state", type=Path, default=paths.brand_dir() / STATE_RELATIVE_PATH)
    parser.add_argument("--conversion-action-name", default=os.environ.get("ADLOOPS_BOOKING_CONVERSION_ACTION", DEFAULT_CONVERSION_NAME))
    args = parser.parse_args(argv)

    if args.lookback_days < 1:
        parser.error("--lookback-days must be >= 1")
    return run(
        dry_run=args.dry_run,
        validate_only=args.validate_only,
        query=args.query,
        lookback_days=args.lookback_days,
        calendar_ids=args.calendar_ids,
        token_path=args.token_json,
        state_path=args.state,
        conversion_action_name=args.conversion_action_name,
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
