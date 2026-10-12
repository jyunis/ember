"""iCloud Reminders tools (v16).

Three tools backed by Apple iCloud CalDAV (VTODO collections):

- `reminders_list` — list reminders in a VTODO calendar, filterable by
  list name, completed flag, and due-window.
- `reminders_add` — create a new VTODO in a chosen list.
- `reminders_complete` — mark a VTODO completed (status + completed +
  percent-complete; we do NOT delete).

Conventions / contracts:
  * `caldav` is a sync library; every blocking call is wrapped in
    `await asyncio.to_thread(...)` so the tool's `call()` stays
    async-friendly.
  * Library exceptions are translated via the shared base mapping
    (`ember.tools._icloud_base`) into Ember's typed error taxonomy.
  * The app password never appears in surfaced strings; the secret-
    hygiene `__repr__` lives on `_iCloudBase`.
  * RFC 5545 priority numeric → string mapping:
        1-4 → "high", 5 → "medium", 6-9 → "low", 0 / missing → "none"
  * Naive datetimes from `caldav` output (defensive; iCloud should
    always TZID-tag) are assumed UTC. Naive datetime in user-supplied
    `due_iso` is rejected up-front.
  * `notes` (DESCRIPTION) truncated to 2000 chars with marker
    "\n...[truncated]" — matches `gmail_read` behavior.
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

_PRIORITY_TO_NUM = {"high": 1, "medium": 5, "low": 9, "none": 0}
_VALID_PRIORITIES = frozenset(_PRIORITY_TO_NUM)


def _priority_num_to_str(n: int | None) -> str:
    if n is None or n == 0:
        return "none"
    if 1 <= n <= 4:
        return "high"
    if n == 5:
        return "medium"
    if 6 <= n <= 9:
        return "low"
    return "none"


def _truncate_notes(text: str | None) -> str:
    if not text:
        return ""
    if len(text) > _NOTES_MAX:
        return text[:_NOTES_MAX] + _TRUNCATION_MARKER
    return text


def _to_utc_iso(dt: Any) -> str:
    """Render a date / datetime as UTC ISO-8601, "" if missing."""
    if dt is None:
        return ""
    if isinstance(dt, datetime.datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc).isoformat()
    if isinstance(dt, datetime.date):
        return dt.isoformat()
    return ""


def _parse_iso_aware(value: str, *, field: str) -> datetime.datetime:
    """Parse an ISO-8601 string requiring an explicit offset.

    Naive datetimes are rejected — the model must say "UTC" (or any
    other zone) explicitly so we never silently coerce.
    """
    try:
        dt = datetime.datetime.fromisoformat(value)
    except ValueError as e:
        raise ToolError(f"invalid {field}: {value!r} ({e})") from None
    if dt.tzinfo is None:
        raise ToolError(
            f"invalid {field}: {value!r} is naive — include a timezone offset (e.g. '...+00:00')"
        )
    return dt


def _translate(exc: BaseException, password: str, what: str) -> Exception:
    """Map an arbitrary library exception to the Ember taxonomy."""
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


def _supports_vtodo(cal: Any) -> bool:
    """True if the calendar collection supports VTODO components."""
    try:
        comps = cal.get_supported_components()
    except Exception:
        return False
    return any(str(c).upper() == "VTODO" for c in (comps or []))


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


def _resolve_vtodo_calendar(principal: Any, list_name: str | None) -> Any:
    """Resolution order (per spec):

    None →   first VTODO calendar named "Reminders" (case-insensitive);
             else first VTODO calendar; else ToolError.
    Named →  case-insensitive match. Multiple → ToolError listing
             candidates. None → ToolError listing all VTODO calendars.
    """
    calendars = principal.calendars()
    vtodo_cals = [c for c in calendars if _supports_vtodo(c)]
    if not vtodo_cals:
        raise ToolError("no reminders (VTODO) calendars found on this account")

    if list_name is None:
        for c in vtodo_cals:
            if _calendar_display_name(c).lower() == "reminders":
                return c
        return vtodo_cals[0]

    target = list_name.strip().lower()
    matches = [c for c in vtodo_cals if _calendar_display_name(c).lower() == target]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        names = sorted({_calendar_display_name(c) for c in matches})
        raise ToolError(f"ambiguous reminders list {list_name!r}; candidates: {names}")
    available = sorted({_calendar_display_name(c) for c in vtodo_cals if _calendar_display_name(c)})
    raise ToolError(f"reminders list {list_name!r} not found; available: {available}")


def _get_vtodo_field(vtodo: Any, name: str) -> Any:
    """Pull a property off a vobject VTODO contents block, or None."""
    try:
        prop = getattr(vtodo, name.lower(), None)
    except Exception:
        prop = None
    if prop is None:
        return None
    return getattr(prop, "value", None)


def _summarize_todo(todo: Any) -> dict[str, Any]:
    """Convert a caldav Todo object into the wire shape."""
    list_name = ""
    parent = getattr(todo, "parent", None)
    if parent is not None:
        list_name = _calendar_display_name(parent)

    vobj = getattr(todo, "vobject_instance", None) or getattr(todo, "instance", None)
    vtodo = None
    if vobj is not None:
        vtodo = getattr(vobj, "vtodo", None)

    uid = ""
    title = ""
    due = None
    priority_num: int | None = None
    completed = False
    notes = ""

    if vtodo is not None:
        uid = _get_vtodo_field(vtodo, "uid") or ""
        title = _get_vtodo_field(vtodo, "summary") or ""
        due = _get_vtodo_field(vtodo, "due")
        prio_raw = _get_vtodo_field(vtodo, "priority")
        if prio_raw is not None:
            try:
                priority_num = int(prio_raw)
            except (TypeError, ValueError):
                priority_num = None
        status = _get_vtodo_field(vtodo, "status")
        completed = bool(status and str(status).upper() == "COMPLETED")
        notes = _get_vtodo_field(vtodo, "description") or ""

    return {
        "uid": str(uid),
        "list": list_name,
        "title": str(title),
        "due_iso": _to_utc_iso(due),
        "priority": _priority_num_to_str(priority_num),
        "completed": completed,
        "notes": _truncate_notes(notes),
    }


# ---------------------------------------------------------------------------
# reminders_list
# ---------------------------------------------------------------------------


class RemindersListTool(_iCloudBase):
    """List VTODOs in one (or all) Reminders lists."""

    name: str = "reminders_list"
    description: str = (
        "List Apple Reminders. Returns uid/list/title/due_iso/priority/completed/notes. "
        "Filterable by list name, completion state, and due-window in days."
    )
    requires_confirmation: bool = False
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "list_name": {
                "type": ["string", "null"],
                "default": None,
                "description": (
                    "Reminders list display name (case-insensitive). None = all VTODO lists."
                ),
            },
            "include_completed": {
                "type": "boolean",
                "default": False,
                "description": "Include completed reminders.",
            },
            "due_within_days": {
                "type": ["integer", "null"],
                "default": None,
                "minimum": 1,
                "maximum": 365,
                "description": "Only return reminders due within the next N days (1-365).",
            },
        },
    }

    async def call(self, args: dict[str, Any]) -> ToolResult:
        list_name = args.get("list_name")
        include_completed = bool(args.get("include_completed", False))
        due_within_days = args.get("due_within_days")
        if due_within_days is not None:
            due_within_days = int(due_within_days)

        return await asyncio.to_thread(
            self._fetch_list,
            list_name=list_name,
            include_completed=include_completed,
            due_within_days=due_within_days,
        )

    def _fetch_list(
        self,
        *,
        list_name: str | None,
        include_completed: bool,
        due_within_days: int | None,
    ) -> ToolResult:
        try:
            client = self._client()
            principal = client.principal()
        except Exception as e:
            raise _translate(e, self._app_password, "reminders_list") from None

        try:
            calendars = principal.calendars()
        except Exception as e:
            raise _translate(e, self._app_password, "reminders_list") from None

        vtodo_cals = [c for c in calendars if _supports_vtodo(c)]
        if list_name is not None:
            target = list_name.strip().lower()
            matches = [c for c in vtodo_cals if _calendar_display_name(c).lower() == target]
            if len(matches) > 1:
                names = sorted({_calendar_display_name(c) for c in matches})
                raise ToolError(f"ambiguous reminders list {list_name!r}; candidates: {names}")
            if not matches:
                available = sorted(
                    {_calendar_display_name(c) for c in vtodo_cals if _calendar_display_name(c)}
                )
                raise ToolError(f"reminders list {list_name!r} not found; available: {available}")
            vtodo_cals = matches

        cutoff: datetime.datetime | None = None
        if due_within_days is not None:
            cutoff = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
                days=due_within_days
            )

        out: list[dict[str, Any]] = []
        for cal in vtodo_cals:
            try:
                todos = cal.todos(include_completed=include_completed)
            except TypeError:
                # Older caldav signatures use sort_keys / different names.
                try:
                    todos = cal.todos()
                except Exception as e:
                    raise _translate(e, self._app_password, "reminders_list") from None
            except Exception as e:
                raise _translate(e, self._app_password, "reminders_list") from None

            for t in todos or []:
                summary = _summarize_todo(t)
                if not include_completed and summary["completed"]:
                    continue
                if cutoff is not None:
                    due_str = summary["due_iso"]
                    if not due_str:
                        continue
                    try:
                        due_dt = datetime.datetime.fromisoformat(due_str)
                    except ValueError:
                        continue
                    if due_dt.tzinfo is None:
                        due_dt = due_dt.replace(tzinfo=datetime.timezone.utc)
                    if due_dt > cutoff:
                        continue
                out.append(summary)

        return ToolResult(output={"reminders": out})


# ---------------------------------------------------------------------------
# reminders_add
# ---------------------------------------------------------------------------


def _ical_escape(text: str) -> str:
    """RFC 5545 TEXT escape: backslash, comma, semicolon, newline."""
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _build_vtodo_ical(
    *,
    uid: str,
    summary: str,
    due: datetime.datetime | None,
    notes: str | None,
    priority_str: str,
) -> str:
    """Hand-roll a minimal RFC 5545 VTODO. We avoid a hard `vobject`
    dependency in our own code path; caldav already pulls it in but we
    don't want to depend on its private API surface here."""
    now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//ember//iCloud Reminders//EN",
        "BEGIN:VTODO",
        f"UID:{uid}",
        f"DTSTAMP:{now_utc}",
        f"SUMMARY:{_ical_escape(summary)}",
    ]
    if due is not None:
        due_utc = due.astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        lines.append(f"DUE:{due_utc}")
    if notes:
        lines.append(f"DESCRIPTION:{_ical_escape(notes)}")
    prio_num = _PRIORITY_TO_NUM.get(priority_str, 0)
    if prio_num != 0:
        lines.append(f"PRIORITY:{prio_num}")
    lines.append("END:VTODO")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


class RemindersAddTool(_iCloudBase):
    """Create a new VTODO."""

    name: str = "reminders_add"
    description: str = (
        "Add an Apple Reminder. Returns the assigned uid + list. "
        "list_name resolution: None → 'Reminders' if present, else first VTODO list."
    )
    requires_confirmation: bool = False
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Reminder title (SUMMARY)."},
            "list_name": {
                "type": ["string", "null"],
                "default": None,
                "description": "Reminders list (case-insensitive). None = first 'Reminders' list.",
            },
            "due_iso": {
                "type": ["string", "null"],
                "default": None,
                "description": "Due date-time, ISO-8601 with offset (naive rejected).",
            },
            "notes": {
                "type": ["string", "null"],
                "default": None,
                "description": "DESCRIPTION free text.",
            },
            "priority": {
                "type": "string",
                "enum": ["high", "medium", "low", "none"],
                "default": "none",
                "description": "RFC 5545 priority bucket.",
            },
        },
        "required": ["title"],
    }

    async def call(self, args: dict[str, Any]) -> ToolResult:
        title = str(args["title"])
        list_name = args.get("list_name")
        due_iso = args.get("due_iso")
        notes = args.get("notes")
        priority = str(args.get("priority", "none")).lower()

        if priority not in _VALID_PRIORITIES:
            raise ToolError(
                f"reminders_add: unknown priority {priority!r}; valid: {sorted(_VALID_PRIORITIES)}"
            )

        due_dt: datetime.datetime | None = None
        if due_iso:
            due_dt = _parse_iso_aware(due_iso, field="reminders_add: due_iso")

        return await asyncio.to_thread(
            self._add,
            title=title,
            list_name=list_name,
            due_dt=due_dt,
            notes=notes,
            priority=priority,
        )

    def _add(
        self,
        *,
        title: str,
        list_name: str | None,
        due_dt: datetime.datetime | None,
        notes: str | None,
        priority: str,
    ) -> ToolResult:
        try:
            client = self._client()
            principal = client.principal()
        except Exception as e:
            raise _translate(e, self._app_password, "reminders_add") from None

        try:
            cal = _resolve_vtodo_calendar(principal, list_name)
        except (AuthError, TransportError, ToolError):
            raise
        except Exception as e:
            raise _translate(e, self._app_password, "reminders_add") from None

        uid = str(uuid.uuid4()).upper() + "@ember"
        ical = _build_vtodo_ical(
            uid=uid,
            summary=title,
            due=due_dt,
            notes=notes,
            priority_str=priority,
        )

        try:
            cal.save_todo(ical=ical)
        except TypeError:
            try:
                cal.add_todo(ical)
            except Exception as e:
                raise _translate(e, self._app_password, "reminders_add") from None
        except Exception as e:
            raise _translate(e, self._app_password, "reminders_add") from None

        return ToolResult(output={"uid": uid, "list": _calendar_display_name(cal)})


# ---------------------------------------------------------------------------
# reminders_complete
# ---------------------------------------------------------------------------


class RemindersCompleteTool(_iCloudBase):
    """Mark a VTODO completed without deleting it."""

    name: str = "reminders_complete"
    description: str = (
        "Mark an Apple Reminder completed. Sets STATUS=COMPLETED, "
        "COMPLETED=now-utc, PERCENT-COMPLETE=100. Does not delete."
    )
    requires_confirmation: bool = False
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "uid": {"type": "string", "description": "VTODO UID (from reminders_list)."},
        },
        "required": ["uid"],
    }

    async def call(self, args: dict[str, Any]) -> ToolResult:
        uid = str(args["uid"])
        return await asyncio.to_thread(self._complete, uid=uid)

    def _complete(self, *, uid: str) -> ToolResult:
        try:
            client = self._client()
            principal = client.principal()
        except Exception as e:
            raise _translate(e, self._app_password, "reminders_complete") from None

        try:
            calendars = principal.calendars()
        except Exception as e:
            raise _translate(e, self._app_password, "reminders_complete") from None

        target_todo: Any = None
        for cal in calendars:
            if not _supports_vtodo(cal):
                continue
            # Prefer a server-side lookup by UID where supported; fall
            # back to scanning all VTODOs on the collection.
            try:
                target_todo = cal.todo_by_uid(uid)
            except AttributeError:
                target_todo = None
            except Exception:
                target_todo = None
            if target_todo is not None:
                break
            try:
                todos = cal.todos(include_completed=True)
            except TypeError:
                try:
                    todos = cal.todos()
                except Exception as e:
                    raise _translate(e, self._app_password, "reminders_complete") from None
            except Exception as e:
                raise _translate(e, self._app_password, "reminders_complete") from None
            for t in todos or []:
                summary = _summarize_todo(t)
                if summary["uid"] == uid:
                    target_todo = t
                    break
            if target_todo is not None:
                break

        if target_todo is None:
            raise ToolError(f"reminders_complete: uid {uid!r} not found")

        now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        try:
            vobj = getattr(target_todo, "vobject_instance", None) or getattr(
                target_todo, "instance", None
            )
            vtodo = getattr(vobj, "vtodo", None) if vobj is not None else None
            if vtodo is None:
                raise ToolError(f"reminders_complete: uid {uid!r} has no VTODO body")

            # Set / overwrite the three properties.
            for prop_name in ("status", "completed", "percent_complete"):
                if hasattr(vtodo, prop_name):
                    try:
                        delattr(vtodo, prop_name)
                    except Exception:
                        pass

            status_p = vtodo.add("status")
            status_p.value = "COMPLETED"
            completed_p = vtodo.add("completed")
            completed_p.value = datetime.datetime.now(datetime.timezone.utc)
            pc = vtodo.add("percent-complete")
            pc.value = "100"

            # Persist.
            target_todo.save()
        except (AuthError, TransportError, ToolError):
            raise
        except Exception as e:
            # Fallback: re-serialize a minimal completion patch via raw
            # iCal if vobject mutation isn't available. Most caldav
            # versions expose `target_todo.data = <ics>` + `.save()`.
            try:
                raw = getattr(target_todo, "data", None)
                if raw is None:
                    raise e
                # Minimal in-place rewrite: append/replace the three
                # properties on the VTODO block. We do a coarse line
                # filter to drop any prior copies, then inject ours
                # before END:VTODO.
                lines = []
                drop_prefixes = ("STATUS:", "COMPLETED:", "PERCENT-COMPLETE:")
                for line in raw.splitlines():
                    if any(line.startswith(p) for p in drop_prefixes):
                        continue
                    if line.startswith("END:VTODO"):
                        lines.append("STATUS:COMPLETED")
                        lines.append(f"COMPLETED:{now_utc}")
                        lines.append("PERCENT-COMPLETE:100")
                    lines.append(line)
                target_todo.data = "\r\n".join(lines)
                target_todo.save()
            except Exception as inner:
                raise _translate(inner, self._app_password, "reminders_complete") from None

        return ToolResult(output={"uid": uid, "completed": True})
