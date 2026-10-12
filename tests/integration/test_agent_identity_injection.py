"""Agent identity injection — v1 surface change.

Per the architecture doc § Soul + profile:
- The agent constructor accepts an optional `identity: IdentityStore`.
- When present, every Provider call's `system` argument is
  `identity.system_prompt()` followed by the TC-1 conversation-timing note.
- When absent, the identity sections are empty and the system block is just
  the TC-1 note — it is unconditional because per-turn stamping always is.

v15 (FC-31, doc #56): the static `_HISTORY_RECALL_NOTE` is appended right
after `_TRANSCRIPT_TIME_NOTE` in every composition — one line pointing the
model at the history tools. Like the time note it is a code constant, so
the block stays byte-stable (TLC-12).

TLC-12: across consecutive turns with no underlying file change, the
system block is byte-identical (prompt-cache stability).

These tests wire a real Store + FakeProvider + FakeChannel + a real
IdentityStore on `tmp_path`, then assert the observable shape of the
system arg into Provider.complete.

We don't read implementation files — just the Agent.__init__ signature
and `_system_prompt` signature in `src/ember/agent.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ember.agent import Agent, _HISTORY_RECALL_NOTE, _SANDBOX_NOTE, _TRANSCRIPT_TIME_NOTE
from ember.channels.base import ChannelMessage
from ember.config import Settings
from ember.identity.store import IdentityStore
from ember.store.repo import Store
from tests.fakes.fake_channel import FakeChannel
from tests.fakes.fake_provider import FakeProvider


# --- Fixtures -------------------------------------------------------------


def _make_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")
    monkeypatch.setenv("DISCORD_CONVERSATIONS_CHANNEL_ID", "2")
    monkeypatch.setenv("DISCORD_SCHEDULER_CHANNEL_ID", "3")
    return Settings()  # type: ignore[call-arg]


def _msg(text: str, *, key: str = "thread-1", mid: str = "m1") -> ChannelMessage:
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


# --- v0 behavior preserved when identity is None -------------------------


class TestNoIdentity:
    async def test_system_is_timing_note_only_when_identity_none(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
    ) -> None:
        settings = _make_settings(monkeypatch, tmp_path)
        provider = FakeProvider()
        channel = FakeChannel()
        agent = Agent(
            store=opened_store,
            provider=provider,
            channel=channel,
            settings=settings,
            identity=None,
        )
        await channel.start(agent.handle_turn)
        provider.queue_text("ok")

        await channel.inject(_msg("hi"))

        assert len(provider.calls) == 1
        # TC-1: no identity → no soul/profile, but the conversation-timing
        # note is always injected (stamping is unconditional). FC-31 appends
        # the static history-recall note right after it, so the block is
        # exactly those two notes rather than "".
        assert (
            provider.calls[0].system
            == _TRANSCRIPT_TIME_NOTE
            + IdentityStore.SECTION_SEPARATOR
            + _HISTORY_RECALL_NOTE
            + IdentityStore.SECTION_SEPARATOR
            + _SANDBOX_NOTE
        )


# --- Identity present: system is identity.system_prompt() ----------------


class TestWithIdentity:
    async def test_system_equals_identity_system_prompt(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
    ) -> None:
        settings = _make_settings(monkeypatch, tmp_path)
        identity = IdentityStore(tmp_path / "identity")
        await identity.ensure_initialized()

        provider = FakeProvider()
        channel = FakeChannel()
        agent = Agent(
            store=opened_store,
            provider=provider,
            channel=channel,
            settings=settings,
            identity=identity,
        )
        await channel.start(agent.handle_turn)
        provider.queue_text("ok")

        # Snapshot what the system prompt should be at the moment of the
        # turn (no edits between snapshot and turn — files are stable). TC-1
        # appends the conversation-timing note after the identity base;
        # FC-31 appends the history-recall note after that.
        expected = (
            identity.system_prompt()
            + IdentityStore.SECTION_SEPARATOR
            + _TRANSCRIPT_TIME_NOTE
            + IdentityStore.SECTION_SEPARATOR
            + _HISTORY_RECALL_NOTE
            + IdentityStore.SECTION_SEPARATOR
            + _SANDBOX_NOTE
        )

        await channel.inject(_msg("hi"))

        assert len(provider.calls) == 1
        assert provider.calls[0].system == expected
        assert provider.calls[0].system != ""

    async def test_system_byte_stable_across_consecutive_turns(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
    ) -> None:
        # TLC-12: with no underlying file change, system across turns is
        # byte-identical.
        settings = _make_settings(monkeypatch, tmp_path)
        identity = IdentityStore(tmp_path / "identity")
        await identity.ensure_initialized()

        provider = FakeProvider()
        channel = FakeChannel()
        agent = Agent(
            store=opened_store,
            provider=provider,
            channel=channel,
            settings=settings,
            identity=identity,
        )
        await channel.start(agent.handle_turn)
        provider.queue_text("a")
        provider.queue_text("b")

        await channel.inject(_msg("first", mid="m1"))
        await channel.inject(_msg("second", mid="m2"))

        assert len(provider.calls) == 2
        assert provider.calls[0].system == provider.calls[1].system
        assert provider.calls[0].system != ""
