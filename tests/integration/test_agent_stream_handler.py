"""Agent stream handler under the v16 consolidation (TLC-27, doc #57).

The agent still constructs a `stream_handler` per turn and hands it to
`Provider.complete` — the observer needs the events for the activity
message's tool count / last tool (UoW-31) and for block streaming
(UoW-32). What CHANGED in v16: the separate per-tool `kind="status"`
posts are REMOVED. `show_tool_calls` now governs whether tool detail
appears in the single edited activity message (see
tests/unit/test_turn_activity.py), not whether separate messages are
posted. The ❌ tool-error path is unchanged (covered in
tests/unit/test_stream_observer_tool_errors.py).

Pinned here:

  - `tool_use` stream events produce NO separate status messages.
  - Text-only assistant events produce no status messages either.
  - The final reply still lands after tool events.
  - The provider still receives a stream_handler on every turn.

The FakeProvider's `queue_text_with_events` plays synthetic stream
events through the agent's handler before returning the
CompletionResult — see `tests/fakes/fake_provider.py`.
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
        yield store, provider, channel, agent, settings
    finally:
        await store.close()


def _msg(text: str = "hi", *, key: str = "thread-1", mid: str = "m1") -> ChannelMessage:
    return ChannelMessage(
        channel="discord",
        conversation_key=key,
        user_id="1000",
        text=text,
        message_id=mid,
    )


def _tool_use_event(tu_id: str, text: str) -> dict:
    return {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": tu_id,
                    "name": "agent__echo",
                    "input": {"text": text},
                }
            ]
        },
    }


# --- Per-tool status posts are gone (TLC-27 consolidation) ----------------


class TestToolUseEventsEmitNoSeparateStatus:
    async def test_single_tool_use_event_produces_no_status_message(self, wiring) -> None:
        _, provider, channel, _, _ = wiring
        provider.queue_text_with_events(
            "the answer",
            events=[_tool_use_event("toolu_1", "hi")],
        )

        await channel.inject(_msg("hello"))

        statuses = [s for s in channel.sent if s.kind == "status"]
        assert statuses == [], (
            "v16 removed the separate per-tool status posts — tool activity "
            "belongs in the edited activity message (TLC-27); got: "
            f"{[s.content for s in statuses]!r}"
        )

    async def test_many_tool_use_events_still_produce_no_status_messages(self, wiring) -> None:
        _, provider, channel, _, _ = wiring
        provider.queue_text_with_events(
            "ok",
            events=[
                _tool_use_event("toolu_1", "first"),
                _tool_use_event("toolu_2", "second"),
                _tool_use_event("toolu_3", "third"),
            ],
        )

        await channel.inject(_msg("hello"))

        statuses = [s for s in channel.sent if s.kind == "status"]
        assert statuses == []

    async def test_final_message_still_sent_after_tool_use_events(self, wiring) -> None:
        _, provider, channel, _, _ = wiring
        provider.queue_text_with_events(
            "the final answer",
            events=[_tool_use_event("toolu_1", "hi")],
        )

        await channel.inject(_msg("hello"))

        finals = [s for s in channel.sent if s.kind == "final"]
        assert len(finals) == 1
        assert finals[0].content == "the final answer"


# --- Text-only events do NOT emit status ---------------------------------


class TestTextOnlyEventsAreSilent:
    async def test_text_only_assistant_event_emits_no_status(self, wiring) -> None:
        _, provider, channel, _, _ = wiring
        provider.queue_text_with_events(
            "the answer",
            events=[
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "thinking out loud..."},
                        ]
                    },
                }
            ],
        )

        await channel.inject(_msg("hello"))

        statuses = [s for s in channel.sent if s.kind == "status"]
        assert statuses == []
        # And the final assistant reply still gets through.
        finals = [s for s in channel.sent if s.kind == "final"]
        assert len(finals) == 1

    async def test_no_events_at_all_emits_no_status(self, wiring) -> None:
        _, provider, channel, _, _ = wiring
        provider.queue_text("just a reply")

        await channel.inject(_msg("hello"))

        statuses = [s for s in channel.sent if s.kind == "status"]
        assert statuses == []


# --- Stream handler is wired into the provider ---------------------------


class TestStreamHandlerWired:
    async def test_provider_receives_a_stream_handler(self, wiring) -> None:
        # The agent loop must still hand a stream_handler to the provider
        # on every turn — the activity message's tool count and block
        # streaming both depend on the observer seeing events.
        _, provider, channel, _, _ = wiring
        provider.queue_text("ok")

        await channel.inject(_msg("hello"))

        assert len(provider.calls) == 1
        assert provider.calls[0].stream_handler_provided is True
