"""Fakes for the `caldav` library used by `ember.tools.icloud_*`.

The real `caldav` library is sync. The two iCloud tool modules
(`icloud_reminders.py`, `icloud_calendar.py`) are expected to:

  - `import caldav` and use `caldav.DAVClient(...)` to construct the
    client (so tests can monkeypatch `caldav_module.DAVClient`).
  - Call `.principal()` on the client.
  - Iterate `.calendars()` on the principal.
  - Use `.todos(...)`, `.search(...)`, `.save_todo(...)`, `.save_event(...)`
    on individual calendars.

These fakes mirror that surface area liberally — the tester can't see
the implementer's code so the contract is what matters; minor method
renames will be harmonized at merge time.

VOBJECT-ish data
----------------
Real caldav exposes parsed iCalendar via a `vobject_instance` attribute
whose nested attributes are vobject ContentLine wrappers with `.value`.
We mimic that with `_VLine` so the implementer can write
`todo.vobject_instance.vtodo.summary.value` and similar.

Recurrence expansion
--------------------
The real `Calendar.search(expand=True, ...)` returns one fake event
*per occurrence*; we replicate that by storing pre-expanded occurrences
on the fake calendar and returning them straight.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# vobject-ish line wrapper
# ---------------------------------------------------------------------------


class _VLine:
    """Mimics a vobject ContentLine: exposes `.value` and `.params`."""

    def __init__(self, value: Any, params: dict[str, Any] | None = None) -> None:
        self.value = value
        self.params = dict(params or {})


class _VComponent:
    """Container with attribute-style access to vobject lines.

    Real vobject lets you write `vtodo.summary.value` *or* call
    `vtodo.contents['summary'][0].value`. We support the attribute form
    only — sufficient for the current contract.
    """

    def __init__(self, **lines: _VLine) -> None:
        # Stash known lines as attributes so callers can do
        # `vtodo.summary.value`. Lines whose `.value is None` are
        # treated as absent (mirrors real vobject AttributeError on
        # missing properties).
        for key, line in lines.items():
            if line is None or line.value is None:
                continue
            setattr(self, key, line)

    def add(self, name: str, value: Any = None, params: dict[str, Any] | None = None) -> _VLine:
        """Set or replace a line. Real vobject's `.add()` returns the new
        ContentLine and accepts a name-only call; the caller assigns
        `.value` after. We mirror that so `vtodo.add('status').value =
        'COMPLETED'` works."""
        line = _VLine(value, params)
        # vobject normalizes property names: lowercase, hyphens → underscores.
        attr = name.lower().replace("-", "_")
        setattr(self, attr, line)
        return line


class _VObjectInstance:
    """Top-level iCal object. Exposes `.vtodo` or `.vevent` depending."""

    def __init__(
        self, *, vtodo: _VComponent | None = None, vevent: _VComponent | None = None
    ) -> None:
        if vtodo is not None:
            self.vtodo = vtodo
        if vevent is not None:
            self.vevent = vevent

    def serialize(self) -> str:
        """Real vobject returns the serialized iCal string. We don't
        need a real serialization for the tool tests, but having a
        method here means a defensive `.serialize()` call won't crash."""
        return "BEGIN:VCALENDAR\nEND:VCALENDAR\n"


# ---------------------------------------------------------------------------
# Fake todo / event objects
# ---------------------------------------------------------------------------


@dataclass
class FakeTodo:
    """A fake VTODO. Exposes `vobject_instance.vtodo.<line>.value`."""

    summary: str = ""
    due: datetime.datetime | datetime.date | None = None
    priority: int = 0
    status: str = ""
    description: str = ""
    uid: str = ""
    completed: datetime.datetime | None = None
    percent_complete: int = 0
    saved: bool = False  # set True after .save()
    deleted: bool = False

    def __post_init__(self) -> None:
        if not self.uid:
            self.uid = str(uuid.uuid4())
        self._vobj: _VObjectInstance | None = None

    @property
    def vobject_instance(self) -> _VObjectInstance:
        # Cache so impl mutations on returned vtodo lines persist across
        # repeated access, then sync back in save().
        if self._vobj is None:
            vtodo = _VComponent(
                summary=_VLine(self.summary) if self.summary else None,
                due=_VLine(self.due) if self.due is not None else None,
                priority=_VLine(self.priority) if self.priority else None,
                status=_VLine(self.status) if self.status else None,
                description=_VLine(self.description) if self.description else None,
                uid=_VLine(self.uid),
                completed=_VLine(self.completed) if self.completed is not None else None,
                percent_complete=(_VLine(self.percent_complete) if self.percent_complete else None),
            )
            vtodo._owner = self  # type: ignore[attr-defined]
            self._vobj = _VObjectInstance(vtodo=vtodo)
        return self._vobj

    def save(self) -> "FakeTodo":
        """Sync mutations on the cached vtodo back to dataclass fields."""
        if self._vobj is not None:
            vtodo = self._vobj.vtodo
            for src, dst in (
                ("summary", "summary"),
                ("status", "status"),
                ("description", "description"),
                ("priority", "priority"),
                ("due", "due"),
                ("completed", "completed"),
                ("percent_complete", "percent_complete"),
            ):
                line = getattr(vtodo, src, None)
                if line is None:
                    continue
                val = line.value
                if val is None:
                    continue
                if dst in ("priority", "percent_complete"):
                    try:
                        val = int(val)
                    except (TypeError, ValueError):
                        continue
                setattr(self, dst, val)
        self.saved = True
        return self

    def complete(self) -> None:
        """Helper for tests / impl to mark a VTODO complete in one step."""
        self.status = "COMPLETED"
        self.percent_complete = 100
        self.completed = datetime.datetime.now(datetime.timezone.utc)

    def delete(self) -> None:
        self.deleted = True


@dataclass
class FakeEvent:
    """A fake VEVENT. Exposes `vobject_instance.vevent.<line>.value`.

    For recurring events, the calendar's `.search(expand=True)` returns
    one FakeEvent per occurrence, all sharing the same `uid` but with
    distinct `dtstart`/`dtend`.
    """

    summary: str = ""
    dtstart: datetime.datetime | datetime.date | None = None
    dtend: datetime.datetime | datetime.date | None = None
    description: str = ""
    location: str = ""
    uid: str = ""
    rrule: str = ""
    saved: bool = False

    def __post_init__(self) -> None:
        if not self.uid:
            self.uid = str(uuid.uuid4())

    @property
    def vobject_instance(self) -> _VObjectInstance:
        # Pass TZID param when the dtstart/dtend has a non-UTC tzinfo so
        # the implementer can detect "America/Chicago" et al.
        dtstart_params: dict[str, Any] = {}
        dtend_params: dict[str, Any] = {}
        if isinstance(self.dtstart, datetime.datetime) and self.dtstart.tzinfo is not None:
            tz_name = getattr(self.dtstart.tzinfo, "key", None) or str(self.dtstart.tzinfo)
            if tz_name and tz_name != "UTC":
                dtstart_params["TZID"] = tz_name
        if isinstance(self.dtend, datetime.datetime) and self.dtend.tzinfo is not None:
            tz_name = getattr(self.dtend.tzinfo, "key", None) or str(self.dtend.tzinfo)
            if tz_name and tz_name != "UTC":
                dtend_params["TZID"] = tz_name

        vevent = _VComponent(
            summary=_VLine(self.summary) if self.summary else None,
            dtstart=_VLine(self.dtstart, dtstart_params) if self.dtstart is not None else None,
            dtend=_VLine(self.dtend, dtend_params) if self.dtend is not None else None,
            description=_VLine(self.description) if self.description else None,
            location=_VLine(self.location) if self.location else None,
            uid=_VLine(self.uid),
            rrule=_VLine(self.rrule) if self.rrule else None,
        )
        return _VObjectInstance(vevent=vevent)

    def save(self) -> "FakeEvent":
        self.saved = True
        return self


# ---------------------------------------------------------------------------
# Fake calendar
# ---------------------------------------------------------------------------


@dataclass
class FakeCalendar:
    """A fake CalDAV calendar.

    Two component-type modes:
      - `supported_components=["VTODO"]` → reminder list.
      - `supported_components=["VEVENT"]` → event calendar.
      - Mixed allowed but iCloud splits them, so we don't model that.

    Read/write privileges are advertised via `privileges` (a set of
    strings). The impl is expected to detect "no write privilege" on
    subscribed calendars and surface `read_only=True`.
    """

    name: str = "Default"
    color: str = "#FF0000"
    supported_components: list[str] = field(default_factory=lambda: ["VTODO"])
    privileges: set[str] = field(default_factory=lambda: {"read", "write"})
    todos_data: list[FakeTodo] = field(default_factory=list)
    events_data: list[FakeEvent] = field(default_factory=list)
    # Pre-expanded occurrences keyed by (start, end) for `.search(expand=True)`.
    expanded_occurrences: list[FakeEvent] = field(default_factory=list)
    # Raise on save for read-only test paths.
    save_raises: BaseException | None = None
    todos_raises: BaseException | None = None
    search_raises: BaseException | None = None

    # Call recording.
    todos_calls: list[dict[str, Any]] = field(default_factory=list)
    search_calls: list[dict[str, Any]] = field(default_factory=list)
    save_todo_calls: list[dict[str, Any]] = field(default_factory=list)
    save_event_calls: list[dict[str, Any]] = field(default_factory=list)

    # ----- caldav surface -----

    def get_supported_components(self) -> list[str]:
        return list(self.supported_components)

    def get_properties(
        self, props: list[Any] | None = None, *args: Any, **kwargs: Any
    ) -> dict[Any, Any]:
        # Real caldav returns a dict keyed by the requested property tuple.
        # We honor the passed-in tuples so impl code that asks specifically
        # for displayname / color / current-user-privilege-set gets just
        # that value back.
        out: dict[Any, Any] = {}
        for key in props or []:
            ns_local = key if isinstance(key, tuple) else (None, str(key))
            local_lower = str(ns_local[1]).lower()
            if local_lower == "displayname":
                out[key] = self.name
            elif local_lower == "calendar-color":
                out[key] = self.color
            elif local_lower == "current-user-privilege-set":
                # Render an XML-ish blob containing a "write" tag iff write
                # is granted. Impl scans for the literal substring "write".
                priv_tags = "".join(f"<{p} />" for p in sorted(self.privileges))
                out[key] = f"<privilege-set>{priv_tags}</privilege-set>"
            else:
                out[key] = None
        return out

    @property
    def read_only(self) -> bool:
        # Convenience for tests; implementer may compute read-only via
        # privilege-set lookup or via this flag.
        return "write" not in self.privileges

    def todos(
        self,
        *,
        sort_keys: tuple[str, ...] = ("due",),
        include_completed: bool = False,
        **kwargs: Any,
    ) -> list[FakeTodo]:
        if self.todos_raises is not None:
            raise self.todos_raises
        self.todos_calls.append(
            {"sort_keys": sort_keys, "include_completed": include_completed, **kwargs}
        )
        result = list(self.todos_data)
        if not include_completed:
            result = [t for t in result if t.status != "COMPLETED"]
        for t in result:
            t.parent = self
        return result

    def search(self, **kwargs: Any) -> list[FakeEvent]:
        if self.search_raises is not None:
            raise self.search_raises
        self.search_calls.append(dict(kwargs))
        if kwargs.get("expand") and self.expanded_occurrences:
            result = list(self.expanded_occurrences)
        else:
            result = list(self.events_data)
        for e in result:
            e.parent = self
        return result

    def save_todo(self, *args: Any, **kwargs: Any) -> FakeTodo:
        if self.save_raises is not None:
            raise self.save_raises
        # Real caldav accepts an ical string either as a positional arg
        # OR as `ical=...` kwarg. Mirror both.
        ical: str | None = None
        if args and isinstance(args[0], str):
            ical = args[0]
        elif isinstance(kwargs.get("ical"), str):
            ical = kwargs.pop("ical")
        if ical is not None:
            priority_raw = _extract_ical_field(ical, "PRIORITY")
            try:
                priority = int(priority_raw) if priority_raw else 0
            except ValueError:
                priority = 0
            summary = _extract_ical_field(ical, "SUMMARY")
            description = _extract_ical_field(ical, "DESCRIPTION")
            uid = _extract_ical_field(ical, "UID") or str(uuid.uuid4())
            # Record both the raw ICS and the parsed fields so tests can
            # assert against either shape.
            self.save_todo_calls.append(
                {
                    "ical": ical,
                    "summary": summary,
                    "description": description,
                    "priority": priority,
                    "uid": uid,
                    **kwargs,
                }
            )
            todo = FakeTodo(
                summary=summary,
                description=description,
                priority=priority,
                uid=uid,
            )
        else:
            self.save_todo_calls.append(dict(kwargs))
            todo = FakeTodo(
                summary=kwargs.get("summary", ""),
                due=kwargs.get("due"),
                priority=int(kwargs.get("priority", 0) or 0),
                description=kwargs.get("description", "") or "",
                uid=kwargs.get("uid", "") or str(uuid.uuid4()),
            )
        self.todos_data.append(todo)
        return todo

    def save_event(self, *args: Any, **kwargs: Any) -> FakeEvent:
        if self.save_raises is not None:
            raise self.save_raises
        # Real caldav accepts either an ical string positional OR
        # keyword fields. We accept both.
        if args and isinstance(args[0], str):
            ical = args[0]
            self.save_event_calls.append({"ical": ical, **kwargs})
            event = FakeEvent(
                summary=_extract_ical_field(ical, "SUMMARY"),
                dtstart=_extract_ical_field(ical, "DTSTART"),
                dtend=_extract_ical_field(ical, "DTEND"),
                location=_extract_ical_field(ical, "LOCATION") or "",
                description=_extract_ical_field(ical, "DESCRIPTION") or "",
                uid=_extract_ical_field(ical, "UID") or str(uuid.uuid4()),
            )
        else:
            self.save_event_calls.append(dict(kwargs))
            event = FakeEvent(
                summary=kwargs.get("summary", ""),
                dtstart=kwargs.get("dtstart"),
                dtend=kwargs.get("dtend"),
                location=kwargs.get("location", "") or "",
                description=kwargs.get("description", "") or "",
                uid=kwargs.get("uid", "") or str(uuid.uuid4()),
            )
        self.events_data.append(event)
        return event


def _extract_ical_field(ical: str, name: str) -> str:
    """Tiny iCal-line extractor for fake save_event(ical=...) paths.

    Returns the raw value string (no datetime parsing) — adequate for
    the implementer's round-trip tests since we control both sides of
    the fake.
    """
    for line in ical.splitlines():
        # Strip params: SUMMARY:foo or SUMMARY;CHARSET=...:foo
        head, sep, value = line.partition(":")
        if not sep:
            continue
        prop = head.split(";", 1)[0].strip().upper()
        if prop == name.upper():
            return value
    return ""


# ---------------------------------------------------------------------------
# Fake principal / client
# ---------------------------------------------------------------------------


@dataclass
class FakePrincipal:
    calendars_data: list[FakeCalendar] = field(default_factory=list)
    calendars_raises: BaseException | None = None

    def calendars(self) -> list[FakeCalendar]:
        if self.calendars_raises is not None:
            raise self.calendars_raises
        return list(self.calendars_data)


@dataclass
class FakeDAVClient:
    """The class the test patches in for `caldav.DAVClient`.

    Construction records the (url, username, password) so tests can
    assert the auth payload, and so secret-leak checks can scan the
    captured value.
    """

    url: str = ""
    username: str = ""
    password: str = ""
    timeout: float | None = None
    principal_obj: FakePrincipal | None = None
    principal_raises: BaseException | None = None
    construct_raises: BaseException | None = None
    construct_calls: list[dict[str, Any]] = field(default_factory=list)

    def __init__(
        self,
        url: str = "",
        username: str = "",
        password: str = "",
        *,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> None:
        # Class-level state (set by `install_fake_caldav` below) lets
        # multiple constructions reach the same fixture.
        self._state: _FakeCaldavState = _CURRENT_STATE  # type: ignore[name-defined]
        if self._state.construct_raises is not None:
            raise self._state.construct_raises
        self.url = url
        self.username = username
        self.password = password
        self.timeout = timeout
        self._state.construct_calls.append(
            {"url": url, "username": username, "password": password, "timeout": timeout, **kwargs}
        )

    def principal(self) -> FakePrincipal:
        if self._state.principal_raises is not None:
            raise self._state.principal_raises
        return self._state.principal


# ---------------------------------------------------------------------------
# Test-side state holder + monkeypatch helper
# ---------------------------------------------------------------------------


class _FakeCaldavState:
    """Shared state between the patched `DAVClient` class and the test."""

    def __init__(self) -> None:
        self.principal: FakePrincipal = FakePrincipal()
        self.construct_calls: list[dict[str, Any]] = []
        self.construct_raises: BaseException | None = None
        self.principal_raises: BaseException | None = None


# Module-level "current state" referenced by the FakeDAVClient class
# constructor. Assigned by `install_fake_caldav` before any client
# construction is triggered. This works because each test installs a
# fresh state at the top of the test (the implementer constructs the
# client inside the tool's `call()` body).
_CURRENT_STATE: _FakeCaldavState | None = None


def install_fake_caldav(
    monkeypatch: Any,
    tool_module: Any,
    state: _FakeCaldavState | None = None,
) -> _FakeCaldavState:
    """Patch `tool_module.caldav.DAVClient` (or `tool_module.DAVClient`)
    to the fake. Returns the state so the test can mutate
    `state.principal.calendars_data` / `state.construct_raises` etc.
    """
    global _CURRENT_STATE
    if state is None:
        state = _FakeCaldavState()
    _CURRENT_STATE = state

    # Patch `DAVClient` on the real `caldav` module so lazy imports
    # inside the tool (e.g. `import caldav; caldav.DAVClient(...)` inside
    # a method) pick up the fake. Also patch any module-level alias the
    # tool module may have exposed, defensively.
    import caldav as _real_caldav

    monkeypatch.setattr(_real_caldav, "DAVClient", FakeDAVClient, raising=True)
    if hasattr(tool_module, "caldav"):
        monkeypatch.setattr(tool_module.caldav, "DAVClient", FakeDAVClient, raising=False)
    if hasattr(tool_module, "DAVClient"):
        monkeypatch.setattr(tool_module, "DAVClient", FakeDAVClient, raising=False)
    return state


__all__ = [
    "FakeCalendar",
    "FakeDAVClient",
    "FakeEvent",
    "FakePrincipal",
    "FakeTodo",
    "_FakeCaldavState",
    "_VComponent",
    "_VLine",
    "_VObjectInstance",
    "install_fake_caldav",
]
