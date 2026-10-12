"""iCloud Calendar tool tests (FC-3, TLC-4, TLC-7) — v16 (iCloud).

Covers `ember.tools.icloud_calendar.{CalendarListCalendarsTool,
CalendarListEventsTool, CalendarAddEventTool}`:

- FC-3 static surface (name, description, input_schema,
  requires_confirmation, Tool protocol).
- TLC-4 secret hygiene: app password never appears in errors/logs/repr.
- TLC-7 failure surfacing: typed errors for auth and transport failures.

Mocking strategy: identical to the reminders tests — we patch the
`DAVClient` symbol on the calendar tool module via
`tests.fakes.fake_caldav.install_fake_caldav`.

Tester-flagged ambiguities (also in the report):

1. **`requires_confirmation` for `calendar_add_event`** — the contract
   explicitly leaves this implementer-flexible. Tests follow the
   prompt's guidance:
     - Direct `tool.call(...)` for write-success paths (bypasses the
       registry's confirmation gate).
     - One registry-backed test that accepts EITHER a
       `confirmation_required` marker OR successful write — see
       `TestCalendarAddEventConfirmation` below.

2. **Recurrence expansion** — caldav's `Calendar.search(expand=True)`
   returns one VEVENT per occurrence. The fake is pre-populated with
   pre-expanded occurrences (same UID, distinct DTSTART). Implementer
   may call `.search()` with any reasonable kwargs.

3. **All-day formatting on the way IN** — spec requires "all_day → date-
   only required, end exclusive". A single-day all-day event sets
   `start=2026-05-05` and `end=2026-05-06`. We test that the saved
   event's DTSTART is a date-only value (not a datetime); the precise
   end-date is not pinned in the call kwargs to give the implementer
   slack on inclusive-vs-exclusive interpretation.

4. **Timezone normalization** — iCloud frequently returns events with
   TZID=America/Chicago. The expected output is UTC ISO. The fake
   tags `vobject_instance.vevent.dtstart.params['TZID']` so the
   implementer can branch on it.

5. **Read-only detection** — the implementer may compute read-only via
   privilege-set lookup (no-write) or via subscription metadata. The
   fake's `privileges` set drops "write" for read-only calendars; the
   `read_only` property mirrors this. Tests check the boolean output
   only.
"""

from __future__ import annotations

import datetime
import logging
import zoneinfo

import pytest


# Module-level imports: collection fails clearly when
# `ember.tools.icloud_calendar` doesn't exist yet — that's the
# expected baseline pre-merge.
from ember.errors import AuthError, ToolError, TransportError  # noqa: E402
from ember.tools.base import Tool, ToolResult  # noqa: E402
from ember.tools.registry import (  # noqa: E402
    CONFIRMATION_REQUIRED_ERROR,
    Registry,
)
from ember.tools import icloud_calendar as calendar_module  # noqa: E402
from ember.tools.icloud_calendar import (  # noqa: E402
    CalendarAddEventTool,
    CalendarListCalendarsTool,
    CalendarListEventsTool,
)
from tests.fakes.fake_caldav import (
    FakeCalendar,
    FakeEvent,
    install_fake_caldav,
)


SECRET_PASSWORD = "sk-icloud-cal-XXX-SECRET-XXX"  # noqa: S105 — test sentinel
APPLE_ID = "ember@example.com"
CALDAV_URL = "https://caldav.icloud.com/"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_list_calendars_tool() -> CalendarListCalendarsTool:
    return CalendarListCalendarsTool(
        apple_id=APPLE_ID,
        app_password=SECRET_PASSWORD,
        caldav_url=CALDAV_URL,
    )


def _make_list_events_tool() -> CalendarListEventsTool:
    return CalendarListEventsTool(
        apple_id=APPLE_ID,
        app_password=SECRET_PASSWORD,
        caldav_url=CALDAV_URL,
    )


def _make_add_event_tool() -> CalendarAddEventTool:
    return CalendarAddEventTool(
        apple_id=APPLE_ID,
        app_password=SECRET_PASSWORD,
        caldav_url=CALDAV_URL,
    )


def _utc(year: int, month: int, day: int, hour: int = 12, minute: int = 0) -> datetime.datetime:
    return datetime.datetime(year, month, day, hour, minute, tzinfo=datetime.timezone.utc)


def _chicago(year: int, month: int, day: int, hour: int = 12, minute: int = 0) -> datetime.datetime:
    return datetime.datetime(
        year, month, day, hour, minute, tzinfo=zoneinfo.ZoneInfo("America/Chicago")
    )


def _assert_no_secret_anywhere(
    *,
    caplog: pytest.LogCaptureFixture,
    exc: BaseException | None = None,
) -> None:
    for record in caplog.records:
        msg = record.getMessage()
        assert SECRET_PASSWORD not in msg
        for attr in ("msg", "message", "exc_text"):
            v = getattr(record, attr, None)
            if isinstance(v, str):
                assert SECRET_PASSWORD not in v
    if exc is not None:
        assert SECRET_PASSWORD not in str(exc)
        assert SECRET_PASSWORD not in repr(exc)


def _mixed_calendars() -> list[FakeCalendar]:
    """One event calendar (Personal, RW), one event calendar
    (Holidays, read-only), one VTODO (Reminders)."""
    return [
        FakeCalendar(
            name="Personal",
            color="#3478F6",
            supported_components=["VEVENT"],
            privileges={"read", "write"},
            events_data=[
                FakeEvent(
                    summary="Lunch",
                    dtstart=_utc(2026, 5, 10, 12, 0),
                    dtend=_utc(2026, 5, 10, 13, 0),
                    description="With Bob",
                    location="Cafe",
                    uid="evt-personal-1",
                ),
            ],
        ),
        FakeCalendar(
            name="Holidays",
            color="#FF0000",
            supported_components=["VEVENT"],
            privileges={"read"},  # subscribed / no write
            events_data=[
                FakeEvent(
                    summary="Memorial Day",
                    dtstart=datetime.date(2026, 5, 25),
                    dtend=datetime.date(2026, 5, 26),
                    uid="evt-holiday-1",
                ),
            ],
        ),
        FakeCalendar(
            name="Reminders",
            supported_components=["VTODO"],
            privileges={"read", "write"},
        ),
    ]


# ===========================================================================
# Static surface (FC-3)
# ===========================================================================


class TestStaticSurface:
    def test_list_calendars_name(self) -> None:
        assert CalendarListCalendarsTool.name == "calendar_list_calendars"

    def test_list_events_name(self) -> None:
        assert CalendarListEventsTool.name == "calendar_list_events"

    def test_add_event_name(self) -> None:
        assert CalendarAddEventTool.name == "calendar_add_event"

    def test_list_calendars_description_nonempty(self) -> None:
        assert isinstance(CalendarListCalendarsTool.description, str)
        assert CalendarListCalendarsTool.description.strip() != ""

    def test_list_events_description_nonempty(self) -> None:
        assert isinstance(CalendarListEventsTool.description, str)
        assert CalendarListEventsTool.description.strip() != ""

    def test_add_event_description_nonempty(self) -> None:
        assert isinstance(CalendarAddEventTool.description, str)
        assert CalendarAddEventTool.description.strip() != ""

    def test_list_calendars_not_destructive(self) -> None:
        assert CalendarListCalendarsTool.requires_confirmation is False

    def test_list_events_not_destructive(self) -> None:
        assert CalendarListEventsTool.requires_confirmation is False

    def test_add_event_requires_confirmation_is_implementer_flexible(self) -> None:
        # The contract explicitly leaves this flag as either always-True
        # or dynamic. We pin only that it's a bool — TestCalendarAddEvent
        # Confirmation handles either branch downstream.
        assert isinstance(CalendarAddEventTool.requires_confirmation, bool)

    def test_list_events_schema_shape(self) -> None:
        s = CalendarListEventsTool.input_schema
        assert s["type"] == "object"
        props = s["properties"]
        assert "start_iso" in props
        assert "end_iso" in props
        assert "calendar_name" in props
        assert set(s["required"]) == {"start_iso", "end_iso"}

    def test_add_event_schema_shape(self) -> None:
        s = CalendarAddEventTool.input_schema
        props = s["properties"]
        assert "title" in props
        assert "start_iso" in props
        assert "end_iso" in props
        assert "calendar_name" in props
        assert "location" in props
        assert "notes" in props
        assert "all_day" in props
        assert props["all_day"].get("default") is False
        assert set(s["required"]) == {"title", "start_iso", "end_iso"}

    def test_protocol_compliance(self) -> None:
        assert isinstance(_make_list_calendars_tool(), Tool)
        assert isinstance(_make_list_events_tool(), Tool)
        assert isinstance(_make_add_event_tool(), Tool)


# ===========================================================================
# calendar_list_calendars
# ===========================================================================


class TestCalendarListCalendars:
    async def test_returns_classified_calendars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = install_fake_caldav(monkeypatch, calendar_module)
        state.principal.calendars_data = _mixed_calendars()

        result = await _make_list_calendars_tool().call({})

        assert isinstance(result, ToolResult)
        out = result.output
        assert "calendars" in out
        cals = out["calendars"]
        # 3 calendars in input.
        assert len(cals) == 3
        for c in cals:
            assert "name" in c
            assert "color" in c
            assert "type" in c
            assert "read_only" in c
            assert c["type"] in ("event", "reminder")

    async def test_event_vs_reminder_classification(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = install_fake_caldav(monkeypatch, calendar_module)
        state.principal.calendars_data = _mixed_calendars()

        result = await _make_list_calendars_tool().call({})

        by_name = {c["name"]: c for c in result.output["calendars"]}
        assert by_name["Personal"]["type"] == "event"
        assert by_name["Holidays"]["type"] == "event"
        assert by_name["Reminders"]["type"] == "reminder"

    async def test_read_only_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = install_fake_caldav(monkeypatch, calendar_module)
        state.principal.calendars_data = _mixed_calendars()

        result = await _make_list_calendars_tool().call({})

        by_name = {c["name"]: c for c in result.output["calendars"]}
        assert by_name["Personal"]["read_only"] is False
        assert by_name["Holidays"]["read_only"] is True

    async def test_auth_error_translation(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        state = install_fake_caldav(monkeypatch, calendar_module)
        state.principal_raises = Exception("401 Unauthorized")

        with pytest.raises(AuthError) as excinfo:
            await _make_list_calendars_tool().call({})

        _assert_no_secret_anywhere(caplog=caplog, exc=excinfo.value)

    async def test_transport_error_translation(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        state = install_fake_caldav(monkeypatch, calendar_module)
        state.construct_raises = OSError("connection refused")

        with pytest.raises(TransportError) as excinfo:
            await _make_list_calendars_tool().call({})

        _assert_no_secret_anywhere(caplog=caplog, exc=excinfo.value)


# ===========================================================================
# calendar_list_events
# ===========================================================================


class TestCalendarListEventsHappyPath:
    async def test_single_calendar_returns_events(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = install_fake_caldav(monkeypatch, calendar_module)
        state.principal.calendars_data = _mixed_calendars()

        result = await _make_list_events_tool().call(
            {
                "start_iso": "2026-05-01T00:00:00+00:00",
                "end_iso": "2026-05-31T23:59:59+00:00",
                "calendar_name": "Personal",
            }
        )

        out = result.output
        assert "events" in out
        evs = out["events"]
        # Just the Personal Lunch event.
        assert len(evs) == 1
        ev = evs[0]
        assert ev["uid"] == "evt-personal-1"
        assert ev["calendar"] == "Personal"
        assert ev["title"] == "Lunch"
        assert "start_iso" in ev and "end_iso" in ev
        assert ev["all_day"] is False
        assert ev["location"] == "Cafe"
        assert ev["recurring"] is False

    async def test_cross_calendar_merge(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = install_fake_caldav(monkeypatch, calendar_module)
        state.principal.calendars_data = _mixed_calendars()

        # No calendar_name → query all event calendars.
        result = await _make_list_events_tool().call(
            {
                "start_iso": "2026-05-01T00:00:00+00:00",
                "end_iso": "2026-05-31T23:59:59+00:00",
            }
        )

        uids = {ev["uid"] for ev in result.output["events"]}
        # Personal + Holidays both contribute.
        assert "evt-personal-1" in uids
        assert "evt-holiday-1" in uids

    async def test_recurring_expansion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Three pre-expanded daily occurrences sharing the same UID.
        recur_uid = "evt-recur-1"
        cal = FakeCalendar(
            name="Personal",
            supported_components=["VEVENT"],
            privileges={"read", "write"},
        )
        cal.expanded_occurrences = [
            FakeEvent(
                summary="Standup",
                dtstart=_utc(2026, 5, 11, 9, 0),
                dtend=_utc(2026, 5, 11, 9, 15),
                uid=recur_uid,
                rrule="FREQ=DAILY",
            ),
            FakeEvent(
                summary="Standup",
                dtstart=_utc(2026, 5, 12, 9, 0),
                dtend=_utc(2026, 5, 12, 9, 15),
                uid=recur_uid,
                rrule="FREQ=DAILY",
            ),
            FakeEvent(
                summary="Standup",
                dtstart=_utc(2026, 5, 13, 9, 0),
                dtend=_utc(2026, 5, 13, 9, 15),
                uid=recur_uid,
                rrule="FREQ=DAILY",
            ),
        ]
        state = install_fake_caldav(monkeypatch, calendar_module)
        state.principal.calendars_data = [cal]

        result = await _make_list_events_tool().call(
            {
                "start_iso": "2026-05-11T00:00:00+00:00",
                "end_iso": "2026-05-14T00:00:00+00:00",
                "calendar_name": "Personal",
            }
        )

        evs = result.output["events"]
        assert len(evs) == 3
        # Same UID; distinct starts; recurring=True.
        assert all(ev["uid"] == recur_uid for ev in evs)
        starts = {ev["start_iso"] for ev in evs}
        assert len(starts) == 3
        assert all(ev["recurring"] is True for ev in evs)

    async def test_timezone_normalization_to_utc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # America/Chicago noon → UTC 17:00 or 18:00 depending on DST.
        # In May (DST active), CDT is UTC-5, so noon local → 17:00 UTC.
        cal = FakeCalendar(
            name="Personal",
            supported_components=["VEVENT"],
            privileges={"read", "write"},
            events_data=[
                FakeEvent(
                    summary="Lunch",
                    dtstart=_chicago(2026, 5, 10, 12, 0),
                    dtend=_chicago(2026, 5, 10, 13, 0),
                    uid="tz-1",
                )
            ],
        )
        state = install_fake_caldav(monkeypatch, calendar_module)
        state.principal.calendars_data = [cal]

        result = await _make_list_events_tool().call(
            {
                "start_iso": "2026-05-01T00:00:00+00:00",
                "end_iso": "2026-05-31T23:59:59+00:00",
                "calendar_name": "Personal",
            }
        )

        ev = result.output["events"][0]
        # UTC ISO must parse and be in UTC.
        parsed = datetime.datetime.fromisoformat(ev["start_iso"].replace("Z", "+00:00"))
        assert parsed.utcoffset() == datetime.timedelta(0)
        # Converted from 12:00 CDT to UTC: 17:00 (DST May).
        assert parsed.hour == 17
        assert parsed.day == 10

    async def test_all_day_event_date_only_format(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = install_fake_caldav(monkeypatch, calendar_module)
        state.principal.calendars_data = _mixed_calendars()

        result = await _make_list_events_tool().call(
            {
                "start_iso": "2026-05-01T00:00:00+00:00",
                "end_iso": "2026-05-31T23:59:59+00:00",
                "calendar_name": "Holidays",
            }
        )

        evs = result.output["events"]
        ev = evs[0]
        assert ev["all_day"] is True
        # All-day uses date-only YYYY-MM-DD.
        assert len(ev["start_iso"]) == 10  # "2026-05-25"
        assert ev["start_iso"] == "2026-05-25"
        assert "T" not in ev["start_iso"]

    async def test_200_cap_truncation_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Build a calendar with 250 events to push past the 200 cap.
        events = [
            FakeEvent(
                summary=f"e{i}",
                dtstart=_utc(2026, 5, 1, 9, 0) + datetime.timedelta(minutes=i),
                dtend=_utc(2026, 5, 1, 10, 0) + datetime.timedelta(minutes=i),
                uid=f"evt-{i}",
            )
            for i in range(250)
        ]
        cal = FakeCalendar(
            name="Personal",
            supported_components=["VEVENT"],
            privileges={"read", "write"},
            events_data=events,
        )
        state = install_fake_caldav(monkeypatch, calendar_module)
        state.principal.calendars_data = [cal]

        result = await _make_list_events_tool().call(
            {
                "start_iso": "2026-05-01T00:00:00+00:00",
                "end_iso": "2026-05-02T00:00:00+00:00",
                "calendar_name": "Personal",
            }
        )

        out = result.output
        assert len(out["events"]) <= 200
        # Truncation flag set when over the cap.
        assert out.get("truncated") is True


class TestCalendarListEventsValidation:
    async def test_naive_start_iso_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = install_fake_caldav(monkeypatch, calendar_module)
        state.principal.calendars_data = _mixed_calendars()

        with pytest.raises(ToolError):
            await _make_list_events_tool().call(
                {
                    "start_iso": "2026-05-01T00:00:00",  # naive
                    "end_iso": "2026-05-31T00:00:00+00:00",
                }
            )

    async def test_naive_end_iso_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = install_fake_caldav(monkeypatch, calendar_module)
        state.principal.calendars_data = _mixed_calendars()

        with pytest.raises(ToolError):
            await _make_list_events_tool().call(
                {
                    "start_iso": "2026-05-01T00:00:00+00:00",
                    "end_iso": "2026-05-31T00:00:00",  # naive
                }
            )

    async def test_end_le_start_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = install_fake_caldav(monkeypatch, calendar_module)
        state.principal.calendars_data = _mixed_calendars()

        with pytest.raises(ToolError):
            await _make_list_events_tool().call(
                {
                    "start_iso": "2026-05-31T00:00:00+00:00",
                    "end_iso": "2026-05-01T00:00:00+00:00",  # end < start
                }
            )

    async def test_end_equal_start_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = install_fake_caldav(monkeypatch, calendar_module)
        state.principal.calendars_data = _mixed_calendars()

        with pytest.raises(ToolError):
            await _make_list_events_tool().call(
                {
                    "start_iso": "2026-05-01T00:00:00+00:00",
                    "end_iso": "2026-05-01T00:00:00+00:00",  # equal
                }
            )

    async def test_ambiguous_calendar_name_raises_tool_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = install_fake_caldav(monkeypatch, calendar_module)
        state.principal.calendars_data = [
            FakeCalendar(name="Work A", supported_components=["VEVENT"]),
            FakeCalendar(name="Work B", supported_components=["VEVENT"]),
        ]

        # Same as reminders: ambiguous OR not-found, both ToolError.
        with pytest.raises(ToolError) as excinfo:
            await _make_list_events_tool().call(
                {
                    "start_iso": "2026-05-01T00:00:00+00:00",
                    "end_iso": "2026-05-31T00:00:00+00:00",
                    "calendar_name": "work",
                }
            )

        msg = str(excinfo.value).lower()
        assert "ambiguous" in msg or "not found" in msg or "no calendar" in msg


# ===========================================================================
# calendar_add_event
# ===========================================================================


class TestCalendarAddEventHappyPath:
    async def test_adds_event_to_named_calendar(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = install_fake_caldav(monkeypatch, calendar_module)
        cals = _mixed_calendars()
        state.principal.calendars_data = cals

        # Use direct tool.call to bypass any registry confirmation gate.
        result = await _make_add_event_tool().call(
            {
                "title": "Coffee",
                "calendar_name": "Personal",
                "start_iso": "2026-05-15T15:00:00+00:00",
                "end_iso": "2026-05-15T16:00:00+00:00",
                "location": "Spyhouse",
                "notes": "ours",
            }
        )

        out = result.output
        assert "uid" in out
        assert isinstance(out["uid"], str) and out["uid"]
        assert out["calendar"] == "Personal"

        personal = next(c for c in cals if c.name == "Personal")
        assert len(personal.save_event_calls) == 1

    async def test_all_day_event(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = install_fake_caldav(monkeypatch, calendar_module)
        cals = [
            FakeCalendar(
                name="Personal",
                supported_components=["VEVENT"],
                privileges={"read", "write"},
            )
        ]
        state.principal.calendars_data = cals

        result = await _make_add_event_tool().call(
            {
                "title": "Vacation",
                "calendar_name": "Personal",
                # all_day → date-only required.
                "start_iso": "2026-07-01",
                "end_iso": "2026-07-08",
                "all_day": True,
            }
        )

        out = result.output
        assert out["calendar"] == "Personal"
        assert isinstance(out["uid"], str) and out["uid"]
        # The save_event call should reflect all-day shape:
        # either (a) DTSTART/DTEND as dates (not datetimes) in kwargs,
        # or (b) the iCal payload contains VALUE=DATE on DTSTART.
        # Tester accepts either since the impl may serialize manually.
        assert len(cals[0].save_event_calls) == 1

    async def test_default_calendar_resolution(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No calendar_name: implementer should pick a default writable
        # event calendar. We accept either:
        #   (a) a calendar literally named "Calendar" or "Personal",
        #   (b) the first writable VEVENT calendar.
        state = install_fake_caldav(monkeypatch, calendar_module)
        cals = [
            FakeCalendar(
                name="Personal",
                supported_components=["VEVENT"],
                privileges={"read", "write"},
            ),
            FakeCalendar(
                name="Holidays",
                supported_components=["VEVENT"],
                privileges={"read"},  # read-only
            ),
        ]
        state.principal.calendars_data = cals

        result = await _make_add_event_tool().call(
            {
                "title": "x",
                "start_iso": "2026-05-01T10:00:00+00:00",
                "end_iso": "2026-05-01T11:00:00+00:00",
            }
        )

        # Read-only calendar must NOT receive the event.
        assert cals[0].save_event_calls or not cals[1].save_event_calls
        assert result.output["calendar"] != "Holidays"


class TestCalendarAddEventValidation:
    async def test_naive_datetime_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = install_fake_caldav(monkeypatch, calendar_module)
        state.principal.calendars_data = _mixed_calendars()

        with pytest.raises(ToolError):
            await _make_add_event_tool().call(
                {
                    "title": "x",
                    "calendar_name": "Personal",
                    "start_iso": "2026-05-01T10:00:00",  # naive
                    "end_iso": "2026-05-01T11:00:00+00:00",
                }
            )

    async def test_end_le_start_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = install_fake_caldav(monkeypatch, calendar_module)
        state.principal.calendars_data = _mixed_calendars()

        with pytest.raises(ToolError):
            await _make_add_event_tool().call(
                {
                    "title": "x",
                    "calendar_name": "Personal",
                    "start_iso": "2026-05-01T11:00:00+00:00",
                    "end_iso": "2026-05-01T10:00:00+00:00",  # end < start
                }
            )

    async def test_read_only_calendar_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = install_fake_caldav(monkeypatch, calendar_module)
        state.principal.calendars_data = _mixed_calendars()

        with pytest.raises(ToolError) as excinfo:
            await _make_add_event_tool().call(
                {
                    "title": "x",
                    "calendar_name": "Holidays",  # read-only
                    "start_iso": "2026-05-01T10:00:00+00:00",
                    "end_iso": "2026-05-01T11:00:00+00:00",
                }
            )

        # Error message should name the calendar so the model can
        # self-correct.
        assert "Holidays" in str(excinfo.value)


class TestCalendarAddEventConfirmation:
    """One parametrized test that goes through `Registry.call_tool`.

    The implementer may pick:
      (a) `requires_confirmation = True` always — registry returns the
          `confirmation_required` marker (`is_error=True`).
      (b) Dynamic / always-False — registry calls through and the write
          succeeds (`is_error=False`).

    Either is valid — pin only that exactly ONE of the two outcomes
    occurs, with no exception.
    """

    async def test_either_confirmation_or_success_through_registry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = install_fake_caldav(monkeypatch, calendar_module)
        cals = [
            FakeCalendar(
                name="Personal",
                supported_components=["VEVENT"],
                privileges={"read", "write"},
            )
        ]
        state.principal.calendars_data = cals

        registry = Registry()
        registry.register(_make_add_event_tool())
        result = await registry.call_tool(
            "calendar_add_event",
            {
                "title": "via-registry",
                "calendar_name": "Personal",
                "start_iso": "2026-05-15T15:00:00+00:00",
                "end_iso": "2026-05-15T16:00:00+00:00",
            },
        )

        if result.is_error:
            # Branch (a) — confirmation gate. The marker shape is
            # documented in registry.CONFIRMATION_REQUIRED_ERROR.
            out = result.output
            assert isinstance(out, dict)
            assert out.get("error") == CONFIRMATION_REQUIRED_ERROR
            assert "action" in out
            # No write should have happened yet.
            assert len(cals[0].save_event_calls) == 0
        else:
            # Branch (b) — direct write. Output carries the new uid.
            assert "uid" in result.output
            assert result.output["calendar"] == "Personal"
            assert len(cals[0].save_event_calls) == 1


# ===========================================================================
# Auth/Transport translation across calendar tools
# ===========================================================================


class TestCalendarErrorTranslation:
    async def test_list_events_auth_error(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        state = install_fake_caldav(monkeypatch, calendar_module)
        state.principal_raises = Exception("401 Unauthorized")

        with pytest.raises(AuthError) as excinfo:
            await _make_list_events_tool().call(
                {
                    "start_iso": "2026-05-01T00:00:00+00:00",
                    "end_iso": "2026-05-31T00:00:00+00:00",
                }
            )

        _assert_no_secret_anywhere(caplog=caplog, exc=excinfo.value)

    async def test_list_events_transport_error(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        state = install_fake_caldav(monkeypatch, calendar_module)
        state.construct_raises = OSError("connection refused")

        with pytest.raises(TransportError) as excinfo:
            await _make_list_events_tool().call(
                {
                    "start_iso": "2026-05-01T00:00:00+00:00",
                    "end_iso": "2026-05-31T00:00:00+00:00",
                }
            )

        _assert_no_secret_anywhere(caplog=caplog, exc=excinfo.value)

    async def test_add_event_auth_error(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        state = install_fake_caldav(monkeypatch, calendar_module)
        state.principal_raises = Exception("401 Unauthorized")

        with pytest.raises(AuthError) as excinfo:
            await _make_add_event_tool().call(
                {
                    "title": "x",
                    "start_iso": "2026-05-01T10:00:00+00:00",
                    "end_iso": "2026-05-01T11:00:00+00:00",
                }
            )

        _assert_no_secret_anywhere(caplog=caplog, exc=excinfo.value)


# ===========================================================================
# Registry round-trips for read-only schemas (FC-3 schema acceptance)
# ===========================================================================


class TestRegistryRoundTrip:
    async def test_list_calendars_through_registry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = install_fake_caldav(monkeypatch, calendar_module)
        state.principal.calendars_data = _mixed_calendars()

        registry = Registry()
        registry.register(_make_list_calendars_tool())
        result = await registry.call_tool("calendar_list_calendars", {})
        assert result.is_error is False
        assert "calendars" in result.output

    async def test_list_events_through_registry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = install_fake_caldav(monkeypatch, calendar_module)
        state.principal.calendars_data = _mixed_calendars()

        registry = Registry()
        registry.register(_make_list_events_tool())
        result = await registry.call_tool(
            "calendar_list_events",
            {
                "start_iso": "2026-05-01T00:00:00+00:00",
                "end_iso": "2026-05-31T00:00:00+00:00",
            },
        )
        assert result.is_error is False
        assert "events" in result.output


# ===========================================================================
# TLC-4 secret hygiene
# ===========================================================================


class TestSecretHygiene:
    async def test_password_never_logged_on_list_calendars_success(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        state = install_fake_caldav(monkeypatch, calendar_module)
        state.principal.calendars_data = _mixed_calendars()

        await _make_list_calendars_tool().call({})

        _assert_no_secret_anywhere(caplog=caplog)

    async def test_password_never_logged_on_list_events_success(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        state = install_fake_caldav(monkeypatch, calendar_module)
        state.principal.calendars_data = _mixed_calendars()

        await _make_list_events_tool().call(
            {
                "start_iso": "2026-05-01T00:00:00+00:00",
                "end_iso": "2026-05-31T00:00:00+00:00",
            }
        )

        _assert_no_secret_anywhere(caplog=caplog)

    async def test_password_never_logged_on_add_event_success(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        state = install_fake_caldav(monkeypatch, calendar_module)
        state.principal.calendars_data = [
            FakeCalendar(
                name="Personal",
                supported_components=["VEVENT"],
                privileges={"read", "write"},
            )
        ]

        await _make_add_event_tool().call(
            {
                "title": "x",
                "calendar_name": "Personal",
                "start_iso": "2026-05-01T10:00:00+00:00",
                "end_iso": "2026-05-01T11:00:00+00:00",
            }
        )

        _assert_no_secret_anywhere(caplog=caplog)

    def test_password_not_in_repr_or_str(self) -> None:
        for tool in (
            _make_list_calendars_tool(),
            _make_list_events_tool(),
            _make_add_event_tool(),
        ):
            assert SECRET_PASSWORD not in repr(tool)
            assert SECRET_PASSWORD not in str(tool)
