"""Partial-failure tolerance — BC-1 end-to-end.

Exercises `DiscordChannel._handle_message` through the full path:

- One attachment download fails, one succeeds.
- The successful attachment flows through to `on_message`.
- The user message is still delivered to the agent (text + the
  successful attachment).
- A status reply naming the failed file is sent to the channel.
- The resulting `ChannelMessage` only contains the successful
  attachment (BC-9: failures never enter the manifest path).

We mock httpx for downloads and synthesize a `discord.Message` /
`discord.Thread` shape via `MagicMock(spec=...)` so the adapter's
`isinstance(channel, discord.Thread)` check succeeds without
constructing a real Thread.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import discord
import pytest

from ember.channels.base import ChannelMessage
from ember.channels.discord import DiscordChannel
from ember.config import Settings


@dataclass
class FakeAtt:
    filename: str
    url: str
    size: int
    content_type: str = "application/octet-stream"
    id: int = 0


class _FakeResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        return None


class _FakeAsyncClient:
    def __init__(self, *, responses=None, errors=None) -> None:
        self._responses = responses or {}
        self._errors = errors or {}

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        if url in self._errors:
            raise self._errors[url]
        return _FakeResponse(self._responses.get(url, b""))


def _settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")
    monkeypatch.setenv("DISCORD_CONVERSATIONS_CHANNEL_ID", "2")
    monkeypatch.setenv("DISCORD_SCHEDULER_CHANNEL_ID", "3")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    s = Settings()  # type: ignore[call-arg]
    return s


async def test_partial_failure_one_succeeds_one_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    s = _settings(monkeypatch, tmp_path)
    s.data_dir.mkdir(parents=True, exist_ok=True)
    # The user must be allow-listed for `_handle_message` to proceed.
    s.discord_allowed_user_ids = [123]

    ch = DiscordChannel(s)

    # Intercept httpx so one URL succeeds and one fails.
    url_good = "https://cdn.discord/good"
    url_bad = "https://cdn.discord/bad"

    def _client_factory(*args: Any, **kwargs: Any) -> _FakeAsyncClient:
        return _FakeAsyncClient(
            responses={url_good: b"yay"},
            errors={url_bad: httpx.HTTPError("simulated download failure")},
        )

    import ember.channels.discord as dmod

    monkeypatch.setattr(dmod.httpx, "AsyncClient", _client_factory)

    # Stub `send` so `_handle_message`'s status path doesn't try to
    # resolve a real Discord thread.
    sent: list[tuple[str, str, str]] = []

    async def fake_send(conversation_key: str, content: str, *, kind: str = "reply") -> None:
        sent.append((conversation_key, content, kind))

    monkeypatch.setattr(ch, "send", fake_send)

    # Capture what arrives at the on_message handler.
    received: list[ChannelMessage] = []

    async def handler(msg: ChannelMessage) -> None:
        received.append(msg)

    ch._on_message = handler

    # Forge enough of a discord.Message / discord.Thread to satisfy the
    # intake path. `MagicMock(spec=...)` makes `isinstance(...)` return
    # True against the spec class.
    thread = MagicMock(spec=discord.Thread)
    thread.id = 555
    thread.parent_id = s.discord_conversations_channel_id

    author = SimpleNamespace(id=123)
    msg = MagicMock(spec=discord.Message)
    msg.id = 9999
    msg.author = author
    msg.channel = thread
    msg.content = "look at these"
    msg.attachments = [
        FakeAtt(filename="good.txt", url=url_good, size=3, content_type="text/plain"),
        FakeAtt(filename="bad.txt", url=url_bad, size=3, content_type="text/plain"),
    ]

    await ch._handle_message(msg)

    # The handler was called once.
    assert len(received) == 1, received
    cm = received[0]

    # BC-9 / BC-1: only the successful attachment is on the message.
    assert len(cm.attachments) == 1, [a.filename for a in cm.attachments]
    assert cm.attachments[0].filename == "good.txt"

    # Text is preserved.
    assert cm.text == "look at these"

    # A status reply was sent, mentioning the failed filename.
    status_msgs = [c for c in sent if c[2] == "status"]
    assert status_msgs, sent
    joined = "\n".join(c[1] for c in status_msgs)
    assert "bad.txt" in joined, joined
