"""Siri voice-note injection (doc #55 step 3).

Conversations on the siri channel get the `_SIRI_VOICE_NOTE` block in
the system prompt — the reply is spoken aloud, so markdown must be
suppressed at the source. Discord conversations must not get it.

Mechanical surface (single-author): asserts only the presence/absence
of the note in the `system` arg seen by the provider.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ember.agent import _SIRI_VOICE_NOTE, Agent
from ember.channels.base import ChannelMessage
from ember.config import Settings
from ember.store.repo import Store
from tests.fakes.fake_channel import FakeChannel
from tests.fakes.fake_provider import FakeProvider


def _make_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")
    monkeypatch.setenv("DISCORD_CONVERSATIONS_CHANNEL_ID", "2")
    monkeypatch.setenv("DISCORD_SCHEDULER_CHANNEL_ID", "3")
    return Settings()  # type: ignore[call-arg]


@pytest.fixture
async def opened_store(tmp_path: Path):
    store = Store(tmp_path / "conversations.db")
    await store.open()
    try:
        yield store
    finally:
        await store.close()


async def _run_turn(
    opened_store: Store,
    settings: Settings,
    *,
    channel_name: str,
    key: str,
) -> FakeProvider:
    provider = FakeProvider()
    channel = FakeChannel()
    agent = Agent(
        store=opened_store,
        provider=provider,
        channel=channel,
        settings=settings,
    )
    await channel.start(agent.handle_turn)
    provider.queue_text("ok")
    await channel.inject(
        ChannelMessage(
            channel=channel_name,
            conversation_key=key,
            user_id="1000",
            text="hi",
            message_id="m1",
        )
    )
    assert len(provider.calls) == 1
    return provider


async def test_siri_conversation_gets_voice_note(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    opened_store: Store,
) -> None:
    settings = _make_settings(monkeypatch, tmp_path)
    provider = await _run_turn(opened_store, settings, channel_name="siri", key="siri:1700000000")
    assert _SIRI_VOICE_NOTE in provider.calls[0].system


async def test_discord_conversation_does_not_get_voice_note(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    opened_store: Store,
) -> None:
    settings = _make_settings(monkeypatch, tmp_path)
    provider = await _run_turn(opened_store, settings, channel_name="discord", key="thread-1")
    assert _SIRI_VOICE_NOTE not in provider.calls[0].system
