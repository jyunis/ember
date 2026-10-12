"""iCloud Calendar tools (v16).

Three tools backed by Apple iCloud CalDAV (VEVENT collections):

- `calendar_list_calendars` — enumerate calendars on the principal,
  including type (event vs reminder), color, and a `read_only` flag.
- `calendar_list_events` — fetch events in a window. Recurring events
  are expanded server-side via `caldav.search(..., expand=True)` so
  each occurrence is its own entry (same UID, distinct start/end).
  Capped at 200 events; sets top-level `truncated: true` past the cap.
- `calendar_add_event` — create a new VEVENT. Per RFC 5545, all-day
  events use date-only DTEND that is *exclusive* (single-day = start +
  1 day).

Conventions / contracts:
  * `caldav` is sync; every call wrapped in `asyncio.to_thread(...)`.
  * Library exceptions translated via the shared base mapping.
  * Naive datetimes from caldav output → assumed UTC (defensive).
  * Naive datetimes in user input are rejected.
  * Calendar matching is case-insensitive against display name; multiple
    matches → ToolError listing candidates.

Confirmation gate (`calendar_add_event`):
  We chose strategy (a): `requires_confirmation = True` *always*. The
  alternative — only confirming on shared/owner-different calendars —
  is murky to detect via caldav's surface and easy to get wrong; the
  blast radius of writing into the wrong calendar (notifying every
  attendee on a shared family calendar) makes always-confirming the
  safe default. Tradeoff: one extra prompt per event creation.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import uuid
from typing import Any

from ember.errors import AuthError, ToolError, TransportError
from ember.tools._icloud_base import _iCloudBase, _is_transport_exc, _safe_error_text
from ember.tools.base import ToolResult

_log = logging.getLogger("ember.tools.icloud")

_TRUNCATION_MARKER = "\n...[truncated]"
_NOTES_MAX = 2000
_EVENT_CAP = 200


def _truncate_notes(text: str | None) -> str:
    if not text:
        return ""
    if len(text) > _NOTES_MAX:
        return text[:_NOTES_MAX] + _TRUNCATION_MARKER
    return text


def _translate(exc: BaseException, password: str, what: str) -> Exception:
    if _is_auth_exc(exc):
        return AuthError(f"{what}: caldav auth refused: {_safe_error_text(exc, password)}")
    if _is_transport_exc(exc):
        return TransportError(f"{what}: caldav transport: {_safe_error_text(exc, password)}")
    return ToolError(f"{what}: {_safe_error_text(exc, password)}")


def _is_auth_exc(exc: BaseException) -> bool:
    if type(exc).__name__ == "AuthorizationError":
        return True
    try:
        from caldav.lib.error import AuthorizationError  # type: ignore[import-not-found]
    except Exception:
        AuthorizationError = ()  # type: ignore[assignment,misc]
    if AuthorizationError and isinstance(exc, AuthorizationError):  # type: ignore[arg-type]
        return True
    msg = str(exc).lower()
    return "401" in msg or "unauthorized" in msg or "authorizationfailed" in msg


def _calendar_display_name(cal: Any) -> str:
    name = getattr(cal, "name", None)
    if name:
        return str(name)
    try:
        props = cal.get_properties([("DAV:", "displayname")])
        for v in props.values():
            if v:
                return str(v)
    except Exception:
        pass
    return ""


def _calendar_color(cal: Any) -> str:
    """Read calendar-color (Apple namespace), "" if unset."""
    try:
        props = cal.get_properties([("http://apple.com/ns/ical/", "calendar-color")])
        for v in props.values():
            if v:
                return str(v)
    except Exception:
        pass
    return ""


def _supported_components(cal: Any) -> set[str]:
    try:
        comps = cal.get_supported_components()
    except Exception:
        return set()
    return {str(c).upper() for c in (comps or [])}


def _calendar_type(cal: Any) -> str:
    """Map supported-components to a coarse type label."""
    comps = _supported_components(cal)
    if "VEVENT" in comps:
        return "event"
    if "VTODO" in comps:
        return "reminder"
    return "event"


def _is_read_only(cal: Any) -> bool:
    """No `write` privilege in current-user-privilege-set → read-only.

    Used to detect subscribed / shared-read calendars before attempting
    to write into them. Defaults to RW (False) when the property is
    unreadable — most owned iCloud collections don't expose a privilege
    set and assuming RW matches reality.
    """
    try:
        props = cal.get_properties([("DAV:", "current-user-privilege-set")])
    except Exception:
        return False
    if not props:
        return False
    for v in props.values():
        if v is None:
            continue
        if "write" in str(v).lower():
            return False
    return True


def _resolve_event_calendar(
    principal: Any,
    name: str | None,
    *,
    require_writable: bool,
) -> Any:
    """Match a calendar by display name (case-insensitive) among VEVENT-
    supporting collections.

    None → first writable VEVENT calendar; ToolError if none.
    """
    calendars = principal.calendars()
    event_cals = [c for c in calendars if "VEVENT" in _supported_components(c)]
    if not event_cals:
        raise ToolError("no event (VEVENT) calendars found on this account")

    if name is None:
        for c in event_cals:
            if not require_writable or not _is_read_only(c):
                return c
        raise ToolError("no writable event calendars found on this account")

    target = name.strip().lower()
    matches = [c for c in event_cals if _calendar_display_name(c).lower() == target]
    if len(matches) > 1:
        names = sorted({_calendar_display_name(c) for c in matches})
        raise ToolError(f"ambiguous calendar {name!r}; candidates: {names}")
    if not matches:
        available = sorted(
            {_calendar_display_name(c) for c in event_cals if _calendar_display_name(c)}
        )
        raise ToolError(f"calendar {name!r} not found; available: {available}")
    cal = matches[0]
    if require_writable and _is_read_only(cal):
        raise ToolError(
            f"calendar {_calendar_display_name(cal)!r} is read-only "
            f"(subscribed or shared without write access)"
        )
    return cal


def _parse_iso_aware(value: str, *, field: str) -> datetime.datetime:
    try:
        dt = datetime.datetime.fromisoformat(value)
    except ValueError as e:
        raise ToolError(f"invalid {field}: {value!r} ({e})") from None
    if dt.tzinfo is None:
        raise ToolError(
            f"invalid {field}: {value!r} is naive — include a timezone offset (e.g. '...+00:00')"
        )
    return dt


def _parse_date_only(value: str, *, field: str) -> datetime.date:
    try:
        return datetime.date.fromisoformat(value)
    except ValueError as e:
        raise ToolError(f"invalid {field}: {value!r} ({e})") from None


def _to_utc_iso(dt: Any) -> str:
    if dt is None:
        return ""
    if isinstance(dt, datetime.datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc).isoformat()
    if isinstance(dt, datetime.date):
        return dt.isoformat()
    return ""


def _get_field(vobj_block: Any, name: str) -> Any:
    try:
        prop = getattr(vobj_block, name.lower(), None)
    except Exception:
        prop = None
    if prop is None:
        return None
    return getattr(prop, "value", None)


def _summarize_event(ev: Any) -> dict[str, Any]:
    calendar = ""
    parent = getattr(ev, "parent", None)
    if parent is not None:
        calendar = _calendar_display_name(parent)

    vobj = getattr(ev, "vobject_instance", None) or getattr(ev, "instance", None)
    vevent = getattr(vobj, "vevent", None) if vobj is not None else None

    uid = ""
    title = ""
    start: Any = None
    end: Any = None
    location = ""
    notes = ""
    recurring = False

    if vevent is not None:
        uid = _get_field(vevent, "uid") or ""
        title = _get_field(vevent, "summary") or ""
        start = _get_field(vevent, "dtstart")
        end = _get_field(vevent, "dtend")
        location = _get_field(vevent, "location") or ""
        notes = _get_field(vevent, "description") or ""
        # An expanded occurrence carries RECURRENCE-ID; a master with
        # RRULE is also recurring. Either flags as `recurring`.
        if (
            getattr(vevent, "rrule", None) is not None
            or getattr(vevent, "recurrence_id", None) is not None
            or getattr(vevent, "recurrence-id", None) is not None
        ):
            recurring = True

    all_day = isinstance(start, datetime.date) and not isinstance(start, datetime.datetime)
    if all_day:
        start_iso = start.isoformat() if start else ""
        end_iso = end.isoformat() if isinstance(end, datetime.date) else ""
    else:
        start_iso = _to_utc_iso(start)
        end_iso = _to_utc_iso(end)

    return {
        "uid": str(uid),
        "calendar": calendar,
        "title": str(title),
        "start_iso": start_iso,
        "end_iso": end_iso,
        "all_day": all_day,
        "location": str(location),
        "notes": _truncate_notes(notes),
        "recurring": recurring,
    }


# ---------------------------------------------------------------------------
# calendar_list_calendars
# ---------------------------------------------------------------------------


class CalendarListCalendarsTool(_iCloudBase):
    """Enumerate the principal's calendars."""

    name: str = "calendar_list_calendars"
    description: str = (
        "List Apple Calendar collections on the account. Returns "
        "name/color/type (event|reminder)/read_only for each."
    )
    requires_confirmation: bool = False
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {},
    }

    async def call(self, args: dict[str, Any]) -> ToolResult:
        return await asyncio.to_thread(self._list)

    def _list(self) -> ToolResult:
        try:
            client = self._client()
            principal = client.principal()
            calendars = principal.calendars()
        except Exception as e:
            raise _translate(e, self._app_password, "calendar_list_calendars") from None

        out: list[dict[str, Any]] = []
        for cal in calendars or []:
            out.append(
                {
                    "name": _calendar_display_name(cal),
                    "color": _calendar_color(cal),
                    "type": _calendar_type(cal),
                    "read_only": _is_read_only(cal),
                }
            )
        return ToolResult(output={"calendars": out})


# ---------------------------------------------------------------------------
# calendar_list_events
# ---------------------------------------------------------------------------


class CalendarListEventsTool(_iCloudBase):
    """List events in a [start, end] window across one or all calendars."""

    name: str = "calendar_list_events"
    description: str = (
        "List Apple Calendar events in a time window. Recurring events are "
        "expanded — each occurrence is a separate entry sharing UID. "
        "Capped at 200 events; truncated:true at top level when hit."
    )
    requires_confirmation: bool = False
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "start_iso": {
                "type": "string",
                "description": "Window start, ISO-8601 with offset.",
            },
            "end_iso": {
                "type": "string",
                "description": "Window end, ISO-8601 with offset (must be > start_iso).",
            },
            "calendar_name": {
                "type": ["string", "null"],
                "default": None,
                "description": "Calendar display name (case-insensitive). None = all event cals.",
            },
        },
        "required": ["start_iso", "end_iso"],
    }

    async def call(self, args: dict[str, Any]) -> ToolResult:
        start_dt = _parse_iso_aware(str(args["start_iso"]), field="calendar_list_events: start_iso")
        end_dt = _parse_iso_aware(str(args["end_iso"]), field="calendar_list_events: end_iso")
        if end_dt <= start_dt:
            raise ToolError("calendar_list_events: end_iso must be > start_iso")
        calendar_name = args.get("calendar_name")
        return await asyncio.to_thread(
            self._list_events,
            start=start_dt,
            end=end_dt,
            calendar_name=calendar_name,
        )

    def _list_events(
        self,
        *,
        start: datetime.datetime,
        end: datetime.datetime,
        calendar_name: str | None,
    ) -> ToolResult:
        try:
            client = self._client()
            principal = client.principal()
        except Exception as e:
            raise _translate(e, self._app_password, "calendar_list_events") from None

        try:
            calendars = principal.calendars()
        except Exception as e:
            raise _translate(e, self._app_password, "calendar_list_events") from None

        event_cals = [c for c in calendars if "VEVENT" in _supported_components(c)]

        if calendar_name is not None:
            target = calendar_name.strip().lower()
            matches = [c for c in event_cals if _calendar_display_name(c).lower() == target]
            if len(matches) > 1:
                names = sorted({_calendar_display_name(c) for c in matches})
                raise ToolError(
                    f"calendar_list_events: ambiguous calendar {calendar_name!r}; "
                    f"candidates: {names}"
                )
            if not matches:
                available = sorted(
                    {_calendar_display_name(c) for c in event_cals if _calendar_display_name(c)}
                )
                raise ToolError(
                    f"calendar_list_events: calendar {calendar_name!r} not found; "
                    f"available: {available}"
                )
            event_cals = matches

        out: list[dict[str, Any]] = []
        truncated = False
        for cal in event_cals:
            if len(out) >= _EVENT_CAP:
                truncated = True
                break
            try:
                events = cal.search(
                    start=start,
                    end=end,
                    event=True,
                    expand=True,
                )
            except TypeError:
                # Older caldav: fall back to date_search.
                try:
                    events = cal.date_search(start=start, end=end, expand=True)
                except Exception as e:
                    raise _translate(e, self._app_password, "calendar_list_events") from None
            except Exception as e:
                raise _translate(e, self._app_password, "calendar_list_events") from None

            for ev in events or []:
                if len(out) >= _EVENT_CAP:
                    truncated = True
                    break
                out.append(_summarize_event(ev))

        result: dict[str, Any] = {"events": out}
        if truncated:
            result["truncated"] = True
        return ToolResult(output=result)


# ---------------------------------------------------------------------------
# calendar_add_event
# ---------------------------------------------------------------------------


def _ical_escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _build_vevent_ical(
    *,
    uid: str,
    summary: str,
    start: datetime.datetime | datetime.date,
    end: datetime.datetime | datetime.date,
    location: str | None,
    notes: str | None,
    all_day: bool,
) -> str:
    now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//ember//iCloud Calendar//EN",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{now_utc}",
        f"SUMMARY:{_ical_escape(summary)}",
    ]
    if all_day:
        # date-only; DTEND is exclusive per RFC 5545.
        lines.append(f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}")
        lines.append(f"DTEND;VALUE=DATE:{end.strftime('%Y%m%d')}")
    else:
        s = start.astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")  # type: ignore[union-attr]
        e = end.astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")  # type: ignore[union-attr]
        lines.append(f"DTSTART:{s}")
        lines.append(f"DTEND:{e}")
    if location:
        lines.append(f"LOCATION:{_ical_escape(location)}")
    if notes:
        lines.append(f"DESCRIPTION:{_ical_escape(notes)}")
    lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


class CalendarAddEventTool(_iCloudBase):
    """Create a new VEVENT.

    `requires_confirmation = True`: chosen as the simpler always-confirm
    strategy (vs. detecting shared-calendar via caldav's murky
    privilege-set surface). Prevents accidental sends to family/shared
    calendars at the cost of one extra prompt per event creation.
    """

    name: str = "calendar_add_event"
    description: str = (
        "Add an event to an Apple Calendar. For all-day, pass YYYY-MM-DD "
        "in start_iso/end_iso; DTEND is exclusive (single-day = start + 1 day). "
        "For timed events, ISO-8601 with offset is required."
    )
    requires_confirmation: bool = True
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Event title (SUMMARY)."},
            "start_iso": {
                "type": "string",
                "description": "Start: ISO-8601 datetime with offset, or YYYY-MM-DD if all_day.",
            },
            "end_iso": {
                "type": "string",
                "description": (
                    "End: ISO-8601 datetime with offset, or YYYY-MM-DD if all_day. "
                    "DTEND is exclusive — single-day all-day events use start + 1 day."
                ),
            },
            "calendar_name": {
                "type": ["string", "null"],
                "default": None,
                "description": (
                    "Calendar display name (case-insensitive). None = first writable event cal."
                ),
            },
            "location": {
                "type": ["string", "null"],
                "default": None,
                "description": "LOCATION free text.",
            },
            "notes": {
                "type": ["string", "null"],
                "default": None,
                "description": "DESCRIPTION free text.",
            },
            "all_day": {
                "type": "boolean",
                "default": False,
                "description": "If True, treat start_iso/end_iso as date-only.",
            },
        },
        "required": ["title", "start_iso", "end_iso"],
    }

    async def call(self, args: dict[str, Any]) -> ToolResult:
        title = str(args["title"])
        start_iso = str(args["start_iso"])
        end_iso = str(args["end_iso"])
        calendar_name = args.get("calendar_name")
        location = args.get("location")
        notes = args.get("notes")
        all_day = bool(args.get("all_day", False))

        start: datetime.datetime | datetime.date
        end: datetime.datetime | datetime.date
        if all_day:
            start = _parse_date_only(start_iso, field="calendar_add_event: start_iso")
            end = _parse_date_only(end_iso, field="calendar_add_event: end_iso")
            if end <= start:
                raise ToolError("calendar_add_event: end_iso must be > start_iso (DTEND exclusive)")
        else:
            start = _parse_iso_aware(start_iso, field="calendar_add_event: start_iso")
            end = _parse_iso_aware(end_iso, field="calendar_add_event: end_iso")
            if end <= start:
                raise ToolError("calendar_add_event: end_iso must be > start_iso")

        return await asyncio.to_thread(
            self._add,
            title=title,
            start=start,
            end=end,
            calendar_name=calendar_name,
            location=location,
            notes=notes,
            all_day=all_day,
        )

    def _add(
        self,
        *,
        title: str,
        start: datetime.datetime | datetime.date,
        end: datetime.datetime | datetime.date,
        calendar_name: str | None,
        location: str | None,
        notes: str | None,
        all_day: bool,
    ) -> ToolResult:
        try:
            client = self._client()
            principal = client.principal()
        except Exception as e:
            raise _translate(e, self._app_password, "calendar_add_event") from None

        try:
            cal = _resolve_event_calendar(principal, calendar_name, require_writable=True)
        except (AuthError, TransportError, ToolError):
            raise
        except Exception as e:
            raise _translate(e, self._app_password, "calendar_add_event") from None

        uid = str(uuid.uuid4()).upper() + "@ember"
        ical = _build_vevent_ical(
            uid=uid,
            summary=title,
            start=start,
            end=end,
            location=location,
            notes=notes,
            all_day=all_day,
        )

        try:
            cal.save_event(ical=ical)
        except TypeError:
            try:
                cal.add_event(ical)
            except Exception as e:
                raise _translate(e, self._app_password, "calendar_add_event") from None
        except Exception as e:
            raise _translate(e, self._app_password, "calendar_add_event") from None

        return ToolResult(output={"uid": uid, "calendar": _calendar_display_name(cal)})
