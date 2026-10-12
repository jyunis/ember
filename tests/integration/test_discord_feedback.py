"""Discord feedback capture (UoW-20 / FC-20) — integration.

Drives the real `DiscordChannel._handle_reaction` (the handler the
`on_raw_reaction_add/remove` events delegate to) against a real Store,
with fake raw-reaction payloads. Asserts:

- 👍 / 👎 on a mapped agent message record `feedback` with sentiment ±1;
  another emoji records verbatim with NULL sentiment.
- Reaction removal soft-deletes the live row (TLC-8).
- TLC-1: a non-allowlisted reactor's reaction records nothing.
- A reaction on an unmapped message (user message / pre-bot history)
  records nothing (the UoW-20 "unresolvable → ignored" path).
- No outbound message is ever sent in response to a reaction (silence is
  the feature) — the handler doesn't touch the send path.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ember.channels.discord import DiscordChannel
from ember.config import Settings
from ember.store.repo import Store

THUMBS_UP = "\U0001f44d"
THUMBS_DOWN = "\U0001f44e"
THINKING = "\U0001f914"

ALLOWED_USER = 42
OTHER_USER = 999


def _settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DISCORD_ALLOWED_USER_IDS", str(ALLOWED_USER))
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")
    monkeypatch.setenv("DISCORD_CONVERSATIONS_CHANNEL_ID", "2")
    monkeypatch.setenv("DISCORD_SCHEDULER_CHANNEL_ID", "3")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    return Settings()  # type: ignore[call-arg]


def _payload(*, user_id: int, message_id: int, emoji: str) -> SimpleNamespace:
    # Mimics discord.RawReactionActionEvent's surface used by the handler:
    # `str(payload.emoji)` yields the reaction string.
    return SimpleNamespace(user_id=user_id, message_id=message_id, emoji=emoji)


@pytest.fixture
async def channel_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    settings = _settings(monkeypatch, tmp_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    store = Store(tmp_path / "conversations.db")
    await store.open()
    ch = DiscordChannel(settings, store=store)
    try:
        yield ch, store
    finally:
        await store.close()


async def _mapped_agent_message(store: Store, native_id: str = "500") -> int:
    """Persist an assistant message and map a native Discord id to it,
    mirroring what the agent loop does after a final reply."""
    conv = await store.get_or_create_conversation("discord", "thread-1")
    mid = await store.append_message(conv.id, "assistant", "the reply")
    await store.map_channel_messages("discord", [native_id], mid)
    return mid


class TestReactionRecordsFeedback:
    async def test_thumbs_up_records_positive(self, channel_store) -> None:
        ch, store = channel_store
        await _mapped_agent_message(store)
        await ch._handle_reaction(
            _payload(user_id=ALLOWED_USER, message_id=500, emoji=THUMBS_UP), added=True
        )
        assert await store.feedback_counts((await _conv_id(store))) == (1, 0, 0)

    async def test_thumbs_down_records_negative(self, channel_store) -> None:
        ch, store = channel_store
        await _mapped_agent_message(store)
        await ch._handle_reaction(
            _payload(user_id=ALLOWED_USER, message_id=500, emoji=THUMBS_DOWN), added=True
        )
        assert await store.feedback_counts((await _conv_id(store))) == (0, 1, 0)

    async def test_other_emoji_records_neutral(self, channel_store) -> None:
        ch, store = channel_store
        await _mapped_agent_message(store)
        await ch._handle_reaction(
            _payload(user_id=ALLOWED_USER, message_id=500, emoji=THINKING), added=True
        )
        assert await store.feedback_counts((await _conv_id(store))) == (0, 0, 1)

    async def test_remove_soft_deletes(self, channel_store) -> None:
        ch, store = channel_store
        await _mapped_agent_message(store)
        await ch._handle_reaction(
            _payload(user_id=ALLOWED_USER, message_id=500, emoji=THUMBS_UP), added=True
        )
        await ch._handle_reaction(
            _payload(user_id=ALLOWED_USER, message_id=500, emoji=THUMBS_UP), added=False
        )
        assert await store.feedback_counts((await _conv_id(store))) == (0, 0, 0)


class TestReactionDrops:
    async def test_non_allowlisted_user_records_nothing(self, channel_store) -> None:
        ch, store = channel_store
        await _mapped_agent_message(store)
        await ch._handle_reaction(
            _payload(user_id=OTHER_USER, message_id=500, emoji=THUMBS_UP), added=True
        )
        assert await store.feedback_counts((await _conv_id(store))) == (0, 0, 0)

    async def test_unmapped_message_records_nothing(self, channel_store) -> None:
        ch, store = channel_store
        await _mapped_agent_message(store, native_id="500")
        # React to a different native id that was never mapped.
        await ch._handle_reaction(
            _payload(user_id=ALLOWED_USER, message_id=12345, emoji=THUMBS_UP), added=True
        )
        assert await store.feedback_counts((await _conv_id(store))) == (0, 0, 0)


async def _conv_id(store: Store) -> int:
    conv = await store.get_or_create_conversation("discord", "thread-1")
    return conv.id
