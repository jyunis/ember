"""`/run <skill> <function> [run_name]` slash command (FC-12, FC-6).

Per FC-6: "Manual trigger same code path." `/run` invokes
`SchedulerService.run_now(skill, function, run_name)`. The command
accepts `<skill> <function> [run_name]` with `run_name` defaulting to
the function name when there is exactly one matching schedule for
`(skill, function)`. If a function has multiple `run_name`s scheduled,
the command requires the user to supply `run_name` explicitly to
disambiguate.

Spec resolution chosen here: simple `(skill, function, run_name?)`
form. If the implementer chose to require `run_name` always, the test
that exercises the default path will surface that.

Tests use a `FakeSchedulerService` because we want to assert what
arguments the command forwards. The implementer's `RunCommand`
constructor takes a `SchedulerService` (Protocol). The fake matches
its surface but lets the test capture invocations.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from ember.commands.base import CommandContext
from ember.commands.registry import CommandRegistry
from ember.config import Settings
from ember.errors import UnknownScheduleError
from ember.scheduler.base import JobSnapshot, ScheduledFunction, ScheduleSpec
from ember.skills.base import ImperativeSkill
from ember.store.repo import Store


def _import_run_command():
    from ember.commands.run import RunCommand

    return RunCommand


# --- fakes ----------------------------------------------------------------


@dataclass
class _RunNowCall:
    skill: str
    function: str
    run_name: str


class FakeSchedulerService:
    """A bare-minimum `SchedulerService` stand-in for command tests.

    Implements only what the slash commands need: `run_now`,
    `set_enabled`, `list_jobs`, plus the ability to surface a
    pre-programmed `UnknownScheduleError`.
    """

    def __init__(
        self,
        *,
        jobs: list[JobSnapshot] | None = None,
        run_now_raises: BaseException | None = None,
        set_enabled_raises: BaseException | None = None,
    ) -> None:
        self._jobs = list(jobs or [])
        self._run_now_raises = run_now_raises
        self._set_enabled_raises = set_enabled_raises
        self.run_now_calls: list[_RunNowCall] = []
        self.set_enabled_calls: list[tuple[str, str, bool]] = []

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def refresh(self) -> None: ...

    async def run_now(self, skill: str, function: str, run_name: str) -> None:
        self.run_now_calls.append(_RunNowCall(skill=skill, function=function, run_name=run_name))
        if self._run_now_raises is not None:
            raise self._run_now_raises

    async def set_enabled(self, skill: str, run_name: str, enabled: bool) -> None:
        self.set_enabled_calls.append((skill, run_name, enabled))
        if self._set_enabled_raises is not None:
            raise self._set_enabled_raises

    def list_jobs(self) -> list[JobSnapshot]:
        return list(self._jobs)


class FakeSkillRegistry:
    """Just enough surface for `RunCommand`'s default-`run_name` lookup."""

    def __init__(self, skills: dict[str, ImperativeSkill]) -> None:
        self._skills = dict(skills)

    def get(self, name: str) -> ImperativeSkill:
        return self._skills[name]

    def list(self) -> list[ImperativeSkill]:
        return list(self._skills.values())

    def names(self) -> list[str]:
        return sorted(self._skills.keys())

    def replace(self, skill: ImperativeSkill) -> None:
        self._skills[skill.name] = skill

    def remove(self, name: str) -> None:
        self._skills.pop(name, None)

    def list_schedules(self) -> list[ScheduledFunction]:
        out: list[ScheduledFunction] = []
        for skill in self._skills.values():
            for spec in skill.scheduled_runs:
                fn = skill.functions.get(spec.function, _noop)
                out.append(ScheduledFunction(skill=skill.name, spec=spec, fn=fn))
        return out

    def on_change(self, callback: Callable[[], None]) -> None:
        return None


async def _noop(_ctx: Any) -> None:
    return None


def _make_skill(
    name: str,
    schedules: list[ScheduleSpec],
) -> ImperativeSkill:
    fns: dict[str, Callable[..., Awaitable[Any]]] = {}
    for spec in schedules:
        fns.setdefault(spec.function, _noop)
    return ImperativeSkill(
        name=name,
        description="stub",
        scheduled_runs=tuple(schedules),
        functions=fns,
    )


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
        RunCommand = _import_run_command()
        assert RunCommand.name == "run"

    def test_options(self) -> None:
        RunCommand = _import_run_command()
        opts = {o.name: o for o in RunCommand.options}
        assert "skill" in opts
        assert "function" in opts
        # `run_name` is optional with a default behavior of "use the
        # function's only matching run_name."
        assert "run_name" in opts


# --- behavior -------------------------------------------------------------


class TestRunCommandBehavior:
    async def test_invokes_run_now_with_explicit_run_name(self, wired) -> None:
        store, settings, registry = wired
        scheduler = FakeSchedulerService()
        skill_registry = FakeSkillRegistry(
            {
                "demo": _make_skill(
                    "demo",
                    [ScheduleSpec(cron="0 5 * * *", function="tick", name="morning")],
                ),
            }
        )

        RunCommand = _import_run_command()
        cmd = RunCommand(scheduler=scheduler, skill_registry=skill_registry)

        out = await cmd.run(
            _ctx(
                store,
                settings,
                registry,
                args={"skill": "demo", "function": "tick", "run_name": "morning"},
            )
        )

        assert isinstance(out, str)
        assert len(scheduler.run_now_calls) == 1
        call = scheduler.run_now_calls[0]
        assert call.skill == "demo"
        assert call.function == "tick"
        assert call.run_name == "morning"

    async def test_defaults_run_name_when_unique(self, wired) -> None:
        store, settings, registry = wired
        scheduler = FakeSchedulerService()
        skill_registry = FakeSkillRegistry(
            {
                "demo": _make_skill(
                    "demo",
                    [
                        ScheduleSpec(cron="0 5 * * *", function="tick", name="only_run"),
                    ],
                ),
            }
        )

        RunCommand = _import_run_command()
        cmd = RunCommand(scheduler=scheduler, skill_registry=skill_registry)

        await cmd.run(
            _ctx(
                store,
                settings,
                registry,
                args={"skill": "demo", "function": "tick"},
            )
        )

        # Default behavior: when there's exactly one matching schedule
        # for (skill, function), use its `run_name`. (See FC-6 prose +
        # the brief at the top of this file.)
        assert len(scheduler.run_now_calls) == 1
        assert scheduler.run_now_calls[0].run_name == "only_run"

    async def test_ambiguous_run_name_requires_disambiguation(self, wired) -> None:
        store, settings, registry = wired
        scheduler = FakeSchedulerService()
        skill_registry = FakeSkillRegistry(
            {
                "demo": _make_skill(
                    "demo",
                    [
                        ScheduleSpec(cron="0 5 * * *", function="tick", name="morning"),
                        ScheduleSpec(cron="0 17 * * *", function="tick", name="evening"),
                    ],
                ),
            }
        )

        RunCommand = _import_run_command()
        cmd = RunCommand(scheduler=scheduler, skill_registry=skill_registry)

        out = await cmd.run(
            _ctx(
                store,
                settings,
                registry,
                # No run_name → ambiguous. The command should refuse
                # without invoking run_now.
                args={"skill": "demo", "function": "tick"},
            )
        )

        assert isinstance(out, str)
        # User-facing message should hint at disambiguation.
        lowered = out.lower()
        assert (
            "ambiguous" in lowered
            or "multiple" in lowered
            or "disambig" in lowered
            or "specify" in lowered
        )
        # No invocation.
        assert scheduler.run_now_calls == []

    async def test_unknown_schedule_surfaced_as_user_message(self, wired) -> None:
        # When run_now raises UnknownScheduleError, the slash command
        # turns it into a user-facing string (FC-12 / TLC-7).
        store, settings, registry = wired
        scheduler = FakeSchedulerService(
            run_now_raises=UnknownScheduleError("demo:nope not loaded")
        )
        skill_registry = FakeSkillRegistry(
            {
                "demo": _make_skill(
                    "demo",
                    [ScheduleSpec(cron="0 5 * * *", function="tick", name="morning")],
                ),
            }
        )

        RunCommand = _import_run_command()
        cmd = RunCommand(scheduler=scheduler, skill_registry=skill_registry)

        out = await cmd.run(
            _ctx(
                store,
                settings,
                registry,
                args={"skill": "demo", "function": "tick", "run_name": "nope"},
            )
        )
        # Surfaced as a user-facing string, not an exception bubbling out.
        assert isinstance(out, str)
        assert "nope" in out.lower() or "unknown" in out.lower()

    async def test_unknown_skill_returns_user_message_no_invocation(self, wired) -> None:
        store, settings, registry = wired
        scheduler = FakeSchedulerService()
        skill_registry = FakeSkillRegistry({})

        RunCommand = _import_run_command()
        cmd = RunCommand(scheduler=scheduler, skill_registry=skill_registry)

        out = await cmd.run(
            _ctx(
                store,
                settings,
                registry,
                args={"skill": "demo", "function": "tick"},
            )
        )
        assert isinstance(out, str)
        # Should not invoke run_now if the skill is not loaded.
        assert scheduler.run_now_calls == []
