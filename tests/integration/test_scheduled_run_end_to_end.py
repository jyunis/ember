"""End-to-end UoW-5 wrapped path via `SchedulerService.run_now` (FC-6).

`run_now` is the same wrapped path used by cron fires (FC-6: "manual
trigger same code path"). After it returns, we expect:
- A row in `scheduled_runs` with `status=ok` for a successful fn.
- A row with `status=error` and exactly one notifier message for a
  raising fn.
- `FakeChannel.scheduler_notifications` is empty for the ok case.

Spec ambiguity flagged in report:
- The exact `SchedulerService` constructor signature is not in the
  contracts doc beyond the Protocol surface in `scheduler/base.py`.
  The tester does not read the implementation, so the test wires the
  service via a single helper that the implementer may need to align.
  We import the concrete service from `ember.scheduler.service`
  (the natural module name given `runner`/`recorder`/`notifier` are
  separate). The signature we assume is:

      SchedulerService(
          *,
          store: Store,
          skill_source: SkillSource,
          channel: Channel,
          settings: Settings,
      )

  If the implementer wires it differently, this test surfaces the
  divergence and the spec doc should grow a §SchedulerService.__init__
  section.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from ember.config import Settings
from ember.scheduler.base import ScheduleSpec
from ember.skills.base import ImperativeSkill
from ember.store.repo import Store
from tests.fakes.fake_channel import FakeChannel


def _import_service():
    from ember.scheduler.service import SchedulerService

    return SchedulerService


def _import_registry():
    from ember.skills.registry import InMemorySkillRegistry

    return InMemorySkillRegistry


# --- helpers --------------------------------------------------------------


def _make_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")
    monkeypatch.setenv("DISCORD_CONVERSATIONS_CHANNEL_ID", "2")
    monkeypatch.setenv("DISCORD_SCHEDULER_CHANNEL_ID", "3")
    return Settings()  # type: ignore[call-arg]


def _make_skill(
    name: str,
    fn: Callable[..., Awaitable[Any]],
    *,
    schedule_name: str = "tick",
    cron: str = "0 5 * * *",
) -> ImperativeSkill:
    return ImperativeSkill(
        name=name,
        description="stub",
        scheduled_runs=(ScheduleSpec(cron=cron, function="tick", name=schedule_name),),
        functions={"tick": fn},
    )


@pytest.fixture
async def wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    settings = _make_settings(monkeypatch, tmp_path)
    store = Store(tmp_path / "conversations.db")
    await store.open()
    channel = FakeChannel()
    InMemorySkillRegistry = _import_registry()
    registry = InMemorySkillRegistry()
    SchedulerService = _import_service()
    service = SchedulerService(
        store=store,
        skill_source=registry,
        channel=channel,
        settings=settings,
    )
    try:
        await service.start()
    except Exception:
        await store.close()
        raise
    try:
        yield service, store, channel, registry
    finally:
        try:
            await service.stop()
        finally:
            await store.close()


# --- tests ----------------------------------------------------------------


class TestRunNowEndToEnd:
    async def test_run_now_ok_writes_row_no_notification(self, wired) -> None:
        service, store, channel, registry = wired

        async def ok_fn(ctx: Any) -> dict[str, str]:
            return {"hello": "world"}

        registry.replace(_make_skill("demo", ok_fn))

        # The scheduler may need a refresh after the registry change to
        # see the new schedule. Run_now should use the registry's
        # current state regardless.
        await service.refresh()
        await service.run_now("demo", "tick", "tick")

        runs = await store.list_runs("demo", limit=10)
        assert len(runs) >= 1
        # Most-recent-first ordering: the ok run is first.
        assert runs[0].status == "ok"
        assert runs[0].skill == "demo"
        assert runs[0].run_name == "tick"

        # No notification on success.
        assert channel.scheduler_notifications == []

    async def test_run_now_error_writes_row_and_notifies_once(self, wired) -> None:
        service, store, channel, registry = wired

        async def boom_fn(ctx: Any) -> None:
            raise RuntimeError("kaboom-sentinel")

        registry.replace(_make_skill("brokenskill", boom_fn))
        await service.refresh()
        await service.run_now("brokenskill", "tick", "tick")

        runs = await store.list_runs("brokenskill", limit=10)
        assert len(runs) >= 1
        assert runs[0].status == "error"
        assert runs[0].error_text is not None
        assert "kaboom-sentinel" in runs[0].error_text

        # Exactly one notification.
        assert len(channel.scheduler_notifications) == 1
        msg = channel.scheduler_notifications[0]
        assert "brokenskill" in msg
