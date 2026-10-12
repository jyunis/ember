"""Multi-thread conversations — v1 acceptance.

The architecture doc lists multi-thread conversations as a v1
deliverable, but notes they already work in v0 (one row per
forum-thread ID via UoW-14). This test confirms the property still
holds with the v1 Agent surface (now with optional identity).

Distinct `conversation_key` values produce distinct conversation rows,
each with independent message history. This is the property that makes
Discord forum threads map cleanly onto Ember conversations.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ember.agent import Agent
from ember.channels.base import ChannelMessage
from ember.config import Settings
from ember.store.repo import Store
from tests.fakes.fake_channel import FakeChannel
from tests.fakes.fake_provider import FakeProvider
from tests.util import unstamp


# --- Fixtures -------------------------------------------------------------


def _make_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")
    monkeypatch.setenv("DISCORD_CONVERSATIONS_CHANNEL_ID", "2")
    monkeypatch.setenv("DISCORD_SCHEDULER_CHANNEL_ID", "3")
    return Settings()  # type: ignore[call-arg]


def _msg(text: str, *, key: str, mid: str) -> ChannelMessage:
    return ChannelMessage(
        channel="discord",
        conversation_key=key,
        user_id="1000",
        text=text,
        message_id=mid,
    )


@pytest.fixture
async def wiring(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    settings = _make_settings(monkeypatch, tmp_path)
    store = Store(tmp_path / "conversations.db")
    await store.open()

    provider = FakeProvider()
    channel = FakeChannel()
    agent = Agent(
        store=store,
        provider=provider,
        channel=channel,
        settings=settings,
        identity=None,
    )
    await channel.start(agent.handle_turn)

    try:
        yield store, provider, channel, agent
    finally:
        await store.close()


# --- Distinct keys → distinct conversations ------------------------------


class TestMultiThreadConversations:
    async def test_two_distinct_keys_create_two_conversations(self, wiring) -> None:
        store, provider, channel, _ = wiring
        provider.queue_text("reply-A")
        provider.queue_text("reply-B")

        await channel.inject(_msg("hello A", key="thread-A", mid="ma"))
        await channel.inject(_msg("hello B", key="thread-B", mid="mb"))

        ca = await store.get_or_create_conversation("discord", "thread-A")
        cb = await store.get_or_create_conversation("discord", "thread-B")
        # Two distinct conversation rows.
        assert ca.id != cb.id

    async def test_each_conversation_history_is_independent(self, wiring) -> None:
        store, provider, channel, _ = wiring
        # Queue four replies — order is FIFO by completion call, which
        # interleaves across the two threads in the order we inject.
        provider.queue_text("reply-A1")
        provider.queue_text("reply-B1")
        provider.queue_text("reply-A2")
        provider.queue_text("reply-B2")

        await channel.inject(_msg("a-first", key="thread-A", mid="a1"))
        await channel.inject(_msg("b-first", key="thread-B", mid="b1"))
        await channel.inject(_msg("a-second", key="thread-A", mid="a2"))
        await channel.inject(_msg("b-second", key="thread-B", mid="b2"))

        ca = await store.get_or_create_conversation("discord", "thread-A")
        cb = await store.get_or_create_conversation("discord", "thread-B")

        a_msgs = await store.recent_messages(ca.id)
        b_msgs = await store.recent_messages(cb.id)

        a_contents = [unstamp(m.content) for m in a_msgs]
        b_contents = [unstamp(m.content) for m in b_msgs]

        # Thread A holds only thread-A turns.
        assert "a-first" in a_contents
        assert "a-second" in a_contents
        assert "reply-A1" in a_contents
        assert "reply-A2" in a_contents
        # And nothing from thread B leaked in.
        assert "b-first" not in a_contents
        assert "b-second" not in a_contents
        assert "reply-B1" not in a_contents
        assert "reply-B2" not in a_contents

        # Thread B holds only thread-B turns.
        assert "b-first" in b_contents
        assert "b-second" in b_contents
        assert "reply-B1" in b_contents
        assert "reply-B2" in b_contents
        assert "a-first" not in b_contents
        assert "a-second" not in b_contents
        assert "reply-A1" not in b_contents
        assert "reply-A2" not in b_contents
