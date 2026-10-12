"""v16 progressive text delivery — TLC-28 / UoW-32 / FC-33 (doc #57).

Three layers under test:

1. `CompletionResult.text_parts: tuple[str, ...] | None = None` — the
   provider-level block list (FC-33). Defaults None (legacy single
   send).
2. `claude_cli` sets `text_parts` to the ordered non-tool text blocks
   observed in its stream, and `text == <join>(text_parts)` under the
   provider's pinned join rule. The join rule itself is the
   implementer's to pin; these tests assert the structural invariant —
   every part appears exactly once, in order, with nothing but
   whitespace separators between and around them — so any sane join
   satisfies it and any duplication/loss/reorder fails it.
3. The agent/observer path (UoW-32): completed text blocks post as
   their own channel messages during the turn, in order; the post-turn
   final send carries only the unstreamed remainder (by posted-block
   COUNT, never character math) and is skipped when everything
   streamed; a failed block post holds that block and all later ones
   for the final send; `text_parts=None`, `stream_text_blocks=False`,
   and siri conversations all take the legacy single-final-send path.

Tests-first: failures against current src (no `text_parts` field, no
observer block posting) are EXPECTED until the implementer lands
UoW-32/FC-33.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from ember.agent import Agent
from ember.channels.base import ChannelMessage
from ember.config import Settings
from ember.providers.base import CompletionResult, Message, Usage
from ember.providers.claude_cli import ClaudeCliProvider
from ember.store.repo import Store
from tests.fakes.fake_channel import FakeChannel, SentMessage
from tests.fakes.fake_provider import FakeProvider

# --- Shared helpers -------------------------------------------------------


def _assert_join_invariant(text: str, parts: tuple[str, ...]) -> None:
    """FC-33: `text == <join>(text_parts)` for the provider's join rule.

    Asserted structurally (parts appear exactly once, in order, with
    whitespace-only separators and nothing else) so the test pins the
    exactly-once invariant without pinning the separator string.
    """
    idx = 0
    for i, part in enumerate(parts):
        found = text.find(part, idx)
        assert found != -1, f"part {i} missing or out of order in text: {part!r} / {text!r}"
        between = text[idx:found]
        assert between.strip() == "", f"non-whitespace content before part {i}: {between!r}"
        idx = found + len(part)
    assert text[idx:].strip() == "", f"trailing content after last part: {text[idx:]!r}"


# ==========================================================================
# 1. CompletionResult.text_parts (FC-33)
# ==========================================================================


class TestTextPartsField:
    def test_defaults_none(self) -> None:
        result = CompletionResult(text="hello")
        assert result.text_parts is None, (
            "text_parts must default None so non-streaming providers keep "
            "the legacy single-send path (FC-33)"
        )

    def test_round_trips_ordered_tuple(self) -> None:
        result = CompletionResult(text="a\n\nb", text_parts=("a", "b"))
        assert result.text_parts == ("a", "b")


# ==========================================================================
# 2. claude_cli stream → text_parts
# ==========================================================================


def _assistant_text_line(text: str) -> bytes:
    event = {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
    return (json.dumps(event) + "\n").encode()


def _assistant_tool_line(tu_id: str, name: str) -> bytes:
    event = {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "id": tu_id, "name": name, "input": {}}]},
    }
    return (json.dumps(event) + "\n").encode()


def _result_line(text: str) -> bytes:
    event = {
        "type": "result",
        "is_error": False,
        "result": text,
        "session_id": "fake-sess",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    return (json.dumps(event) + "\n").encode()


def _intercept_subprocess(
    monkeypatch: pytest.MonkeyPatch, stdout_lines: list[bytes]
) -> list[list[str]]:
    """Fake `asyncio.create_subprocess_exec` so ClaudeCliProvider reads a
    scripted stream-json stdout (pattern: tests/unit/test_claude_cli_prompt.py)."""
    captured: list[list[str]] = []

    class _FakeStream:
        def __init__(self, lines: list[bytes]) -> None:
            self._lines = list(lines)

        def __aiter__(self) -> "_FakeStream":
            return self

        async def __anext__(self) -> bytes:
            if not self._lines:
                raise StopAsyncIteration
            return self._lines.pop(0)

        async def read(self) -> bytes:
            return b""

    class _FakeProc:
        def __init__(self) -> None:
            self.stdout = _FakeStream(stdout_lines)
            self.stderr = _FakeStream([])

        async def wait(self) -> int:
            return 0

    async def _fake_create(*args: Any, **kwargs: Any):  # noqa: ANN401, ARG001
        captured.append(list(args))
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    return captured


class TestClaudeCliTextParts:
    async def test_three_blocks_interleaved_with_tools(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parts = (
            "First block of narration.",
            "Second block, after a tool call.",
            "Third and final block.",
        )
        _intercept_subprocess(
            monkeypatch,
            [
                _assistant_text_line(parts[0]),
                _assistant_tool_line("tu_1", "mcp__agent__web_fetch"),
                _assistant_text_line(parts[1]),
                _assistant_tool_line("tu_2", "mcp__agent__web_search"),
                _assistant_text_line(parts[2]),
                _result_line("\n\n".join(parts)),
            ],
        )
        provider = ClaudeCliProvider()
        result = await provider.complete(system="", messages=[Message("user", "go")])

        assert result.text_parts == parts, (
            "text_parts must be the ordered tuple of text blocks, tool_use "
            f"blocks excluded; got {result.text_parts!r}"
        )
        _assert_join_invariant(result.text, result.text_parts)

    async def test_single_block(self, monkeypatch: pytest.MonkeyPatch) -> None:
        block = "Just one block."
        _intercept_subprocess(
            monkeypatch,
            [_assistant_text_line(block), _result_line(block)],
        )
        provider = ClaudeCliProvider()
        result = await provider.complete(system="", messages=[Message("user", "go")])

        assert result.text_parts == (block,)
        _assert_join_invariant(result.text, result.text_parts)


# ==========================================================================
# 3. Agent/observer block posting (UoW-32)
# ==========================================================================

B1 = "BLOCK-ONE unmistakable alpha marker."
B2 = "BLOCK-TWO unmistakable beta marker."
B3 = "BLOCK-THREE unmistakable gamma marker."


def _make_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")
    monkeypatch.setenv("DISCORD_CONVERSATIONS_CHANNEL_ID", "2")
    monkeypatch.setenv("DISCORD_SCHEDULER_CHANNEL_ID", "3")
    return Settings()  # type: ignore[call-arg]


def _msg(text: str = "hi", *, channel: str = "discord", key: str = "thread-1") -> ChannelMessage:
    return ChannelMessage(
        channel=channel,
        conversation_key=key,
        user_id="1000",
        text=text,
        message_id="m1",
    )


@pytest.fixture
async def opened_store(tmp_path: Path):
    store = Store(tmp_path / "conversations.db")
    await store.open()
    try:
        yield store
    finally:
        await store.close()


class _ProbeProvider(FakeProvider):
    """Snapshots `channel.sent` at the moment `complete` returns, so
    tests can tell mid-turn block posts from the agent's post-turn final
    send without racing."""

    def __init__(self, channel: FakeChannel) -> None:
        super().__init__()
        self.name = "claude_cli"
        self._channel = channel
        self.sent_at_return: list[SentMessage] = []

    async def complete(
        self,
        system: str,
        messages: list,  # type: ignore[type-arg]
        tools: list | None = None,  # type: ignore[type-arg]
        model: str | None = None,
        stream_handler: Any = None,
        session_id: str | None = None,
    ):
        result = await super().complete(
            system,
            messages,
            tools=tools,
            model=model,
            stream_handler=stream_handler,
            session_id=session_id,
        )
        self.sent_at_return = list(self._channel.sent)
        return result


async def _wire(store: Store, settings: Settings) -> tuple[FakeChannel, _ProbeProvider]:
    channel = FakeChannel()
    provider = _ProbeProvider(channel)
    agent = Agent(store=store, provider=provider, channel=channel, settings=settings)
    await channel.start(agent.handle_turn)
    return channel, provider


def _text_event(text: str) -> dict[str, Any]:
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def _tool_event(tu_id: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {
            "content": [{"type": "tool_use", "id": tu_id, "name": "agent__echo", "input": {}}]
        },
    }


def _completion(parts: tuple[str, ...]) -> CompletionResult:
    return CompletionResult(
        text="\n\n".join(parts),
        text_parts=parts,
        usage=Usage(input_tokens=1, output_tokens=1, subscription=True),
    )


def _reply_contents(sent: list[SentMessage]) -> list[str]:
    """Contents of non-status sends (block posts + finals), in order."""
    return [s.content for s in sent if s.kind != "status"]


def _count_occurrences(sent: list[SentMessage], marker: str) -> int:
    return sum(marker in s.content for s in sent)


class TestBlockPosting:
    async def test_blocks_posted_in_order_during_turn_and_final_send_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened_store: Store
    ) -> None:
        settings = _make_settings(monkeypatch, tmp_path)
        channel, provider = await _wire(opened_store, settings)
        provider.queue_response_with_events(
            _completion((B1, B2, B3)),
            [
                _text_event(B1),
                _tool_event("tu_1"),
                _text_event(B2),
                _tool_event("tu_2"),
                _text_event(B3),
            ],
        )

        await channel.inject(_msg())

        assert _reply_contents(provider.sent_at_return) == [B1, B2, B3], (
            "each completed text block must post as its own message, in order, DURING the turn"
        )
        after = channel.sent[len(provider.sent_at_return) :]
        assert _reply_contents(after) == [], (
            "everything streamed → the final send is skipped entirely "
            f"(TLC-28); got post-turn sends: {_reply_contents(after)!r}"
        )
        for block in (B1, B2, B3):
            assert _count_occurrences(channel.sent, block) == 1, (
                f"exactly-once delivery violated for {block!r}"
            )

    async def test_final_send_carries_only_unstreamed_remainder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened_store: Store
    ) -> None:
        """Two of three blocks stream; the final send is text_parts[2:]
        — computed by posted-block count, never character math."""
        settings = _make_settings(monkeypatch, tmp_path)
        channel, provider = await _wire(opened_store, settings)
        provider.queue_response_with_events(
            _completion((B1, B2, B3)),
            [_text_event(B1), _tool_event("tu_1"), _text_event(B2)],
        )

        await channel.inject(_msg())

        assert _reply_contents(provider.sent_at_return) == [B1, B2]
        after = channel.sent[len(provider.sent_at_return) :]
        remainder = _reply_contents(after)
        assert len(remainder) == 1, f"expected one remainder send; got {remainder!r}"
        assert B3 in remainder[0]
        assert B1 not in remainder[0] and B2 not in remainder[0], (
            "final send must carry ONLY the unstreamed remainder"
        )
        for block in (B1, B2, B3):
            assert _count_occurrences(channel.sent, block) == 1

    async def test_failed_block_post_holds_that_block_and_all_later_ones(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened_store: Store
    ) -> None:
        """UoW-32 failure mode: a mid-list post failure — the failed
        block AND every later block ride the final send (exactly-once is
        preserved by count)."""
        settings = _make_settings(monkeypatch, tmp_path)
        channel, provider = await _wire(opened_store, settings)
        channel.fail_sends_containing.append(B2)
        provider.queue_response_with_events(
            _completion((B1, B2, B3)),
            [_text_event(B1), _text_event(B2), _text_event(B3)],
        )

        await channel.inject(_msg())

        assert _reply_contents(provider.sent_at_return) == [B1], (
            "after B2's post fails, B3 must be held back too — no "
            f"mid-turn posts past the failure; got "
            f"{_reply_contents(provider.sent_at_return)!r}"
        )
        assert any(B2 in content for _, content in channel.failed_sends), (
            "the injected failure should have hit B2's post"
        )
        after = channel.sent[len(provider.sent_at_return) :]
        joined_after = "\n".join(_reply_contents(after))
        assert B2 in joined_after and B3 in joined_after, (
            f"held-back blocks must ride the final send; got {joined_after!r}"
        )
        assert joined_after.find(B2) < joined_after.find(B3), "remainder keeps block order"
        assert B1 not in joined_after
        for block in (B1, B2, B3):
            assert _count_occurrences(channel.sent, block) == 1, (
                f"exactly-once delivery violated for {block!r}"
            )


class TestLegacyPaths:
    async def test_text_parts_none_takes_single_final_send(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened_store: Store
    ) -> None:
        """A provider that reports no text parts (failover, FC-33) keeps
        today's buffered path: exactly one final send."""
        settings = _make_settings(monkeypatch, tmp_path)
        channel, provider = await _wire(opened_store, settings)
        provider.queue_text("plain buffered reply")  # text_parts defaults None

        await channel.inject(_msg())

        replies = [s for s in channel.sent if s.kind != "status"]
        assert len(replies) == 1
        assert replies[0].kind == "final"
        assert replies[0].content == "plain buffered reply"

    async def test_stream_flag_off_means_no_mid_turn_posts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened_store: Store
    ) -> None:
        monkeypatch.setenv("STREAM_TEXT_BLOCKS", "false")
        settings = _make_settings(monkeypatch, tmp_path)
        assert settings.stream_text_blocks is False
        channel, provider = await _wire(opened_store, settings)
        provider.queue_response_with_events(
            _completion((B1, B2)),
            [_text_event(B1), _text_event(B2)],
        )

        await channel.inject(_msg())

        assert _reply_contents(provider.sent_at_return) == [], (
            "stream_text_blocks=False must suppress all mid-turn text posts"
        )
        replies = _reply_contents(channel.sent)
        assert len(replies) == 1, f"expected one buffered final send; got {replies!r}"
        assert B1 in replies[0] and B2 in replies[0]
        for block in (B1, B2):
            assert _count_occurrences(channel.sent, block) == 1

    async def test_siri_conversation_never_streams_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened_store: Store
    ) -> None:
        """Voice needs one utterance — siri turns keep the buffered path
        (doc #57 non-goals / TLC-28)."""
        settings = _make_settings(monkeypatch, tmp_path)
        channel, provider = await _wire(opened_store, settings)
        provider.queue_response_with_events(
            _completion((B1, B2)),
            [_text_event(B1), _text_event(B2)],
        )

        await channel.inject(_msg(channel="siri", key="siri:1000"))

        assert _reply_contents(provider.sent_at_return) == [], (
            "siri conversations must not receive mid-turn text posts"
        )
        replies = _reply_contents(channel.sent)
        assert len(replies) == 1, f"expected one buffered final send; got {replies!r}"
        assert B1 in replies[0] and B2 in replies[0]
