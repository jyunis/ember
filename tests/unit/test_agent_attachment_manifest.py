"""Manifest injection on the persisted user message (BC-3 / BC-4 / BC-5).

Drive a turn end-to-end against a real Store, FakeProvider, and
FakeChannel. After the turn, fetch the persisted user message and
assert:

- BC-5: text-only message (no attachments) → persisted text == msg.text
  (no manifest, no trailing whitespace).
- BC-4: with attachments → manifest is appended after a blank line, with
  the documented format and stable size formatting.
- BC-3 size formatting (sub-KB → "B", KB integer, MB one decimal at >=1MB).

We use the public Agent / Store / FakeChannel / FakeProvider wiring used
by `test_agent_loop.py` and inspect what hit the store.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ember.agent import Agent
from ember.channels.base import ChannelMessage, InboundAttachment
from ember.config import Settings
from ember.store.repo import Store
from tests.fakes.fake_channel import FakeChannel
from tests.fakes.fake_provider import FakeProvider
from tests.util import unstamp


def _make_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")
    monkeypatch.setenv("DISCORD_CONVERSATIONS_CHANNEL_ID", "2")
    monkeypatch.setenv("DISCORD_SCHEDULER_CHANNEL_ID", "3")
    return Settings()  # type: ignore[call-arg]


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
    )
    await channel.start(agent.handle_turn)
    try:
        yield store, provider, channel, agent
    finally:
        await store.close()


def _msg(text: str, *, attachments=()) -> ChannelMessage:
    return ChannelMessage(
        channel="discord",
        conversation_key="thread-1",
        user_id="100",
        text=text,
        message_id="m1",
        attachments=tuple(attachments),
    )


async def _persisted_user_text(store: Store) -> str:
    # Strip the TC-1 timestamp prefix so these assertions test the manifest,
    # not the stamp (stamping is covered in test_message_timestamps.py).
    c = await store.get_or_create_conversation("discord", "thread-1")
    msgs = await store.recent_messages(c.id)
    user = next(m for m in msgs if m.role == "user")
    return unstamp(user.content)


# --- BC-5: no manifest for text-only turns ---------------------------------


class TestNoManifestWithoutAttachments:
    async def test_text_only_turn_persists_user_text_unchanged(self, wiring) -> None:
        store, provider, channel, _ = wiring
        provider.queue_text("ok")

        await channel.inject(_msg("hello world"))

        assert (await _persisted_user_text(store)) == "hello world"

    async def test_text_only_turn_has_no_attachments_marker(self, wiring) -> None:
        # BC-5: manifest only appears when attachments are non-empty.
        # A text-only turn must not introduce the marker.
        store, provider, channel, _ = wiring
        provider.queue_text("ok")

        await channel.inject(_msg("just text"))

        assert "[Attachments:" not in (await _persisted_user_text(store))


# --- BC-4: manifest format -------------------------------------------------


class TestManifestFormat:
    async def test_manifest_appended_after_blank_line(self, wiring) -> None:
        store, provider, channel, _ = wiring
        provider.queue_text("ok")

        atts = (
            InboundAttachment(
                filename="a.txt",
                path="/data/uploads/thread-1/m1/a.txt",
                content_type="text/plain",
                size=100,
            ),
        )
        await channel.inject(_msg("look at this", attachments=atts))

        text = await _persisted_user_text(store)
        # Manifest is appended (not prepended) after a blank line.
        assert text.startswith("look at this\n\n[Attachments:"), text
        assert text.rstrip().endswith("]")

    async def test_manifest_one_bullet_per_attachment(self, wiring) -> None:
        store, provider, channel, _ = wiring
        provider.queue_text("ok")

        atts = (
            InboundAttachment(
                filename="a.txt",
                path="/data/uploads/t1/m1/a.txt",
                content_type="text/plain",
                size=100,
            ),
            InboundAttachment(
                filename="b.png",
                path="/data/uploads/t1/m1/b.png",
                content_type="image/png",
                size=2048,
            ),
        )
        await channel.inject(_msg("two files", attachments=atts))

        text = await _persisted_user_text(store)
        # One bullet per attachment in `- <abs_path> (<mime>, <size>)` form.
        assert "- /data/uploads/t1/m1/a.txt (text/plain, 100 B)" in text
        assert "- /data/uploads/t1/m1/b.png (image/png, 2 KB)" in text

    async def test_manifest_opens_and_closes_on_own_lines(self, wiring) -> None:
        store, provider, channel, _ = wiring
        provider.queue_text("ok")

        atts = (InboundAttachment(filename="a", path="/p/a", content_type="text/plain", size=10),)
        await channel.inject(_msg("x", attachments=atts))

        text = await _persisted_user_text(store)
        # `[Attachments:` on its own line, `]` on its own line.
        assert re.search(r"(^|\n)\[Attachments:\n", text), text
        assert re.search(r"\n\]\s*$", text), text

    async def test_manifest_alone_when_msg_text_empty(self, wiring) -> None:
        # When the user sent zero text, the manifest stands alone — no
        # leading blank line, no leading newline at the start.
        store, provider, channel, _ = wiring
        provider.queue_text("ok")

        atts = (InboundAttachment(filename="x", path="/p/x", content_type="text/plain", size=5),)
        await channel.inject(_msg("", attachments=atts))

        text = await _persisted_user_text(store)
        assert text.startswith("[Attachments:"), text


# --- BC-4: size formatting -------------------------------------------------


class TestManifestSizeFormatting:
    @pytest.mark.parametrize(
        "size,expected",
        [
            (0, "0 B"),
            (1, "1 B"),
            (1023, "1023 B"),
            (1024, "1 KB"),
            (2048, "2 KB"),
            (10 * 1024, "10 KB"),
            (1024 * 1024, "1.0 MB"),
            # 1.2 MB — test the one-decimal MB format.
            (int(1.2 * 1024 * 1024), "1.2 MB"),
        ],
    )
    async def test_size_format_in_manifest(self, wiring, size: int, expected: str) -> None:
        store, provider, channel, _ = wiring
        provider.queue_text("ok")

        atts = (
            InboundAttachment(
                filename="f.bin",
                path="/p/f.bin",
                content_type="application/octet-stream",
                size=size,
            ),
        )
        await channel.inject(_msg("here", attachments=atts))

        text = await _persisted_user_text(store)
        assert f"({expected})" in text or f", {expected})" in text, (
            f"size {size} → expected {expected!r} in manifest, got: {text!r}"
        )


# --- BC-5: assistant turn has no manifest --------------------------------


class TestAssistantTurnHasNoManifest:
    async def test_assistant_message_does_not_carry_manifest(self, wiring) -> None:
        store, provider, channel, _ = wiring
        provider.queue_text("the reply")

        atts = (InboundAttachment(filename="a", path="/p/a", content_type="image/png", size=512),)
        await channel.inject(_msg("hi", attachments=atts))

        c = await store.get_or_create_conversation("discord", "thread-1")
        msgs = await store.recent_messages(c.id)
        assistant = next(m for m in msgs if m.role == "assistant")
        # Manifest applies only to inbound user messages (BC-5).
        assert "[Attachments:" not in assistant.content
