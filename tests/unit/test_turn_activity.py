"""v16 turn liveness — TLC-27 / UoW-31 (doc #57).

Every Discord turn shows a native typing indicator from turn start until
the final send, refreshed on an interval. Turns exceeding
`ACTIVITY_DELAY_S` additionally show exactly ONE activity status message
(`⏳ working — …`), edited in place with elapsed time / tool-call count /
last tool (tool detail only when `show_tool_calls`), and ALWAYS deleted
on every terminal path — success, error, cancellation. All liveness I/O
is best-effort: channel failures inside the machinery never affect the
turn, and a failed delete is retried exactly once.

Seam: the machinery runs inside `Agent.handle_turn`, its timing driven
by the FC-32 module constants on `ember.channels.base`
(`ACTIVITY_DELAY_S`, `ACTIVITY_EDIT_INTERVAL_S`, `TYPING_REFRESH_S`).
Tests patch those constants to milliseconds — on both channels.base and
ember.agent (`raising=False`), so a `from ... import NAME` binding in
the agent module is covered too — and drive real turns through a
provider that holds `complete` open until released, exposing the turn's
stream handler so observer events can be played mid-turn.

Tests-first: failures against current src (no constants, no liveness
calls recorded) are EXPECTED until the implementer lands UoW-31.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import ember.agent as agent_mod
import ember.channels.base as channels_base
from ember.agent import Agent
from ember.channels.base import ChannelMessage
from ember.config import Settings
from ember.store.repo import Store
from tests.fakes.fake_channel import FakeChannel
from tests.fakes.fake_provider import FakeProvider
from tests.util import wait_until

# --- Wiring ---------------------------------------------------------------


def _make_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")
    monkeypatch.setenv("DISCORD_CONVERSATIONS_CHANNEL_ID", "2")
    monkeypatch.setenv("DISCORD_SCHEDULER_CHANNEL_ID", "3")
    return Settings()  # type: ignore[call-arg]


def _msg(text: str = "hi", *, key: str = "thread-1", mid: str = "m1") -> ChannelMessage:
    return ChannelMessage(
        channel="discord",
        conversation_key=key,
        user_id="1000",
        text=text,
        message_id=mid,
    )


@pytest.fixture
async def opened_store(tmp_path: Path):
    store = Store(tmp_path / "conversations.db")
    await store.open()
    try:
        yield store
    finally:
        await store.close()


async def _wire(store: Store, settings: Settings, provider: FakeProvider) -> FakeChannel:
    channel = FakeChannel()
    agent = Agent(store=store, provider=provider, channel=channel, settings=settings)
    await channel.start(agent.handle_turn)
    return channel


def _patch_fast(
    monkeypatch: pytest.MonkeyPatch,
    *,
    delay: float = 0.05,
    edit: float = 0.02,
    typing: float = 0.05,
) -> None:
    """Scale the FC-32 timing constants to milliseconds. Patched on
    channels.base (where they live) AND on ember.agent (`raising=False`)
    so a `from channels.base import NAME` binding is covered either way."""
    for mod in (channels_base, agent_mod):
        monkeypatch.setattr(mod, "ACTIVITY_DELAY_S", delay, raising=False)
        monkeypatch.setattr(mod, "ACTIVITY_EDIT_INTERVAL_S", edit, raising=False)
        monkeypatch.setattr(mod, "TYPING_REFRESH_S", typing, raising=False)


class _HeldProvider(FakeProvider):
    """FakeProvider that holds every `complete` call open until released,
    exposing the turn's stream handler so tests can play observer events
    mid-turn (UoW-31 step 3: edits on observer events)."""

    def __init__(self) -> None:
        super().__init__()
        self.name = "claude_cli"
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.handler: Any = None

    async def complete(
        self,
        system: str,
        messages: list,  # type: ignore[type-arg]
        tools: list | None = None,  # type: ignore[type-arg]
        model: str | None = None,
        stream_handler: Any = None,
        session_id: str | None = None,
    ):
        self.handler = stream_handler
        self.entered.set()
        await self.release.wait()
        return await super().complete(
            system,
            messages,
            tools=tools,
            model=model,
            stream_handler=stream_handler,
            session_id=session_id,
        )


def _tool_use_event(name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "id": "tu_1", "name": name, "input": args}]},
    }


async def _finish(task: asyncio.Task) -> None:  # type: ignore[type-arg]
    """Await a turn task tolerating error/cancel terminal paths."""
    await asyncio.gather(task, return_exceptions=True)


# ==========================================================================
# FC-32 timing constants
# ==========================================================================


class TestConstants:
    def test_fc32_timing_constants_pinned(self) -> None:
        """Doc #57 FC-32: module constants on the channel layer, not config."""
        assert getattr(channels_base, "ACTIVITY_DELAY_S", None) == 20
        assert getattr(channels_base, "ACTIVITY_EDIT_INTERVAL_S", None) == 5
        assert getattr(channels_base, "TYPING_REFRESH_S", None) == 8


# ==========================================================================
# Typing keepalive
# ==========================================================================


class TestTyping:
    async def test_short_turn_shows_typing_but_no_activity_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened_store: Store
    ) -> None:
        """A turn well under the (unpatched) 20s threshold shows typing
        and never posts an activity message (TLC-27)."""
        settings = _make_settings(monkeypatch, tmp_path)
        provider = FakeProvider()
        provider.name = "claude_cli"
        provider.queue_text("quick answer")
        channel = await _wire(opened_store, settings, provider)

        await channel.inject(_msg("hi"))

        assert "typing_started" in channel.liveness_ops(), (
            f"typing must start at turn start; liveness log: {channel.liveness_ops()!r}"
        )
        assert channel.status_posts() == [], (
            "a turn under ACTIVITY_DELAY_S must not post an activity message"
        )
        finals = [s for s in channel.sent if s.kind == "final"]
        assert len(finals) == 1

    async def test_typing_refreshes_while_turn_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened_store: Store
    ) -> None:
        settings = _make_settings(monkeypatch, tmp_path)
        # Fast typing refresh; activity threshold pushed out of reach so
        # only the keepalive is under test.
        _patch_fast(monkeypatch, delay=30.0, edit=30.0, typing=0.01)
        provider = _HeldProvider()
        provider.queue_text("done")
        channel = await _wire(opened_store, settings, provider)

        task = asyncio.create_task(channel.inject(_msg("hi")))
        try:
            await wait_until(
                lambda: channel.liveness_ops().count("typing_started") >= 3,
                message="at least 3 typing starts/refreshes",
            )
        finally:
            provider.release.set()
            await _finish(task)

        assert channel.status_posts() == [], "activity threshold not reached; no post expected"


# ==========================================================================
# Activity message lifecycle
# ==========================================================================


class TestActivityMessage:
    async def test_posted_after_threshold_edited_then_deleted_on_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened_store: Store
    ) -> None:
        settings = _make_settings(monkeypatch, tmp_path)
        _patch_fast(monkeypatch)
        provider = _HeldProvider()
        provider.queue_text("the reply")
        channel = await _wire(opened_store, settings, provider)

        task = asyncio.create_task(channel.inject(_msg("hi")))
        try:
            await wait_until(lambda: channel.status_posts(), message="activity message post")
            posts = channel.status_posts()
            assert len(posts) == 1, "exactly one activity message per turn"
            assert "⏳" in (posts[0].text or ""), f"UoW-31 format: got {posts[0].text!r}"
            assert "working" in (posts[0].text or "")
            native_id = posts[0].native_id
            await wait_until(
                lambda: any(e.native_id == native_id for e in channel.status_edits()),
                message="an in-place edit of the activity message",
            )
        finally:
            provider.release.set()
            await _finish(task)

        # Deleted on success (finally path); no residue.
        deletes = [e for e in channel.status_deletes() if e.native_id == native_id]
        assert deletes, f"activity message never deleted; log: {channel.liveness_ops()!r}"
        assert channel.live_statuses == {}, "TLC-27: zero liveness residue after the turn"
        # Ordering: post < first edit < delete in the event log.
        ops = [
            (c.op, c.native_id)
            for c in channel.liveness_calls
            if c.op.endswith("_status") and c.native_id == native_id
        ]
        assert ops[0][0] == "post_status"
        assert ops[-1][0] == "delete_status"
        # Still exactly one activity message over the whole turn.
        assert len(channel.status_posts()) == 1
        finals = [s for s in channel.sent if s.kind == "final"]
        assert len(finals) == 1 and finals[0].content == "the reply"

    async def test_edit_carries_tool_count_and_last_tool_when_show_tool_calls(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened_store: Store
    ) -> None:
        monkeypatch.setenv("SHOW_TOOL_CALLS", "true")
        settings = _make_settings(monkeypatch, tmp_path)
        assert settings.show_tool_calls is True
        _patch_fast(monkeypatch)
        provider = _HeldProvider()
        provider.queue_text("ok")
        channel = await _wire(opened_store, settings, provider)

        task = asyncio.create_task(channel.inject(_msg("hi")))
        try:
            await wait_until(lambda: channel.status_posts(), message="activity message post")
            assert provider.handler is not None
            await provider.handler(
                _tool_use_event("agent__web_fetch", {"url": "https://example.com/page"})
            )
            await wait_until(
                lambda: any("web_fetch" in (e.text or "") for e in channel.status_edits()),
                message="an edit naming the last tool",
            )
        finally:
            provider.release.set()
            await _finish(task)

        detail_edits = [e for e in channel.status_edits() if "web_fetch" in (e.text or "")]
        # Tool-call count rides the same activity line (UoW-31 step 3).
        assert any("1" in (e.text or "") and "tool" in (e.text or "") for e in detail_edits), (
            f"expected elapsed · tool count · last tool; edits: "
            f"{[e.text for e in channel.status_edits()]!r}"
        )

    async def test_tool_args_are_truncated_in_activity_edit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened_store: Store
    ) -> None:
        monkeypatch.setenv("SHOW_TOOL_CALLS", "true")
        settings = _make_settings(monkeypatch, tmp_path)
        _patch_fast(monkeypatch)
        provider = _HeldProvider()
        provider.queue_text("ok")
        channel = await _wire(opened_store, settings, provider)

        long_arg = "x" * 5000
        task = asyncio.create_task(channel.inject(_msg("hi")))
        try:
            await wait_until(lambda: channel.status_posts(), message="activity message post")
            assert provider.handler is not None
            await provider.handler(_tool_use_event("agent__web_fetch", {"text": long_arg}))
            await wait_until(
                lambda: any("web_fetch" in (e.text or "") for e in channel.status_edits()),
                message="an edit naming the last tool",
            )
        finally:
            provider.release.set()
            await _finish(task)

        for edit in channel.status_edits():
            text = edit.text or ""
            assert long_arg not in text, "activity edit must truncate tool args"
            assert len(text) < 500, f"activity edit unreasonably long: {len(text)} chars"

    async def test_edit_omits_tool_detail_when_show_tool_calls_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened_store: Store
    ) -> None:
        """TLC-27: `show_tool_calls` now governs tool detail in the
        activity message (the old separate per-tool posts are gone)."""
        monkeypatch.setenv("SHOW_TOOL_CALLS", "false")
        settings = _make_settings(monkeypatch, tmp_path)
        assert settings.show_tool_calls is False
        _patch_fast(monkeypatch)
        provider = _HeldProvider()
        provider.queue_text("ok")
        channel = await _wire(opened_store, settings, provider)

        task = asyncio.create_task(channel.inject(_msg("hi")))
        try:
            await wait_until(lambda: channel.status_posts(), message="activity message post")
            edits_before = len(channel.status_edits())
            assert provider.handler is not None
            await provider.handler(
                _tool_use_event("agent__web_fetch", {"url": "https://example.com"})
            )
            # Wait for a couple of post-event edits so absence of tool
            # detail is meaningful, not just "no edit happened yet".
            await wait_until(
                lambda: len(channel.status_edits()) >= edits_before + 2,
                message="edits after the tool event",
            )
        finally:
            provider.release.set()
            await _finish(task)

        for edit in channel.status_edits():
            assert "web_fetch" not in (edit.text or ""), (
                f"tool detail must be omitted when show_tool_calls is off: {edit.text!r}"
            )


# ==========================================================================
# Terminal cleanup — error / cancellation (finally path)
# ==========================================================================


class TestTerminalCleanup:
    async def test_activity_deleted_when_provider_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened_store: Store
    ) -> None:
        settings = _make_settings(monkeypatch, tmp_path)
        _patch_fast(monkeypatch)
        provider = _HeldProvider()
        provider.queue_error(RuntimeError("provider exploded"))
        channel = await _wire(opened_store, settings, provider)

        task = asyncio.create_task(channel.inject(_msg("hi")))
        await wait_until(lambda: channel.status_posts(), message="activity message post")
        provider.release.set()
        await _finish(task)

        assert channel.status_deletes(), (
            f"activity message must be deleted on error; log: {channel.liveness_ops()!r}"
        )
        assert channel.live_statuses == {}

    async def test_activity_deleted_on_cancellation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened_store: Store
    ) -> None:
        settings = _make_settings(monkeypatch, tmp_path)
        _patch_fast(monkeypatch)
        provider = _HeldProvider()  # never released — cancelled mid-turn
        channel = await _wire(opened_store, settings, provider)

        task = asyncio.create_task(channel.inject(_msg("hi")))
        await wait_until(lambda: channel.status_posts(), message="activity message post")
        task.cancel()
        await _finish(task)

        assert channel.status_deletes(), (
            "activity message must be deleted on cancellation (finally-guarded); "
            f"log: {channel.liveness_ops()!r}"
        )
        assert channel.live_statuses == {}


# ==========================================================================
# Best-effort I/O — failures swallowed, delete retried once
# ==========================================================================


class TestBestEffort:
    async def test_liveness_channel_failures_never_break_the_turn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened_store: Store
    ) -> None:
        settings = _make_settings(monkeypatch, tmp_path)
        _patch_fast(monkeypatch)
        provider = _HeldProvider()
        provider.queue_text("still fine")
        channel = await _wire(opened_store, settings, provider)
        channel.fail_next("typing_started", times=1000)
        channel.fail_next("post_status", times=1000)
        channel.fail_next("edit_status", times=1000)

        task = asyncio.create_task(channel.inject(_msg("hi")))
        await wait_until(
            lambda: any(not c.ok for c in channel.liveness_calls),
            message="a failed (injected) liveness call",
        )
        provider.release.set()
        # Must not raise: liveness failures are swallowed by the machinery.
        await task

        finals = [s for s in channel.sent if s.kind == "final"]
        assert len(finals) == 1 and finals[0].content == "still fine"

    async def test_failed_delete_is_retried_once_then_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened_store: Store
    ) -> None:
        settings = _make_settings(monkeypatch, tmp_path)
        _patch_fast(monkeypatch)
        provider = _HeldProvider()
        provider.queue_text("ok")
        channel = await _wire(opened_store, settings, provider)
        channel.fail_next("delete_status", times=1)

        task = asyncio.create_task(channel.inject(_msg("hi")))
        await wait_until(lambda: channel.status_posts(), message="activity message post")
        provider.release.set()
        await task

        attempts = [c for c in channel.liveness_calls if c.op == "delete_status"]
        assert len(attempts) == 2, (
            f"failed delete must be retried exactly once; attempts: {attempts!r}"
        )
        assert attempts[0].ok is False and attempts[1].ok is True
        assert channel.live_statuses == {}

    async def test_delete_gives_up_after_one_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened_store: Store
    ) -> None:
        settings = _make_settings(monkeypatch, tmp_path)
        _patch_fast(monkeypatch)
        provider = _HeldProvider()
        provider.queue_text("ok")
        channel = await _wire(opened_store, settings, provider)
        channel.fail_next("delete_status", times=2)

        task = asyncio.create_task(channel.inject(_msg("hi")))
        await wait_until(lambda: channel.status_posts(), message="activity message post")
        provider.release.set()
        await task  # must not raise despite both deletes failing

        attempts = [c for c in channel.liveness_calls if c.op == "delete_status"]
        assert len(attempts) == 2, f"retry once then log — no third attempt; attempts: {attempts!r}"
        finals = [s for s in channel.sent if s.kind == "final"]
        assert len(finals) == 1


# ==========================================================================
# Kill-switch
# ==========================================================================


class TestKillSwitch:
    async def test_turn_activity_disabled_means_zero_liveness_calls(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened_store: Store
    ) -> None:
        """`turn_activity_enabled=False` turns off the whole tier-1
        machinery — no typing, no activity message (doc #57 TLC-27)."""
        monkeypatch.setenv("TURN_ACTIVITY_ENABLED", "false")
        settings = _make_settings(monkeypatch, tmp_path)
        assert settings.turn_activity_enabled is False
        _patch_fast(monkeypatch)
        provider = _HeldProvider()
        provider.queue_text("ok")
        channel = await _wire(opened_store, settings, provider)

        task = asyncio.create_task(channel.inject(_msg("hi")))
        await wait_until(lambda: provider.entered.is_set(), message="provider entered")
        # Hold well past the (patched) activity threshold to give the
        # machinery every chance to (wrongly) fire.
        await asyncio.sleep(0.2)
        provider.release.set()
        await task

        assert channel.liveness_calls == [], (
            f"flag off must mean zero liveness machinery; log: {channel.liveness_ops()!r}"
        )
        finals = [s for s in channel.sent if s.kind == "final"]
        assert len(finals) == 1
