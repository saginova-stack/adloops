import datetime as dt
import json

from scripts import calendar_booking_upload as cbu


def test_booking_from_event_extracts_attendee_and_click_ids():
    event = {
        "id": "evt1",
        "status": "confirmed",
        "summary": "AI Numbers Game Demo",
        "created": "2026-07-20T08:00:00Z",
        "start": {"dateTime": "2026-07-21T10:00:00+02:00"},
        "description": "Booked from https://ai.numbersgame.xyz/?gclid=TEST-CLICK",
        "organizer": {"email": "assistantboba@gmail.com", "self": True},
        "creator": {"email": "assistantboba@gmail.com", "self": True},
        "attendees": [
            {"email": "assistantboba@gmail.com", "self": True},
            {"email": "Prospect@Example.COM"},
        ],
    }

    booking = cbu.booking_from_event("primary", event, query="AI Numbers Game Demo")

    assert booking is not None
    assert booking.attendee_email == "prospect@example.com"
    assert booking.gclid == "TEST-CLICK"
    assert booking.state_key == "primary:evt1"


def test_booking_from_event_ignores_non_matching_summary():
    event = {
        "id": "evt1",
        "summary": "Internal demo",
        "created": "2026-07-20T08:00:00Z",
        "start": {"dateTime": "2026-07-21T10:00:00+02:00"},
    }

    assert cbu.booking_from_event("primary", event, query="AI Numbers Game Demo") is None


def test_run_dry_run_does_not_write_state(tmp_path, monkeypatch, capsys):
    booking = cbu.Booking(
        calendar_id="primary",
        event_id="evt1",
        summary="AI Numbers Game Demo",
        created="2026-07-20T08:00:00Z",
        start="2026-07-21T10:00:00+02:00",
        attendee_email="prospect@example.com",
    )

    monkeypatch.setattr(cbu, "CalendarApi", lambda token_path: object())
    monkeypatch.setattr(cbu, "find_bookings", lambda *a, **k: [booking])

    state = tmp_path / "state.json"
    rc = cbu.run(
        dry_run=True,
        validate_only=False,
        query="AI Numbers Game Demo",
        lookback_days=30,
        calendar_ids=None,
        token_path=tmp_path / "token.json",
        state_path=state,
        conversion_action_name="Calendar booking - offline upload",
    )

    assert rc == 0
    assert not state.exists()
    assert "DRY would upload" in capsys.readouterr().out


def test_run_live_upload_writes_dedupe_state(tmp_path, monkeypatch):
    booking = cbu.Booking(
        calendar_id="primary",
        event_id="evt1",
        summary="AI Numbers Game Demo",
        created="2026-07-20T08:00:00Z",
        start="2026-07-21T10:00:00+02:00",
        attendee_email="prospect@example.com",
    )
    uploads = []

    class FakeUploader:
        def __init__(self, *, conversion_action_name):
            assert conversion_action_name == "Calendar booking - offline upload"

        def upload(self, bookings, *, validate_only=False):
            uploads.extend(bookings)
            return {"uploaded": len(bookings), "partial_failure_error": None, "results": ["ok"]}

    monkeypatch.setattr(cbu, "CalendarApi", lambda token_path: object())
    monkeypatch.setattr(cbu, "find_bookings", lambda *a, **k: [booking])
    monkeypatch.setattr(cbu, "GoogleAdsOfflineUploader", FakeUploader)
    monkeypatch.setattr(cbu, "_utc_now", lambda: dt.datetime(2026, 7, 20, 9, 0, tzinfo=dt.timezone.utc))

    state = tmp_path / "state.json"
    rc = cbu.run(
        dry_run=False,
        validate_only=False,
        query="AI Numbers Game Demo",
        lookback_days=30,
        calendar_ids=None,
        token_path=tmp_path / "token.json",
        state_path=state,
        conversion_action_name="Calendar booking - offline upload",
    )

    assert rc == 0
    assert uploads == [booking]
    payload = json.loads(state.read_text())
    assert "primary:evt1" in payload["uploaded"]

    uploads.clear()
    rc = cbu.run(
        dry_run=False,
        validate_only=False,
        query="AI Numbers Game Demo",
        lookback_days=30,
        calendar_ids=None,
        token_path=tmp_path / "token.json",
        state_path=state,
        conversion_action_name="Calendar booking - offline upload",
    )
    assert rc == 0
    assert uploads == []
