"""Slash-command UX: native choices/autocomplete wiring + command
completers + the self-documenting /help.

The goal of these tests is the *discoverability* contract:
- options with `choices` become Discord dropdowns;
- options with `autocomplete=True` (on a command that implements
  `autocomplete`) become Discord type-aheads;
- each command's completer returns sensible, context-aware suggestions;
- `/help` groups commands and details a single one on request.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from discord import app_commands

from ember.channels.discord import DiscordChannel
from ember.commands.base import CommandContext, SupportsAutocomplete
from ember.commands.help import HelpCommand
from ember.commands.model import ModelCommand
from ember.commands.run import RunCommand
from ember.commands.runs import RunsCommand
from ember.commands.schedule import ScheduleCommand
from ember.commands.skill import SkillCommand
from ember.config import Settings
from ember.scheduler.base import JobSnapshot


# --- stubs ----------------------------------------------------------------


class _Router:
    def __init__(self, names: list[str]) -> None:
        self._names = names

    def names(self) -> list[str]:
        return self._names

    def model_routes(self) -> list[str]:
        # Mirror ProviderRouter.model_routes(); these fakes advertise no
        # curated <provider>:<model> routes, so autocomplete falls back to
        # bare provider names (+ the cheap-tier "(disable)" option).
        return []


class _SkillRegistry:
    def __init__(self, names: list[str]) -> None:
        self._names = names

    def names(self) -> list[str]:
        return self._names


class _Scheduler:
    def __init__(self, jobs: list[JobSnapshot]) -> None:
        self._jobs = jobs

    def list_jobs(self) -> list[JobSnapshot]:
        return self._jobs


def _job(skill: str, run_name: str, function: str) -> JobSnapshot:
    return JobSnapshot(
        skill=skill,
        run_name=run_name,
        function=function,
        cron="0 0 * * *",
        next_fire_time=None,
        enabled=True,
    )


class _Registry:
    def __init__(self, commands) -> None:
        self._commands = commands

    def list_commands(self):
        return self._commands


def _ctx(*, args=None, router=None, registry=None) -> CommandContext:
    return CommandContext(
        conversation_key="t",
        user_id="1",
        store=None,  # type: ignore[arg-type]
        settings=None,  # type: ignore[arg-type]
        registry=registry,  # type: ignore[arg-type]
        args=args or {},
        router=router,
    )


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")
    monkeypatch.setenv("DISCORD_CONVERSATIONS_CHANNEL_ID", "2")
    monkeypatch.setenv("DISCORD_SCHEDULER_CHANNEL_ID", "3")
    return Settings()  # type: ignore[call-arg]


def _params(channel: DiscordChannel, cmd) -> dict:
    """Build the app_commands.Command the adapter would register and return
    {option_name: app_commands.Parameter}."""
    handler = channel._build_options_handler(cmd, cmd.options)
    obj = app_commands.Command(name=cmd.name, description=cmd.description, callback=handler)
    return {p.name: p for p in obj.parameters}


# --- Discord-native wiring ------------------------------------------------


class TestNativeWiring:
    def test_model_action_is_dropdown_value_is_autocomplete(self, settings: Settings) -> None:
        ch = DiscordChannel(settings)
        params = _params(ch, ModelCommand())
        # action → fixed dropdown of the declared choices
        assert [c.value for c in params["action"].choices] == [
            "show",
            "set",
            "failover",
            "cheap",
            "list",
        ]
        assert params["action"].autocomplete is False
        # value → dynamic type-ahead, no static choices
        assert params["value"].autocomplete is True
        assert params["value"].choices == []

    def test_every_autocomplete_command_implements_protocol(self) -> None:
        for cmd in [
            ModelCommand(),
            SkillCommand(skill_registry=_SkillRegistry([]), store=None),  # type: ignore[arg-type]
            ScheduleCommand(scheduler=_Scheduler([])),
            RunsCommand(recorder=None, scheduler=_Scheduler([])),  # type: ignore[arg-type]
            RunCommand(scheduler=_Scheduler([]), skill_registry=_SkillRegistry([])),
            HelpCommand(),
        ]:
            assert isinstance(cmd, SupportsAutocomplete)
            assert any(o.autocomplete for o in cmd.options)


# --- Command completers ---------------------------------------------------


class TestCompleters:
    async def test_model_value_failover_suggests_on_off(self) -> None:
        cmd = ModelCommand()
        out = await cmd.autocomplete(_ctx(args={"action": "failover"}), "value", "")
        assert out == ["on", "off"]
        out = await cmd.autocomplete(_ctx(args={"action": "failover"}), "value", "o")
        assert out == ["on", "off"]

    async def test_model_value_set_suggests_routes(self) -> None:
        cmd = ModelCommand()
        router = _Router(["claude_cli", "deepseek_api", "anthropic_api"])
        out = await cmd.autocomplete(_ctx(args={"action": "set"}, router=router), "value", "deep")
        assert out == ["deepseek_api"]

    async def test_model_value_cheap_includes_disable(self) -> None:
        cmd = ModelCommand()
        router = _Router(["claude_cli"])
        out = await cmd.autocomplete(_ctx(args={"action": "cheap"}, router=router), "value", "")
        assert "(disable)" in out

    async def test_skill_target_completes_names(self) -> None:
        cmd = SkillCommand(
            skill_registry=_SkillRegistry(["daily_brief", "recipe_of_day"]), store=None
        )  # type: ignore[arg-type]
        out = await cmd.autocomplete(_ctx(), "target", "rec")
        assert out == ["recipe_of_day"]

    async def test_schedule_target_completes_skill_run(self) -> None:
        sched = _Scheduler([_job("brief", "evening", "run"), _job("digest", "morning", "go")])
        cmd = ScheduleCommand(scheduler=sched)
        out = await cmd.autocomplete(_ctx(), "target", "brief")
        assert out == ["brief:evening"]

    async def test_runs_skill_completes_from_jobs(self) -> None:
        sched = _Scheduler([_job("brief", "evening", "run")])
        cmd = RunsCommand(recorder=None, scheduler=sched)  # type: ignore[arg-type]
        out = await cmd.autocomplete(_ctx(), "skill", "")
        assert out == ["brief"]

    async def test_run_function_narrows_to_selected_skill(self) -> None:
        sched = _Scheduler([_job("brief", "evening", "send"), _job("digest", "morning", "compile")])
        cmd = RunCommand(scheduler=sched, skill_registry=_SkillRegistry([]))
        # function suggestions are scoped to the already-chosen skill
        out = await cmd.autocomplete(_ctx(args={"skill": "brief"}), "function", "")
        assert out == ["send"]

    async def test_help_command_completes_command_names(self) -> None:
        reg = _Registry([ModelCommand(), HelpCommand()])
        cmd = HelpCommand()
        out = await cmd.autocomplete(_ctx(registry=reg), "command", "mod")
        assert out == ["model"]


# --- /help rendering ------------------------------------------------------


class TestHelpRendering:
    async def test_overview_groups_and_lists_all(self) -> None:
        cmds = [
            ModelCommand(),
            SkillCommand(skill_registry=_SkillRegistry([]), store=None),
            HelpCommand(),
        ]  # type: ignore[arg-type]
        reg = _Registry(cmds)
        out = await HelpCommand().run(_ctx(args={}, registry=reg))
        assert "Model & memory" in out
        assert "Skills & schedules" in out
        # every command appears
        for c in cmds:
            assert f"/{c.name}" in out

    async def test_detail_mode_shows_usage_and_options(self) -> None:
        reg = _Registry([ModelCommand()])
        out = await HelpCommand().run(_ctx(args={"command": "model"}, registry=reg))
        assert "Usage:" in out
        assert "action:<show|set|failover|cheap|list>" in out
        assert "autocompletes" in out  # value option flagged as type-ahead

    async def test_detail_unknown_command(self) -> None:
        reg = _Registry([ModelCommand()])
        out = await HelpCommand().run(_ctx(args={"command": "nope"}, registry=reg))
        assert "no command" in out
