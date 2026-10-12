"""`/runs <skill>` slash command (FC-12, UoW-5 surface).

Reads the most recent run history for a given skill from a `RunRecorder`
and renders a user-facing summary. Default limit 20; "no runs yet"
message when the table is empty.

Spec ambiguity flagged in report:
- The architecture doc names the command `/runs <skill>`. Implementer
  may bind it as `runs` with a `skill` option. We use that shape
  here since it matches the existing `/model action:value:` style
  established by `ModelCommand`.
- The `RunsCommand` constructor takes a `RunRecorder` (the recorder is
  the read source per UoW-5 audit boundary). If the implementer chose
  to thread it via `CommandContext` instead, this test signature is
  what flags the divergence.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ember.commands.base import CommandContext, CommandOption
from ember.commands.registry import CommandRegistry
from ember.config import Settings
from ember.scheduler.base import ScheduledRunRow
from ember.store.repo import Store
from tests.fakes.fake_run_recorder import FakeRunRecorder


def _import_runs_command():
    from ember.commands.runs import RunsCommand

    return RunsCommand


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


def _row(
    skill: str,
    function: str,
    run_name: str,
    *,
    rid: int = 1,
    status: str = "ok",
    started: datetime | None = None,
    finished: datetime | None = None,
    result_json: str | None = None,
    error_text: str | None = None,
) -> ScheduledRunRow:
    started = started or datetime(2026, 4, 27, 5, 0, 1, tzinfo=timezone.utc)
    finished = finished or (started + timedelta(seconds=2))
    return ScheduledRunRow(
        id=rid,
        skill=skill,
        function=function,
        run_name=run_name,
        scheduled_for=None,
        started_at=started,
        finished_at=finished,
        status=status,  # type: ignore[arg-type]
        result_json=result_json,
        error_text=error_text,
    )


# --- static surface -------------------------------------------------------


class TestStaticSurface:
    def test_name(self) -> None:
        RunsCommand = _import_runs_command()
        assert RunsCommand.name == "runs"

    def test_options_include_skill(self) -> None:
        RunsCommand = _import_runs_command()
        opts = {o.name: o for o in RunsCommand.options}
        assert "skill" in opts
        assert isinstance(opts["skill"], CommandOption)


# --- behavior -------------------------------------------------------------


class TestRunsCommandBehavior:
    async def test_renders_recent_rows(self, wired) -> None:
        store, settings, registry = wired
        recorder = FakeRunRecorder()
        # Pre-populate `runs` so list_runs returns them.
        recorder.runs = [
            _row("demo", "tick", "tick", rid=3, status="ok"),
            _row(
                "demo",
                "tick",
                "tick",
                rid=2,
                status="error",
                error_text="boom",
            ),
            _row("demo", "tick", "tick", rid=1, status="timeout"),
        ]
        RunsCommand = _import_runs_command()
        cmd = RunsCommand(recorder=recorder)

        out = await cmd.run(_ctx(store, settings, registry, args={"skill": "demo"}))
        assert isinstance(out, str)
        # Each status appears in the rendered output.
        lowered = out.lower()
        assert "ok" in lowered
        assert "error" in lowered
        assert "timeout" in lowered
        # Skill name appears at least once.
        assert "demo" in lowered

    async def test_default_limit_is_20(self, wired) -> None:
        store, settings, registry = wired
        # Recorder has 25 rows; default limit must clip to 20.
        recorder = FakeRunRecorder()
        recorder.runs = [_row("demo", "tick", "tick", rid=i, status="ok") for i in range(25, 0, -1)]
        RunsCommand = _import_runs_command()
        cmd = RunsCommand(recorder=recorder)

        # Spy on what limit was passed.
        observed: dict[str, int] = {}

        original_list = recorder.list_runs

        async def spy(skill: str, *, limit: int = 20) -> list[ScheduledRunRow]:
            observed["limit"] = limit
            return await original_list(skill, limit=limit)

        recorder.list_runs = spy  # type: ignore[assignment]

        await cmd.run(_ctx(store, settings, registry, args={"skill": "demo"}))
        assert observed.get("limit") == 20

    async def test_no_rows_returns_friendly_message(self, wired) -> None:
        store, settings, registry = wired
        recorder = FakeRunRecorder()
        # `runs` empty; recorder returns [].
        RunsCommand = _import_runs_command()
        cmd = RunsCommand(recorder=recorder)

        out = await cmd.run(_ctx(store, settings, registry, args={"skill": "demo"}))
        assert isinstance(out, str)
        # Some indication of "nothing yet" — not pinned to exact wording.
        lowered = out.lower()
        assert "no runs" in lowered or "no run" in lowered or "none" in lowered

    async def test_missing_skill_arg_returns_usage(self, wired) -> None:
        store, settings, registry = wired
        recorder = FakeRunRecorder()
        RunsCommand = _import_runs_command()
        cmd = RunsCommand(recorder=recorder)
        out = await cmd.run(_ctx(store, settings, registry, args={}))
        assert isinstance(out, str)
        # Some kind of usage message — exact wording not pinned.
        assert out != ""
