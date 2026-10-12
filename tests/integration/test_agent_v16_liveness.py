"""v16 liveness end-to-end through the Agent (doc #57).

Three integration claims the unit files can't make alone:

A. Message map (UoW-32 step 4 / TLC-28): a turn with streamed blocks
   maps ALL posted native ids — streamed block posts AND the final
   remainder send — to the assistant `messages` row in
   `channel_message_map`, so a reaction on any streamed message
   resolves to feedback (UoW-15/UoW-20 in spirit).
B. Thread-residue invariant (TLC-27 postcondition): after success,
   error, AND interrupt (real TurnGate cancellation), every activity
   status the channel ever posted has been deleted — zero liveness
   residue.
C. The two v16 kill-switches default True in Settings (FC-33, house
   flag pattern — see tests/unit/test_config.py).

Tests-first: failures against current src are EXPECTED until the
implementer lands UoW-31/UoW-32.
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
from ember.channels.gate import TurnGate
from ember.config import Settings, load_settings
from ember.providers.base import CompletionResult, Usage
from ember.store.repo import Store
from tests.fakes.fake_channel import FakeChannel
from tests.fakes.fake_provider import FakeProvider
from tests.util import wait_until

B1 = "STREAMED-BLOCK-ONE marker."
B2 = "STREAMED-BLOCK-TWO marker."
B3 = "UNSTREAMED-BLOCK-THREE marker."

_DEBOUNCE = 0.05


def _make_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")
    monkeypatch.setenv("DISCORD_CONVERSATIONS_CHANNEL_ID", "2")
    monkeypatch.setenv("DISCORD_SCHEDULER_CHANNEL_ID", "3")
    return Settings()  # type: ignore[call-arg]


def _msg(text: str = "hi", *, mid: str = "m1") -> ChannelMessage:
    return ChannelMessage(
        channel="discord",
        conversation_key="thread-1",
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


def _patch_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """FC-32 constants scaled to milliseconds — patched on channels.base
    (where they live) AND ember.agent (`raising=False`) so a
    `from channels.base import NAME` binding is covered either way."""
    for mod in (channels_base, agent_mod):
        monkeypatch.setattr(mod, "ACTIVITY_DELAY_S", 0.05, raising=False)
        monkeypatch.setattr(mod, "ACTIVITY_EDIT_INTERVAL_S", 0.02, raising=False)
        monkeypatch.setattr(mod, "TYPING_REFRESH_S", 0.05, raising=False)


async def _wire(store: Store, settings: Settings, provider: FakeProvider) -> FakeChannel:
    channel = FakeChannel()
    agent = Agent(store=store, provider=provider, channel=channel, settings=settings)
    await channel.start(agent.handle_turn)
    return channel


class _HeldProvider(FakeProvider):
    """Holds `complete` open until released (see unit twin)."""

    def __init__(self) -> None:
        super().__init__()
        self.name = "claude_cli"
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(
        self,
        system: str,
        messages: list,  # type: ignore[type-arg]
        tools: list | None = None,  # type: ignore[type-arg]
        model: str | None = None,
        stream_handler: Any = None,
        session_id: str | None = None,
    ):
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


class _FirstCallBlocks(FakeProvider):
    """First completion blocks until the gate cancels it; later calls
    answer from the queue (pattern: test_turn_interrupt_end_to_end)."""

    def __init__(self) -> None:
        super().__init__()
        self.name = "claude_cli"
        self._first_taken = False

    async def complete(
        self,
        system: str,
        messages: list,  # type: ignore[type-arg]
        tools: list | None = None,  # type: ignore[type-arg]
        model: str | None = None,
        stream_handler: Any = None,
        session_id: str | None = None,
    ):
        if not self._first_taken:
            self._first_taken = True
            await asyncio.sleep(30)  # "thinking" — the gate cancels this
        return await super().complete(
            system,
            messages,
            tools=tools,
            model=model,
            stream_handler=stream_handler,
            session_id=session_id,
        )


def _assert_no_status_residue(channel: FakeChannel) -> None:
    posts = channel.status_posts()
    assert posts, (
        f"liveness machinery never posted an activity message; log: {channel.liveness_ops()!r}"
    )
    deleted = {c.native_id for c in channel.status_deletes()}
    for post in posts:
        assert post.native_id in deleted, (
            f"posted status {post.native_id!r} ({post.text!r}) was never deleted"
        )
    assert channel.live_statuses == {}, (
        f"liveness residue left in thread: {channel.live_statuses!r}"
    )


def _text_event(text: str) -> dict[str, Any]:
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


# ==========================================================================
# A. All posted native ids map to the assistant row
# ==========================================================================


class TestMessageMap:
    async def test_streamed_and_final_native_ids_all_map_to_assistant_row(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened_store: Store
    ) -> None:
        settings = _make_settings(monkeypatch, tmp_path)
        provider = FakeProvider()
        provider.name = "claude_cli"
        channel = await _wire(opened_store, settings, provider)
        parts = (B1, B2, B3)
        provider.queue_response_with_events(
            CompletionResult(
                text="\n\n".join(parts),
                text_parts=parts,
                usage=Usage(input_tokens=1, output_tokens=1, subscription=True),
            ),
            # Only two blocks stream → the final send carries B3, so the
            # map assertion covers BOTH streamed and final native ids.
            [_text_event(B1), _text_event(B2)],
        )

        await channel.inject(_msg("do the research"))

        conv = await opened_store.get_or_create_conversation("discord", "thread-1")
        msgs = await opened_store.recent_messages(conv.id)
        assistants = [m for m in msgs if m.role == "assistant"]
        assert len(assistants) == 1, "one assistant row per turn regardless of streaming"
        assistant_id = assistants[0].id

        reply_sends = [s for s in channel.sent if any(b in s.content for b in parts)]
        assert len(reply_sends) == 3, (
            f"expected 2 streamed posts + 1 final remainder; got "
            f"{[s.content for s in reply_sends]!r}"
        )
        for s in reply_sends:
            mapped = await opened_store.message_id_for_channel_message("discord", s.message_id)
            assert mapped == assistant_id, (
                f"native id {s.message_id!r} ({s.content[:40]!r}) maps to "
                f"{mapped!r}, expected assistant row {assistant_id} — "
                "reactions on streamed messages must resolve (TLC-28)"
            )


# ==========================================================================
# B. Thread-residue invariant on every terminal path
# ==========================================================================


class TestNoStatusResidue:
    async def test_success_leaves_no_residue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened_store: Store
    ) -> None:
        settings = _make_settings(monkeypatch, tmp_path)
        _patch_fast(monkeypatch)
        provider = _HeldProvider()
        provider.queue_text("the reply")
        channel = await _wire(opened_store, settings, provider)

        task = asyncio.create_task(channel.inject(_msg()))
        await wait_until(lambda: channel.status_posts(), message="activity message post")
        provider.release.set()
        await task

        _assert_no_status_residue(channel)
        finals = [s for s in channel.sent if s.kind == "final"]
        assert len(finals) == 1

    async def test_error_leaves_no_residue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened_store: Store
    ) -> None:
        settings = _make_settings(monkeypatch, tmp_path)
        _patch_fast(monkeypatch)
        provider = _HeldProvider()
        provider.queue_error(RuntimeError("upstream exploded"))
        channel = await _wire(opened_store, settings, provider)

        task = asyncio.create_task(channel.inject(_msg()))
        await wait_until(lambda: channel.status_posts(), message="activity message post")
        provider.release.set()
        await asyncio.gather(task, return_exceptions=True)

        _assert_no_status_residue(channel)

    async def test_interrupt_leaves_no_residue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened_store: Store
    ) -> None:
        """A real TurnGate interrupt: the first turn's activity message
        is deleted by its finally path even though the turn was
        cancelled mid-provider-call."""
        settings = _make_settings(monkeypatch, tmp_path)
        _patch_fast(monkeypatch)
        provider = _FirstCallBlocks()
        provider.queue_text("second answer")
        channel = FakeChannel()
        agent = Agent(store=opened_store, provider=provider, channel=channel, settings=settings)
        gate = TurnGate(agent.handle_turn, debounce_s=_DEBOUNCE, interrupt=True)

        first = asyncio.create_task(gate.submit(_msg("long question", mid="m1")))
        await wait_until(
            lambda: channel.status_posts(),
            timeout=5.0,
            message="first turn's activity message post",
        )
        second = asyncio.create_task(gate.submit(_msg("never mind", mid="m2")))
        await asyncio.wait_for(asyncio.gather(first, second), timeout=10)

        _assert_no_status_residue(channel)
        finals = [s for s in channel.sent if s.kind == "final"]
        assert any(s.content == "second answer" for s in finals)


# ==========================================================================
# C. Kill-switch defaults
# ==========================================================================

REQUIRED_ENV = {
    "DISCORD_BOT_TOKEN": "secret-token-value",
    "DISCORD_GUILD_ID": "11111",
    "DISCORD_CONVERSATIONS_CHANNEL_ID": "22222",
    "DISCORD_SCHEDULER_CHANNEL_ID": "33333",
}


class TestFlagDefaults:
    def test_v16_flags_default_true(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)  # no .env pickup
        for key in ("TURN_ACTIVITY_ENABLED", "STREAM_TEXT_BLOCKS"):
            monkeypatch.delenv(key, raising=False)
        for k, v in REQUIRED_ENV.items():
            monkeypatch.setenv(k, v)
        s = load_settings()
        assert s.turn_activity_enabled is True
        assert s.stream_text_blocks is True
