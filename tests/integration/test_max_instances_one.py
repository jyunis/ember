"""APScheduler `max_instances=1` (FC-6, TLC-10).

A scheduled run that's still in flight must prevent a second
concurrent fire — APScheduler's `max_instances=1` semantics. Either:
- the second `run_now` call is rejected/dropped, OR
- only one in-progress row exists at any moment.

We assert the weaker observable form: while the first call is running,
no second `scheduled_runs` row is in the "started but not finished"
state for the same `(skill, run_name)`. This admits both
implementations (raise on overlap vs silently drop the duplicate fire).
"""

from __future__ import annotations

import asyncio
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


def _make_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")
    monkeypatch.setenv("DISCORD_CONVERSATIONS_CHANNEL_ID", "2")
    monkeypatch.setenv("DISCORD_SCHEDULER_CHANNEL_ID", "3")
    return Settings()  # type: ignore[call-arg]


def _make_skill(name: str, fn: Callable[..., Awaitable[Any]]) -> ImperativeSkill:
    return ImperativeSkill(
        name=name,
        description="stub",
        scheduled_runs=(ScheduleSpec(cron="0 5 * * *", function="tick", name="tick"),),
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
    await service.start()
    try:
        yield service, store, channel, registry
    finally:
        try:
            await service.stop()
        finally:
            await store.close()


async def _count_in_progress(store: Store, skill: str) -> int:
    """Count rows where started_at IS NOT NULL AND finished_at IS NULL
    for `skill` — the "in-progress" state."""
    cur = await store._c.execute(
        "SELECT COUNT(*) FROM scheduled_runs "
        "WHERE skill = ? AND started_at IS NOT NULL AND finished_at IS NULL",
        (skill,),
    )
    row = await cur.fetchone()
    await cur.close()
    return int(row[0]) if row else 0


class TestMaxInstancesOne:
    async def test_two_overlapping_run_now_does_not_double_run(self, wired) -> None:
        service, store, channel, registry = wired
        running_event = asyncio.Event()
        release_event = asyncio.Event()

        async def slow_fn(ctx: Any) -> str:
            running_event.set()
            await release_event.wait()
            return "done"

        registry.replace(_make_skill("slowskill", slow_fn))
        await service.refresh()

        # Kick off first call; wait until it's actually running.
        first = asyncio.create_task(service.run_now("slowskill", "tick", "tick"))
        await asyncio.wait_for(running_event.wait(), timeout=2.0)

        # Now fire the second one. Either it's rejected, or it's
        # silently dropped, or it queues. Either way: there must NOT
        # be two in-progress rows for the same (skill, run_name).
        async def fire_second() -> None:
            try:
                await service.run_now("slowskill", "tick", "tick")
            except Exception:
                # Implementer may choose to raise on overlap; that's
                # also acceptable.
                pass

        second = asyncio.create_task(fire_second())

        # Give the second task a moment to either be admitted or rejected.
        await asyncio.sleep(0.1)

        in_progress = await _count_in_progress(store, "slowskill")
        # Strictly at most one in-progress row.
        assert in_progress <= 1

        # Let the first call finish; clean up.
        release_event.set()
        await asyncio.wait_for(first, timeout=2.0)
        await asyncio.wait_for(second, timeout=2.0)
