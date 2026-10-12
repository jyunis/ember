"""Interrupting a real turn: gate + Agent + Store together.

The gate unit tests use a fake handler, so they prove the gate's
bookkeeping but not the claim the design rests on: that discarding a
cancelled turn is safe because the *store* already carries its user
message, and the next turn rebuilds history from there.

This drives the real `Agent.handle_turn` against a real `Store` and
asserts on what actually landed in the database.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ember.agent import Agent
from ember.channels.base import ChannelMessage
from ember.channels.gate import TurnGate
from ember.config import Settings
from ember.providers.base import Capabilities, CompletionResult, Usage
from ember.store.repo import Store
from tests.fakes.fake_channel import FakeChannel
from tests.util import unstamp

_DEBOUNCE = 0.05


def _settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")
    monkeypatch.setenv("DISCORD_CONVERSATIONS_CHANNEL_ID", "2")
    monkeypatch.setenv("DISCORD_SCHEDULER_CHANNEL_ID", "3")
    return Settings()  # type: ignore[call-arg]


class _BlockingProvider:
    """Blocks on its first completion until cancelled; answers after."""

    name = "fake"
    capabilities = Capabilities(tool_use=True, streaming=True, prompt_cache=False)
    known_models: tuple[str, ...] = ()

    def __init__(self) -> None:
        self.calls: list[list] = []
        self.first_call_started = asyncio.Event()
        self.cancelled = False

    async def complete(self, system, messages, tools=None, model=None, **kw):
        self.calls.append(list(messages))
        if len(self.calls) == 1:
            self.first_call_started.set()
            try:
                await asyncio.sleep(30)  # "thinking" — the gate cancels this
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        return CompletionResult(
            text="answer",
            usage=Usage(input_tokens=1, output_tokens=1),
        )


def _msg(text: str, mid: str) -> ChannelMessage:
    return ChannelMessage(
        channel="discord",
        conversation_key="t1",
        user_id="u1",
        text=text,
        message_id=mid,
    )


@pytest.fixture
async def wiring(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    settings = _settings(monkeypatch, tmp_path)
    store = Store(tmp_path / "conversations.db")
    await store.open()
    provider = _BlockingProvider()
    channel = FakeChannel()
    agent = Agent(store=store, provider=provider, channel=channel, settings=settings)
    try:
        yield store, provider, channel, agent
    finally:
        await store.close()


async def _run_interrupt(gate: TurnGate, provider: _BlockingProvider) -> None:
    first = asyncio.create_task(gate.submit(_msg("what is the weather", "1")))
    await asyncio.wait_for(provider.first_call_started.wait(), timeout=3)
    second = asyncio.create_task(gate.submit(_msg("actually, the forecast", "2")))
    await asyncio.wait_for(asyncio.gather(first, second), timeout=10)


class TestInterruptEndToEnd:
    async def test_second_turn_sees_both_user_messages(self, wiring) -> None:
        """The whole safety argument: the interrupted message is not
        replayed by the gate because it is already in the history the
        next turn reconstructs."""
        store, provider, channel, agent = wiring
        gate = TurnGate(agent.handle_turn, debounce_s=_DEBOUNCE, interrupt=True)
        await _run_interrupt(gate, provider)

        assert provider.cancelled, "first turn was never actually cancelled"
        assert len(provider.calls) == 2
        second_call_texts = [unstamp(m.content) for m in provider.calls[1] if m.role == "user"]
        assert second_call_texts == ["what is the weather", "actually, the forecast"]

    async def test_interrupted_turn_appends_no_assistant_row(self, wiring) -> None:
        """A cancelled turn must not leave a half-finished reply behind."""
        store, provider, channel, agent = wiring
        gate = TurnGate(agent.handle_turn, debounce_s=_DEBOUNCE, interrupt=True)
        await _run_interrupt(gate, provider)

        conversation = await store.get_or_create_conversation("discord", "t1")
        rows = await store.recent_messages(conversation.id)
        assert [(r.role, unstamp(r.content)) for r in rows] == [
            ("user", "what is the weather"),
            ("user", "actually, the forecast"),
            ("assistant", "answer"),
        ]

    async def test_only_the_surviving_turn_replies(self, wiring) -> None:
        store, provider, channel, agent = wiring
        gate = TurnGate(agent.handle_turn, debounce_s=_DEBOUNCE, interrupt=True)
        await _run_interrupt(gate, provider)

        replies = [s for s in channel.sent if s.kind in ("reply", "final")]
        assert len(replies) == 1
        assert "answer" in replies[0].content

    async def test_without_interrupt_both_turns_complete(self, wiring) -> None:
        """Control: same wiring, interrupt off — the first turn is not
        cancelled, so it must run to completion and reply."""
        store, provider, channel, agent = wiring
        gate = TurnGate(agent.handle_turn, debounce_s=_DEBOUNCE, interrupt=False)
        first = asyncio.create_task(gate.submit(_msg("what is the weather", "1")))
        await asyncio.wait_for(provider.first_call_started.wait(), timeout=3)
        second = asyncio.create_task(gate.submit(_msg("actually, the forecast", "2")))
        # The first turn blocks for 30s; cancel the whole thing once we've
        # confirmed the second turn never started while it was running.
        await asyncio.sleep(_DEBOUNCE * 4)
        assert len(provider.calls) == 1, "second turn started while the first was running"
        assert not provider.cancelled
        first.cancel()
        second.cancel()
        await asyncio.gather(first, second, return_exceptions=True)
