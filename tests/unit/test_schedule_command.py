"""`/schedule list|enable|disable` slash command (FC-12, FC-6).

Three actions:
- `list` (default) → one row per loaded job:
  `skill:run_name | cron | next_fire_time | enabled?`
- `disable <skill>:<run_name>` → `SchedulerService.set_enabled(skill, run_name, False)`
- `enable  <skill>:<run_name>` → `SchedulerService.set_enabled(skill, run_name, True)`

Unknown target → `UnknownScheduleError` surfaced as a user-facing string.

The command takes a `SchedulerService`. We reuse the
`FakeSchedulerService` shape from `test_run_command.py` (duplicated
here to keep test-file isolation; tests are not supposed to import
from one another).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ember.commands.base import CommandContext, CommandOption
from ember.commands.registry import CommandRegistry
from ember.config import Settings
from ember.errors import UnknownScheduleError
from ember.scheduler.base import JobSnapshot
from ember.store.repo import Store


def _import_schedule_command():
    from ember.commands.schedule import ScheduleCommand

    return ScheduleCommand


# --- fakes ----------------------------------------------------------------


@dataclass
class _SetEnabledCall:
    skill: str
    run_name: str
    enabled: bool


class FakeSchedulerService:
    """Minimal SchedulerService fake for command tests."""

    def __init__(
        self,
        *,
        jobs: list[JobSnapshot] | None = None,
        set_enabled_raises: BaseException | None = None,
    ) -> None:
        self._jobs = list(jobs or [])
        self._set_enabled_raises = set_enabled_raises
        self.set_enabled_calls: list[_SetEnabledCall] = []

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def refresh(self) -> None: ...

    async def run_now(self, skill: str, function: str, run_name: str) -> None: ...

    async def set_enabled(self, skill: str, run_name: str, enabled: bool) -> None:
        self.set_enabled_calls.append(
            _SetEnabledCall(skill=skill, run_name=run_name, enabled=enabled)
        )
        if self._set_enabled_raises is not None:
            raise self._set_enabled_raises

    def list_jobs(self) -> list[JobSnapshot]:
        return list(self._jobs)


# --- fixtures -------------------------------------------------------------


def _make_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")
    monkeypatch.setenv("DISCORD_CONVERSATIONS_CHANNEL_ID", "2")
    monkeypatch.setenv("DISCORD_SCHEDULER_CHANNEL_ID", "3")
    return Settings()  # type: ignore[call-arg]


@pytest.fixture
async def wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    settings = _make_settings(monkeypatch, tmp_path)
    store = Store(tmp_path / "conversations.db")
    await store.open()
    registry = CommandRegistry()
    try:
        yield store, settings, registry
    finally:
        await store.close()


def _ctx(
    store: Store,
    settings: Settings,
    registry: CommandRegistry,
    *,
    args: dict | None = None,
) -> CommandContext:
    return CommandContext(
        conversation_key="thread-1",
        user_id="1000",
        store=store,
        settings=settings,
        registry=registry,
        args=args or {},
    )


# --- static surface -------------------------------------------------------


class TestStaticSurface:
    def test_name(self) -> None:
        ScheduleCommand = _import_schedule_command()
        assert ScheduleCommand.name == "schedule"

    def test_options_have_action_and_target(self) -> None:
        ScheduleCommand = _import_schedule_command()
        opts = {o.name: o for o in ScheduleCommand.options}
        assert "action" in opts
        # The "target" option carries `<skill>:<run_name>` for enable/disable.
        assert "target" in opts
        # Action defaults to "list" so bare `/schedule` works.
        action = opts["action"]
        assert isinstance(action, CommandOption)
        assert action.default == "list"
        if action.choices is not None:
            assert set(action.choices) == {"list", "enable", "disable"}


# --- /schedule list ------------------------------------------------------


class TestScheduleList:
    async def test_list_renders_one_row_per_job(self, wired) -> None:
        store, settings, registry = wired
        jobs = [
            JobSnapshot(
                skill="demo",
                run_name="morning",
                function="tick",
                cron="0 5 * * *",
                next_fire_time=datetime(2026, 4, 28, 5, 0, tzinfo=timezone.utc),
                enabled=True,
            ),
            JobSnapshot(
                skill="demo",
                run_name="evening",
                function="tick",
                cron="0 17 * * *",
                next_fire_time=datetime(2026, 4, 28, 17, 0, tzinfo=timezone.utc),
                enabled=False,
            ),
        ]
        scheduler = FakeSchedulerService(jobs=jobs)

        ScheduleCommand = _import_schedule_command()
        cmd = ScheduleCommand(scheduler=scheduler)

        out = await cmd.run(_ctx(store, settings, registry, args={"action": "list"}))
        assert isinstance(out, str)
        # Both runs surface in the listing.
        assert "morning" in out
        assert "evening" in out
        # Cron strings appear.
        assert "0 5 * * *" in out
        assert "0 17 * * *" in out

    async def test_list_when_no_jobs(self, wired) -> None:
        store, settings, registry = wired
        scheduler = FakeSchedulerService(jobs=[])

        ScheduleCommand = _import_schedule_command()
        cmd = ScheduleCommand(scheduler=scheduler)

        out = await cmd.run(_ctx(store, settings, registry, args={"action": "list"}))
        assert isinstance(out, str)
        # Some sort of "no jobs" message; not pinned to wording.
        lowered = out.lower()
        assert "no" in lowered or "empty" in lowered or "none" in lowered


# --- /schedule enable / disable ------------------------------------------


class TestScheduleToggle:
    async def test_disable_calls_set_enabled_false(self, wired) -> None:
        store, settings, registry = wired
        scheduler = FakeSchedulerService(
            jobs=[
                JobSnapshot(
                    skill="demo",
                    run_name="morning",
                    function="tick",
                    cron="0 5 * * *",
                    next_fire_time=None,
                    enabled=True,
                ),
            ]
        )

        ScheduleCommand = _import_schedule_command()
        cmd = ScheduleCommand(scheduler=scheduler)
        out = await cmd.run(
            _ctx(
                store,
                settings,
                registry,
                args={"action": "disable", "target": "demo:morning"},
            )
        )
        assert isinstance(out, str)
        assert len(scheduler.set_enabled_calls) == 1
        call = scheduler.set_enabled_calls[0]
        assert call.skill == "demo"
        assert call.run_name == "morning"
        assert call.enabled is False

    async def test_enable_calls_set_enabled_true(self, wired) -> None:
        store, settings, registry = wired
        scheduler = FakeSchedulerService(
            jobs=[
                JobSnapshot(
                    skill="demo",
                    run_name="morning",
                    function="tick",
                    cron="0 5 * * *",
                    next_fire_time=None,
                    enabled=False,
                ),
            ]
        )

        ScheduleCommand = _import_schedule_command()
        cmd = ScheduleCommand(scheduler=scheduler)
        await cmd.run(
            _ctx(
                store,
                settings,
                registry,
                args={"action": "enable", "target": "demo:morning"},
            )
        )
        assert len(scheduler.set_enabled_calls) == 1
        assert scheduler.set_enabled_calls[0].enabled is True

    async def test_unknown_target_surfaces_user_message(self, wired) -> None:
        store, settings, registry = wired
        scheduler = FakeSchedulerService(
            set_enabled_raises=UnknownScheduleError("demo:nope not loaded"),
        )

        ScheduleCommand = _import_schedule_command()
        cmd = ScheduleCommand(scheduler=scheduler)
        out = await cmd.run(
            _ctx(
                store,
                settings,
                registry,
                args={"action": "disable", "target": "demo:nope"},
            )
        )
        # Must not propagate; surfaces a user-facing string.
        assert isinstance(out, str)
        lowered = out.lower()
        assert "nope" in lowered or "unknown" in lowered

    async def test_malformed_target_returns_usage(self, wired) -> None:
        store, settings, registry = wired
        scheduler = FakeSchedulerService()

        ScheduleCommand = _import_schedule_command()
        cmd = ScheduleCommand(scheduler=scheduler)
        out = await cmd.run(
            _ctx(
                store,
                settings,
                registry,
                args={"action": "disable", "target": "not-a-colon-target"},
            )
        )
        # Some kind of usage hint; no scheduler call.
        assert isinstance(out, str)
        assert scheduler.set_enabled_calls == []
