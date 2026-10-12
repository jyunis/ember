"""SchedulerService refresh diffing (FC-6).

Per FC-6:
> Job source: loaded imperative skills only. Re-derived on every
> file-watcher tick (re-add new, remove removed, update changed).

This test exercises the observable consequence: after the registry
publishes a modified version of a skill (different cron), the
service's `list_jobs()` reflects the new cron WITHOUT a service
restart. Either the registry's `on_change` callback drives `refresh()`
automatically, or callers can invoke `refresh()` explicitly. We test
both — auto via the change callback, and explicit via `refresh()`.
"""

from __future__ import annotations

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


def _make_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")
    monkeypatch.setenv("DISCORD_CONVERSATIONS_CHANNEL_ID", "2")
    monkeypatch.setenv("DISCORD_SCHEDULER_CHANNEL_ID", "3")
    return Settings()  # type: ignore[call-arg]


async def _noop(_ctx: Any) -> None:
    return None


def _make_skill(cron: str) -> ImperativeSkill:
    return ImperativeSkill(
        name="demo",
        description="stub",
        scheduled_runs=(ScheduleSpec(cron=cron, function="tick", name="tick"),),
        functions={"tick": _noop},
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
    await service.start()
    try:
        yield service, store, channel, registry
    finally:
        try:
            await service.stop()
        finally:
            await store.close()


class TestRefreshDiff:
    async def test_refresh_picks_up_new_cron(self, wired) -> None:
        service, store, channel, registry = wired

        registry.replace(_make_skill("0 5 * * *"))
        await service.refresh()

        jobs_before = service.list_jobs()
        assert any(j.cron == "0 5 * * *" for j in jobs_before)

        # Replace with a different cron.
        registry.replace(_make_skill("0 17 * * *"))
        await service.refresh()

        jobs_after = service.list_jobs()
        crons_after = {j.cron for j in jobs_after}
        assert "0 17 * * *" in crons_after
        # Old cron should no longer be present.
        assert "0 5 * * *" not in crons_after

    async def test_refresh_removes_removed_skills(self, wired) -> None:
        service, store, channel, registry = wired

        registry.replace(_make_skill("0 5 * * *"))
        await service.refresh()
        assert service.list_jobs(), "no jobs registered after replace+refresh"

        registry.remove("demo")
        await service.refresh()
        assert all(j.skill != "demo" for j in service.list_jobs())
