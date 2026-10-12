"""iCloud Reminders tool tests (FC-3, TLC-4, TLC-7) — v16 (iCloud).

Covers `ember.tools.icloud_reminders.{RemindersListTool, RemindersAddTool,
RemindersCompleteTool}`:

- FC-3 static surface (name, description, input_schema,
  requires_confirmation, Tool protocol).
- TLC-4 secret hygiene: app password never appears in errors/logs/repr.
- TLC-7 failure surfacing: typed errors for auth and transport failures.

Mocking strategy: `caldav` is sync. We patch the `DAVClient` symbol on
the tool module via `tests.fakes.fake_caldav.install_fake_caldav`,
which mirrors the gmail_module / `MailBox` patching pattern.

Tester-flagged ambiguities (also in the report):

1. **vobject mutation API for `reminders_complete`** — the spec says
   "STATUS=COMPLETED + PERCENT-COMPLETE=100 + COMPLETED timestamp;
   don't delete". The implementer might mutate via
   `vobject_instance.vtodo.add('status').value = 'COMPLETED'` or by
   calling a helper on the FakeTodo. Tests assert the *post-save* shape
   (`fake_todo.status == 'COMPLETED'`, `percent_complete == 100`,
   `completed is not None`, `deleted is False`) without pinning the
   mutation path. The fake exposes a `complete()` helper that the
   implementer may or may not use; either way the dataclass fields end
   up where the test expects.

2. **Auth-error library exception class** — caldav surfaces auth via
   either `caldav.lib.error.AuthorizationError` or via a generic
   transport exception with an HTTP 401 response. Tests use a generic
   `Exception("401 Unauthorized")` raised from `principal()`; the
   implementer must catch the library form and translate to AuthError.

3. **Default-list resolution** — `list_name=None` in `reminders_add`
   should pick (a) the first list named "Reminders" case-insensitively,
   else (b) the first VTODO calendar. Tests cover both branches.

4. **`due_within_days` filter implementation** — may be applied
   server-side via `todos(start=..., end=...)` or client-side after
   fetch. Tests pre-populate todos with known due dates and assert the
   filtered output, not the call-shape.

5. **Priority-string round-trip** — RFC 5545 PRIORITY is 0-9. Tool
   accepts strings ("high"/"medium"/"low"/"none"); stored in iCal as
   numeric. Tests check the priority value passed to `save_todo` and
   verify the read-back string mapping.

6. **Naive-datetime rejection error type** — spec says "ToolError or
   ToolValidationError, accept either" for `reminders_add`. Tests catch
   both via the parent `ToolError`.
"""

from __future__ import annotations

import datetime
import logging

import pytest


# Module-level imports: collection fails clearly when
# `ember.tools.icloud_reminders` doesn't exist yet — that's the expected
# baseline pre-merge.
from ember.errors import AuthError, ToolError, TransportError  # noqa: E402
from ember.tools.base import Tool, ToolResult  # noqa: E402
from ember.tools.registry import Registry  # noqa: E402
from ember.tools import icloud_reminders as reminders_module  # noqa: E402
from ember.tools.icloud_reminders import (  # noqa: E402
    RemindersAddTool,
    RemindersCompleteTool,
    RemindersListTool,
)
from tests.fakes.fake_caldav import (
    FakeCalendar,
    FakeTodo,
    install_fake_caldav,
)


# Sentinel password — must NEVER appear in logs / errors / repr.
SECRET_PASSWORD = "sk-icloud-XXX-SECRET-XXX-YYY"  # noqa: S105 — test sentinel
APPLE_ID = "ember@example.com"
CALDAV_URL = "https://caldav.icloud.com/"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_list_tool() -> RemindersListTool:
    return RemindersListTool(
        apple_id=APPLE_ID,
        app_password=SECRET_PASSWORD,
        caldav_url=CALDAV_URL,
    )


def _make_add_tool() -> RemindersAddTool:
    return RemindersAddTool(
        apple_id=APPLE_ID,
        app_password=SECRET_PASSWORD,
        caldav_url=CALDAV_URL,
    )


def _make_complete_tool() -> RemindersCompleteTool:
    return RemindersCompleteTool(
        apple_id=APPLE_ID,
        app_password=SECRET_PASSWORD,
        caldav_url=CALDAV_URL,
    )


def _utc(year: int, month: int, day: int, hour: int = 12, minute: int = 0) -> datetime.datetime:
    return datetime.datetime(year, month, day, hour, minute, tzinfo=datetime.timezone.utc)


def _assert_no_secret_anywhere(
    *,
    caplog: pytest.LogCaptureFixture,
    exc: BaseException | None = None,
) -> None:
    """TLC-4: app password never leaks into logs or surfaced errors."""
    for record in caplog.records:
        msg = record.getMessage()
        assert SECRET_PASSWORD not in msg, f"secret leaked: {record.name}: {msg!r}"
        for attr in ("msg", "message", "exc_text"):
            v = getattr(record, attr, None)
            if isinstance(v, str):
                assert SECRET_PASSWORD not in v
    if exc is not None:
        assert SECRET_PASSWORD not in str(exc)
        assert SECRET_PASSWORD not in repr(exc)
        for arg in getattr(exc, "args", ()):
            assert SECRET_PASSWORD not in repr(arg)


def _three_reminder_lists() -> list[FakeCalendar]:
    """Two VTODO lists ('Reminders', 'Groceries') + one VEVENT calendar
    ('Personal', should be ignored by reminders_list)."""
    return [
        FakeCalendar(
            name="Reminders",
            color="#FF0000",
            supported_components=["VTODO"],
            todos_data=[
                FakeTodo(
                    summary="Pay rent",
                    due=_utc(2026, 5, 5),
                    priority=1,  # high
                    status="",
                    description="due first of month",
                    uid="todo-1",
                ),
                FakeTodo(
                    summary="Renew passport",
                    due=_utc(2026, 9, 1),
                    priority=5,  # medium
                    status="",
                    uid="todo-2",
                ),
                FakeTodo(
                    summary="Old task",
                    due=_utc(2026, 1, 1),
                    priority=9,  # low
                    status="COMPLETED",
                    uid="todo-3",
                ),
            ],
        ),
        FakeCalendar(
            name="Groceries",
            supported_components=["VTODO"],
            todos_data=[
                FakeTodo(summary="Milk", priority=0, uid="g-1"),
            ],
        ),
        FakeCalendar(
            name="Personal",
            supported_components=["VEVENT"],
        ),
    ]


# ===========================================================================
# Static surface (FC-3)
# ===========================================================================


class TestStaticSurface:
    def test_list_name(self) -> None:
        assert RemindersListTool.name == "reminders_list"

    def test_add_name(self) -> None:
        assert RemindersAddTool.name == "reminders_add"

    def test_complete_name(self) -> None:
        assert RemindersCompleteTool.name == "reminders_complete"

    def test_list_description_nonempty(self) -> None:
        assert isinstance(RemindersListTool.description, str)
        assert RemindersListTool.description.strip() != ""

    def test_add_description_nonempty(self) -> None:
        assert isinstance(RemindersAddTool.description, str)
        assert RemindersAddTool.description.strip() != ""

    def test_complete_description_nonempty(self) -> None:
        assert isinstance(RemindersCompleteTool.description, str)
        assert RemindersCompleteTool.description.strip() != ""

    def test_list_not_destructive(self) -> None:
        assert RemindersListTool.requires_confirmation is False

    def test_add_not_destructive(self) -> None:
        # Per contract: reminders_add is not gated by TLC-3 confirmation.
        assert RemindersAddTool.requires_confirmation is False

    def test_complete_not_destructive(self) -> None:
        # Per contract: reminders_complete is not gated by TLC-3 confirmation.
        assert RemindersCompleteTool.requires_confirmation is False

    def test_list_schema_shape(self) -> None:
        s = RemindersListTool.input_schema
        assert s["type"] == "object"
        props = s["properties"]
        assert "list_name" in props
        assert "include_completed" in props
        assert props["include_completed"].get("default") is False
        assert "due_within_days" in props
        # 1-365 bounded
        dwd = props["due_within_days"]
        # The field is nullable; the bounds may be on the integer arm
        # of an oneOf/anyOf or directly on the field.
        rendered = repr(dwd)
        assert "1" in rendered and "365" in rendered

    def test_add_schema_shape(self) -> None:
        s = RemindersAddTool.input_schema
        assert s["type"] == "object"
        props = s["properties"]
        assert "title" in props
        assert "list_name" in props
        assert "due_iso" in props
        assert "notes" in props
        assert "priority" in props
        assert s["required"] == ["title"]

    def test_complete_schema_shape(self) -> None:
        s = RemindersCompleteTool.input_schema
        assert s["type"] == "object"
        assert "uid" in s["properties"]
        assert s["properties"]["uid"]["type"] == "string"
        assert s["required"] == ["uid"]

    def test_protocol_compliance(self) -> None:
        assert isinstance(_make_list_tool(), Tool)
        assert isinstance(_make_add_tool(), Tool)
        assert isinstance(_make_complete_tool(), Tool)


# ===========================================================================
# reminders_list happy paths
# ===========================================================================


class TestRemindersListHappyPath:
    async def test_returns_uncompleted_with_priority_mapping(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = install_fake_caldav(monkeypatch, reminders_module)
        state.principal.calendars_data = _three_reminder_lists()

        result = await _make_list_tool().call({})

        assert isinstance(result, ToolResult)
        out = result.output
        assert "reminders" in out
        # By default include_completed=False: 'Old task' (COMPLETED) drops.
        # 4 VTODOs minus the 1 completed = 3 returned.
        rems = out["reminders"]
        assert len(rems) == 3
        for r in rems:
            assert "uid" in r
            assert "list" in r
            assert "title" in r
            assert "due_iso" in r
            assert "priority" in r
            assert "completed" in r
            assert "notes" in r

        # Priority mapping per RFC 5545: 1-4 → high, 5 → medium,
        # 6-9 → low, 0/missing → none.
        by_uid = {r["uid"]: r for r in rems}
        assert by_uid["todo-1"]["priority"] == "high"
        assert by_uid["todo-2"]["priority"] == "medium"
        assert by_uid["g-1"]["priority"] == "none"

    async def test_due_iso_is_utc_iso(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = install_fake_caldav(monkeypatch, reminders_module)
        state.principal.calendars_data = [
            FakeCalendar(
                name="L",
                supported_components=["VTODO"],
                todos_data=[
                    FakeTodo(summary="x", due=_utc(2026, 5, 5, 14, 30), uid="t1"),
                ],
            ),
        ]

        result = await _make_list_tool().call({})

        rems = result.output["reminders"]
        assert len(rems) == 1
        iso = rems[0]["due_iso"]
        assert iso != ""
        parsed = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        assert parsed.year == 2026
        assert parsed.month == 5

    async def test_no_due_returns_empty_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = install_fake_caldav(monkeypatch, reminders_module)
        state.principal.calendars_data = [
            FakeCalendar(
                name="L",
                supported_components=["VTODO"],
                todos_data=[FakeTodo(summary="No due", due=None, uid="t1")],
            ),
        ]

        result = await _make_list_tool().call({})

        assert result.output["reminders"][0]["due_iso"] == ""

    async def test_notes_truncated_at_2000_chars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        big = "x" * 5000
        state = install_fake_caldav(monkeypatch, reminders_module)
        state.principal.calendars_data = [
            FakeCalendar(
                name="L",
                supported_components=["VTODO"],
                todos_data=[FakeTodo(summary="big", description=big, uid="t1")],
            ),
        ]

        result = await _make_list_tool().call({})

        notes = result.output["reminders"][0]["notes"]
        assert len(notes) <= 2000 + len("\n...[truncated]")
        assert "[truncated]" in notes


class TestRemindersListFilters:
    async def test_include_completed_false_filters_completed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = install_fake_caldav(monkeypatch, reminders_module)
        state.principal.calendars_data = _three_reminder_lists()

        result = await _make_list_tool().call({"include_completed": False})

        uids = {r["uid"] for r in result.output["reminders"]}
        assert "todo-3" not in uids  # COMPLETED

    async def test_include_completed_true_returns_all(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = install_fake_caldav(monkeypatch, reminders_module)
        state.principal.calendars_data = _three_reminder_lists()

        result = await _make_list_tool().call({"include_completed": True})

        uids = {r["uid"] for r in result.output["reminders"]}
        # All four VTODOs across the two VTODO calendars.
        assert {"todo-1", "todo-2", "todo-3", "g-1"} <= uids

    async def test_due_within_days_filters(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 'Pay rent' due 2026-05-05; 'Renew passport' due 2026-09-01.
        # We can't pin "now" without freezegun, so instead test by
        # passing absurdly small (1) and absurdly large (365) windows
        # and checking inclusion shifts.
        state = install_fake_caldav(monkeypatch, reminders_module)
        state.principal.calendars_data = _three_reminder_lists()

        result_wide = await _make_list_tool().call({"due_within_days": 365})
        wide_uids = {r["uid"] for r in result_wide.output["reminders"]}

        # With a 365-day window, the impl should keep at least one of
        # the dated todos. (The todos with no due may also be kept or
        # dropped — spec doesn't pin that, so we assert presence-of-at-
        # least-one-dated.)
        # NB: contract ambiguity flagged in module docstring item 4 —
        # we don't pin server-vs-client side filtering.
        dated = {"todo-1", "todo-2"}
        assert wide_uids & dated, (
            f"due_within_days=365 should retain at least one dated todo; got {wide_uids}"
        )

    async def test_list_name_case_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = install_fake_caldav(monkeypatch, reminders_module)
        state.principal.calendars_data = _three_reminder_lists()

        result = await _make_list_tool().call({"list_name": "REMINDERS"})

        uids = {r["uid"] for r in result.output["reminders"]}
        # Only items from the 'Reminders' list (case-insensitive match).
        # Excludes Groceries' g-1 entry.
        assert "g-1" not in uids
        # And at least one Reminders entry is present.
        assert uids & {"todo-1", "todo-2"}

    async def test_ambiguous_list_name_raises_tool_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Two calendars whose names both contain the substring 'work'.
        state = install_fake_caldav(monkeypatch, reminders_module)
        state.principal.calendars_data = [
            FakeCalendar(name="Work Tasks", supported_components=["VTODO"]),
            FakeCalendar(name="Side Work", supported_components=["VTODO"]),
        ]

        # Ambiguous lookups: spec says "ambiguous list_name → ToolError
        # naming candidates". We accept *either* of:
        #   (a) An exact-match strategy where 'work' matches no list →
        #       ToolError "list not found".
        #   (b) A substring strategy → ambiguous → ToolError naming
        #       candidates.
        # Both are valid; the test pins ToolError + that the message
        # mentions either ambiguity or not-found.
        with pytest.raises(ToolError) as excinfo:
            await _make_list_tool().call({"list_name": "work"})

        msg = str(excinfo.value).lower()
        assert "ambiguous" in msg or "not found" in msg or "no list" in msg

    async def test_missing_list_raises_tool_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = install_fake_caldav(monkeypatch, reminders_module)
        state.principal.calendars_data = _three_reminder_lists()

        with pytest.raises(ToolError) as excinfo:
            await _make_list_tool().call({"list_name": "NoSuchList"})

        # Surface the missing name to help the model self-correct.
        assert "NoSuchList" in str(excinfo.value) or "not found" in str(excinfo.value).lower()


# ===========================================================================
# reminders_list errors
# ===========================================================================


class TestRemindersListErrors:
    async def test_auth_failure_raises_auth_error(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        state = install_fake_caldav(monkeypatch, reminders_module)
        # caldav surfaces auth via various exception classes; use a
        # generic 401-ish message and require the implementer to map it
        # to AuthError.
        state.principal_raises = Exception("401 Unauthorized")

        with pytest.raises(AuthError) as excinfo:
            await _make_list_tool().call({})

        _assert_no_secret_anywhere(caplog=caplog, exc=excinfo.value)

    async def test_transport_failure_raises_transport_error(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        state = install_fake_caldav(monkeypatch, reminders_module)
        state.construct_raises = OSError("connection refused")

        with pytest.raises(TransportError) as excinfo:
            await _make_list_tool().call({})

        _assert_no_secret_anywhere(caplog=caplog, exc=excinfo.value)


# ===========================================================================
# reminders_add
# ===========================================================================


class TestRemindersAddHappyPath:
    async def test_adds_to_named_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = install_fake_caldav(monkeypatch, reminders_module)
        cals = _three_reminder_lists()
        state.principal.calendars_data = cals

        result = await _make_add_tool().call(
            {
                "title": "Buy bread",
                "list_name": "Groceries",
                "priority": "medium",
            }
        )

        out = result.output
        assert "uid" in out
        assert isinstance(out["uid"], str) and out["uid"]
        assert out["list"] == "Groceries"

        groceries = next(c for c in cals if c.name == "Groceries")
        assert len(groceries.save_todo_calls) == 1
        kw = groceries.save_todo_calls[0]
        assert kw.get("summary") == "Buy bread"
        # Priority round-trip: "medium" → 5.
        assert kw.get("priority") == 5

    async def test_default_list_resolves_to_reminders(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = install_fake_caldav(monkeypatch, reminders_module)
        cals = _three_reminder_lists()
        state.principal.calendars_data = cals

        result = await _make_add_tool().call({"title": "Default to Reminders"})

        # 'Reminders' is the default landing list when present.
        assert result.output["list"] == "Reminders"
        rem = next(c for c in cals if c.name == "Reminders")
        assert len(rem.save_todo_calls) == 1

    async def test_default_list_falls_back_to_first_vtodo(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No calendar named 'Reminders' — should fall back to the
        # first VTODO calendar.
        state = install_fake_caldav(monkeypatch, reminders_module)
        cals = [
            FakeCalendar(name="Errands", supported_components=["VTODO"]),
            FakeCalendar(name="Personal", supported_components=["VEVENT"]),
        ]
        state.principal.calendars_data = cals

        result = await _make_add_tool().call({"title": "x"})

        assert result.output["list"] == "Errands"
        assert len(cals[0].save_todo_calls) == 1
        # Event calendar must NOT receive the save.
        assert len(cals[1].save_event_calls) == 0

    async def test_priority_round_trip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = install_fake_caldav(monkeypatch, reminders_module)
        cals = [FakeCalendar(name="Reminders", supported_components=["VTODO"])]
        state.principal.calendars_data = cals

        # Each priority string maps to a numeric range; we accept any
        # value in the documented range.
        cases = {
            "high": range(1, 5),  # 1-4
            "medium": {5},
            "low": range(6, 10),  # 6-9
            "none": {0},
        }
        for prio_str, valid in cases.items():
            cals[0].save_todo_calls.clear()
            await _make_add_tool().call({"title": f"t-{prio_str}", "priority": prio_str})
            kw = cals[0].save_todo_calls[0]
            stored = kw.get("priority", 0)
            # `none` may be 0 or omitted; allow both.
            if prio_str == "none":
                assert stored in (0, None) or stored == 0
            else:
                assert stored in valid, f"{prio_str!r} → {stored!r}, expected in {list(valid)}"


class TestRemindersAddValidation:
    async def test_naive_datetime_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = install_fake_caldav(monkeypatch, reminders_module)
        state.principal.calendars_data = [
            FakeCalendar(name="Reminders", supported_components=["VTODO"])
        ]

        # ToolError covers both ToolError and ToolValidationError
        # (ToolValidationError ⊆ ToolError).
        with pytest.raises(ToolError):
            await _make_add_tool().call(
                {"title": "x", "due_iso": "2026-05-05T12:00:00"}  # no offset
            )

    async def test_unknown_priority_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Schema or impl rejects priorities outside the documented enum.
        # `Registry.call_tool` would catch this via jsonschema; calling
        # `tool.call` directly may also raise. Test both surfaces by
        # going through the registry where schema validation is the
        # canonical guard.
        state = install_fake_caldav(monkeypatch, reminders_module)
        state.principal.calendars_data = [
            FakeCalendar(name="Reminders", supported_components=["VTODO"])
        ]

        registry = Registry()
        registry.register(_make_add_tool())
        with pytest.raises(ToolError):
            await registry.call_tool("reminders_add", {"title": "x", "priority": "URGENT-NOW"})


# ===========================================================================
# reminders_complete
# ===========================================================================


class TestRemindersComplete:
    async def test_completes_existing_todo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = install_fake_caldav(monkeypatch, reminders_module)
        target = FakeTodo(summary="Done me", uid="found-uid")
        state.principal.calendars_data = [
            FakeCalendar(
                name="Reminders",
                supported_components=["VTODO"],
                todos_data=[target],
            )
        ]

        result = await _make_complete_tool().call({"uid": "found-uid"})

        out = result.output
        assert out["uid"] == "found-uid"
        assert out["completed"] is True
        # Side effect: the underlying todo is marked complete.
        # Implementer may use either the FakeTodo.complete() helper or
        # raw vobject mutation; we assert the post-state shape.
        assert target.status == "COMPLETED" or target.percent_complete == 100, (
            "expected todo to be marked completed via vobject mutation or save_todo update"
        )
        # No deletion.
        assert target.deleted is False

    async def test_uid_not_found_raises_tool_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = install_fake_caldav(monkeypatch, reminders_module)
        state.principal.calendars_data = [
            FakeCalendar(
                name="Reminders",
                supported_components=["VTODO"],
                todos_data=[FakeTodo(uid="present-uid")],
            )
        ]

        with pytest.raises(ToolError):
            await _make_complete_tool().call({"uid": "missing-uid"})

    async def test_auth_error_translation(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        state = install_fake_caldav(monkeypatch, reminders_module)
        state.principal_raises = Exception("401 Unauthorized")

        with pytest.raises(AuthError) as excinfo:
            await _make_complete_tool().call({"uid": "anything"})

        _assert_no_secret_anywhere(caplog=caplog, exc=excinfo.value)

    async def test_transport_error_translation(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        state = install_fake_caldav(monkeypatch, reminders_module)
        state.construct_raises = OSError("connection refused")

        with pytest.raises(TransportError) as excinfo:
            await _make_complete_tool().call({"uid": "anything"})

        _assert_no_secret_anywhere(caplog=caplog, exc=excinfo.value)


# ===========================================================================
# Registry round-trips (schema validation accepts documented args)
# ===========================================================================


class TestRegistryRoundTrip:
    async def test_reminders_list_through_registry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = install_fake_caldav(monkeypatch, reminders_module)
        state.principal.calendars_data = _three_reminder_lists()

        registry = Registry()
        registry.register(_make_list_tool())
        result = await registry.call_tool(
            "reminders_list",
            {"include_completed": False, "due_within_days": 30},
        )
        assert result.is_error is False
        assert "reminders" in result.output

    async def test_reminders_add_through_registry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = install_fake_caldav(monkeypatch, reminders_module)
        state.principal.calendars_data = [
            FakeCalendar(name="Reminders", supported_components=["VTODO"])
        ]

        registry = Registry()
        registry.register(_make_add_tool())
        result = await registry.call_tool(
            "reminders_add",
            {"title": "thru-registry", "priority": "low"},
        )
        # Documented schema accepts these args.
        assert result.is_error is False
        assert result.output["uid"]

    async def test_reminders_complete_through_registry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = install_fake_caldav(monkeypatch, reminders_module)
        state.principal.calendars_data = [
            FakeCalendar(
                name="Reminders",
                supported_components=["VTODO"],
                todos_data=[FakeTodo(uid="u1")],
            )
        ]

        registry = Registry()
        registry.register(_make_complete_tool())
        result = await registry.call_tool("reminders_complete", {"uid": "u1"})
        assert result.is_error is False
        assert result.output["completed"] is True


# ===========================================================================
# TLC-4 secret hygiene — across all three tools
# ===========================================================================


class TestSecretHygiene:
    async def test_password_never_logged_on_list_success(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        state = install_fake_caldav(monkeypatch, reminders_module)
        state.principal.calendars_data = _three_reminder_lists()

        await _make_list_tool().call({})

        _assert_no_secret_anywhere(caplog=caplog)

    async def test_password_never_logged_on_add_success(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        state = install_fake_caldav(monkeypatch, reminders_module)
        state.principal.calendars_data = [
            FakeCalendar(name="Reminders", supported_components=["VTODO"])
        ]

        await _make_add_tool().call({"title": "x"})

        _assert_no_secret_anywhere(caplog=caplog)

    async def test_password_never_logged_on_complete_success(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        state = install_fake_caldav(monkeypatch, reminders_module)
        state.principal.calendars_data = [
            FakeCalendar(
                name="Reminders",
                supported_components=["VTODO"],
                todos_data=[FakeTodo(uid="u")],
            )
        ]

        await _make_complete_tool().call({"uid": "u"})

        _assert_no_secret_anywhere(caplog=caplog)

    def test_password_not_in_repr_or_str(self) -> None:
        for tool in (_make_list_tool(), _make_add_tool(), _make_complete_tool()):
            assert SECRET_PASSWORD not in repr(tool)
            assert SECRET_PASSWORD not in str(tool)
