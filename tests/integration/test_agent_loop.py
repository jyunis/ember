"""Agent turn loop (UoW-1, UoW-3, UoW-14, TLC-5, TLC-7, TLC-10, TLC-12).

These exercise the full v0 happy path and failure modes by wiring an
`Agent` against a real `Store` (file-backed, per-test) plus the
FakeProvider and FakeChannel stubs.

We don't read implementation files — these tests assert observable
behavior promised by the contracts doc. If a contract is implemented
differently than described, the test fails and the human resolves the
spec gap.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ember.agent import Agent
from ember.channels.base import ChannelMessage
from ember.config import Settings
from ember.errors import (
    AuthError,
    RateLimitError,
    ToolError,
    ToolRetryable,
    TransportError,
)
from ember.providers.base import CompletionResult, Usage
from ember.store.repo import Store
from ember.tools.base import ToolResult
from tests.fakes.fake_channel import FakeChannel
from tests.fakes.fake_provider import FakeProvider
from tests.util import unstamp


# --- Fixtures -------------------------------------------------------------


def _make_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    """Build a Settings instance with the bare minimum required fields.

    NOTE: we avoid setting DISCORD_ALLOWED_USER_IDS via env because
    pydantic-settings 2.x parses env-sourced complex types (list[int])
    as JSON before any field_validator runs, so the CSV-mode validator
    in config.py doesn't fire on env input. See the test_config.py
    failures and the spec-ambiguity note in the report.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")
    monkeypatch.setenv("DISCORD_CONVERSATIONS_CHANNEL_ID", "2")
    monkeypatch.setenv("DISCORD_SCHEDULER_CHANNEL_ID", "3")
    return Settings()  # type: ignore[call-arg]


@pytest.fixture
async def wiring(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Standard agent wiring: opened Store + FakeProvider + FakeChannel +
    Agent constructed against the four. Channel is `start`-ed with the
    agent's `handle_turn` as the registered handler."""
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


def _msg(
    text: str, *, key: str = "thread-1", user: str = "1000", mid: str = "m1"
) -> ChannelMessage:
    return ChannelMessage(
        channel="discord",
        conversation_key=key,
        user_id=user,
        text=text,
        message_id=mid,
    )


# --- UoW-1: full happy-path turn -----------------------------------------


class TestHappyPathTurn:
    async def test_turn_persists_user_and_assistant_messages(self, wiring) -> None:
        store, provider, channel, agent, _ = wiring
        provider.queue_text("hello back", input_tokens=4, output_tokens=2, subscription=True)

        await channel.inject(_msg("hi"))

        # UoW-14: conversation created.
        c = await store.get_or_create_conversation("discord", "thread-1")
        msgs = await store.recent_messages(c.id)
        # User then assistant, oldest-first.
        assert [m.role for m in msgs] == ["user", "assistant"]
        assert unstamp(msgs[0].content) == "hi"
        assert msgs[1].content == "hello back"

    async def test_turn_calls_provider_complete_once(self, wiring) -> None:
        _, provider, channel, _, _ = wiring
        provider.queue_text("ok")
        await channel.inject(_msg("hi"))
        assert len(provider.calls) == 1

    async def test_turn_sends_final_message_via_channel(self, wiring) -> None:
        _, provider, channel, _, _ = wiring
        provider.queue_text("the answer")
        await channel.inject(_msg("hi"))
        finals = [s for s in channel.sent if s.kind == "final"]
        assert len(finals) == 1
        assert finals[0].content == "the answer"
        assert finals[0].conversation_key == "thread-1"

    async def test_final_reply_native_ids_mapped_to_assistant_row(self, wiring) -> None:
        # UoW-20 / FC-20 glue: after the final reply, the agent loop maps
        # the channel-native message id(s) to the assistant `messages` row
        # so a later reaction resolves. FakeChannel synthesizes a native id
        # per send and returns it; the loop records the map.
        store, provider, channel, _, _ = wiring
        provider.queue_text("the answer")
        await channel.inject(_msg("hi"))

        c = await store.get_or_create_conversation("discord", "thread-1")
        msgs = await store.recent_messages(c.id)
        assistant_id = next(m.id for m in msgs if m.role == "assistant")
        final = next(s for s in channel.sent if s.kind == "final")

        resolved = await store.message_id_for_channel_message("discord", final.message_id)
        assert resolved == assistant_id
        # And feedback recorded against the resolved id lands on the row.
        await store.record_feedback(resolved, "\U0001f44d")
        assert await store.feedback_counts(c.id) == (1, 0, 0)

    async def test_provider_sees_prior_turns_on_second_message(self, wiring) -> None:
        _, provider, channel, _, _ = wiring
        provider.queue_text("first reply")
        provider.queue_text("second reply")

        await channel.inject(_msg("first user msg", mid="m1"))
        await channel.inject(_msg("second user msg", mid="m2"))

        # Second provider call should have received the prior turns in
        # `messages`. Exact ordering is oldest-first per the prompt
        # assembly contract.
        second_call = provider.calls[1]
        contents = [unstamp(m.content) for m in second_call.messages]
        # We expect to see both prior user/assistant turns plus the
        # current user message at the end.
        assert "first user msg" in contents
        assert "first reply" in contents
        assert contents[-1] == "second user msg"


# --- UoW-14: conversation creation reuse ----------------------------------


class TestConversationReuse:
    async def test_first_msg_creates_second_msg_reuses(self, wiring) -> None:
        store, provider, channel, _, _ = wiring
        provider.queue_text("a")
        provider.queue_text("b")

        await channel.inject(_msg("hi 1", mid="m1"))
        await channel.inject(_msg("hi 2", mid="m2"))

        # One conversation row, four messages (2 user + 2 assistant).
        c = await store.get_or_create_conversation("discord", "thread-1")
        assert (await store.message_count(c.id)) == 4

    async def test_distinct_keys_distinct_conversations(self, wiring) -> None:
        store, provider, channel, _, _ = wiring
        provider.queue_text("a")
        provider.queue_text("b")

        await channel.inject(_msg("hi 1", key="thread-A", mid="ma"))
        await channel.inject(_msg("hi 2", key="thread-B", mid="mb"))

        ca = await store.get_or_create_conversation("discord", "thread-A")
        cb = await store.get_or_create_conversation("discord", "thread-B")
        assert ca.id != cb.id


# --- TLC-5: cost transparency -------------------------------------------


class TestCostTransparency:
    async def test_subscription_turn_persists_with_cost_null(self, wiring) -> None:
        store, provider, channel, _, _ = wiring
        provider.queue_response(
            CompletionResult(
                text="sub reply",
                usage=Usage(input_tokens=5, output_tokens=10, subscription=True),
            )
        )

        await channel.inject(_msg("hi"))
        c = await store.get_or_create_conversation("discord", "thread-1")
        msgs = await store.recent_messages(c.id)
        a = next(m for m in msgs if m.role == "assistant")
        assert a.usage is not None
        assert a.usage.subscription is True
        assert a.usage.cost_usd is None

    async def test_metered_turn_persists_with_cost_value(self, wiring) -> None:
        store, provider, channel, _, _ = wiring
        provider.queue_response(
            CompletionResult(
                text="metered reply",
                usage=Usage(
                    input_tokens=100,
                    output_tokens=50,
                    cost_usd=0.42,
                    subscription=False,
                ),
            )
        )

        await channel.inject(_msg("hi"))
        c = await store.get_or_create_conversation("discord", "thread-1")
        msgs = await store.recent_messages(c.id)
        a = next(m for m in msgs if m.role == "assistant")
        assert a.usage is not None
        assert a.usage.subscription is False
        assert a.usage.cost_usd == pytest.approx(0.42)


# --- TLC-7 / UoW-3: failure surfacing ------------------------------------


class TestFailureSurfacing:
    async def test_rate_limit_surfaces_to_channel(self, wiring) -> None:
        store, provider, channel, _, _ = wiring
        provider.queue_error(RateLimitError("slow down"))

        await channel.inject(_msg("hi"))

        # The user should see *some* outbound message describing the
        # failure. Spec doesn't pin exact wording; we check that at
        # least one outbound message landed and that the agent loop
        # didn't propagate the exception out.
        assert len(channel.sent) >= 1

    async def test_user_message_persists_on_provider_error(self, wiring) -> None:
        store, provider, channel, _, _ = wiring
        provider.queue_error(AuthError("bad key"))

        await channel.inject(_msg("hi"))

        # TLC-7 / UoW-1 atomicity: user message is persisted; partial
        # assistant state is not committed.
        c = await store.get_or_create_conversation("discord", "thread-1")
        msgs = await store.recent_messages(c.id)
        roles = [m.role for m in msgs]
        assert "user" in roles
        # No assistant row should exist for this failed turn.
        assert "assistant" not in roles

    async def test_transport_error_surfaces_and_is_recoverable(self, wiring) -> None:
        store, provider, channel, _, _ = wiring
        provider.queue_error(TransportError("conn reset"))

        await channel.inject(_msg("hi"))

        # The agent loop must not let the exception escape.
        assert len(channel.sent) >= 1

    async def test_handle_turn_does_not_raise_on_provider_error(self, wiring) -> None:
        # Direct call form, in case the channel layer swallows things —
        # the agent loop itself promises TLC-7.
        store, provider, channel, agent, _ = wiring
        provider.queue_error(RateLimitError())
        # Should not raise.
        await agent.handle_turn(_msg("hi"))


# --- Transient tool-failure retry (network/DNS blips) --------------------


class TestToolDispatchRetry:
    """`_dispatch_tool_call_with_retry` absorbs transient tool failures
    (ToolRetryable / TransportError) with bounded backoff, so a brief
    egress/DNS blip doesn't surface a hard error to the model. Terminal
    errors propagate immediately."""

    @staticmethod
    def _patch_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
        sleeps: list[float] = []

        async def _no_sleep(delay: float) -> None:
            sleeps.append(delay)

        monkeypatch.setattr("ember.agent.asyncio.sleep", _no_sleep)
        return sleeps

    async def test_retries_transient_then_succeeds(
        self, wiring, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, _, _, agent, _ = wiring
        sleeps = self._patch_sleep(monkeypatch)
        ok = ToolResult(output={"ok": True})
        calls = {"n": 0}

        async def _flaky(name, args, per_turn_tools):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise TransportError("web_fetch transport error: ConnectError")
            return ok

        monkeypatch.setattr(agent, "_dispatch_tool_call", _flaky)

        result = await agent._dispatch_tool_call_with_retry("web_fetch", {}, None)
        assert result is ok
        assert calls["n"] == 3  # two transient failures, then success
        assert sleeps == [0.5, 1.5]  # backoff schedule honored

    async def test_exhausted_retries_propagate(
        self, wiring, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, _, _, agent, _ = wiring
        sleeps = self._patch_sleep(monkeypatch)
        calls = {"n": 0}

        async def _always_flaky(name, args, per_turn_tools):
            calls["n"] += 1
            raise ToolRetryable("web_browse: browserless server error (HTTP 502)")

        monkeypatch.setattr(agent, "_dispatch_tool_call", _always_flaky)

        with pytest.raises(ToolRetryable):
            await agent._dispatch_tool_call_with_retry("web_browse", {}, None)
        assert calls["n"] == 3  # initial + two retries, then give up
        assert sleeps == [0.5, 1.5]

    async def test_terminal_error_not_retried(
        self, wiring, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, _, _, agent, _ = wiring
        sleeps = self._patch_sleep(monkeypatch)
        calls = {"n": 0}

        async def _terminal(name, args, per_turn_tools):
            calls["n"] += 1
            raise ToolError("web_fetch: unsupported content-type: image/png")

        monkeypatch.setattr(agent, "_dispatch_tool_call", _terminal)

        with pytest.raises(ToolError):
            await agent._dispatch_tool_call_with_retry("web_fetch", {}, None)
        assert calls["n"] == 1  # no retry on a terminal failure
        assert sleeps == []


# --- TLC-10: idempotency on duplicate inbound -----------------------------
#
# FC-1 places channel-level dedup on the adapter ("the adapter dedupes
# inbound deliveries by the channel's native message ID before invoking
# on_message"). The corresponding test was relocated to
# tests/integration/test_discord_channel.py per the v0 resolution table.
# It is skipped pending the real DiscordChannel unit-test surface.


# --- TLC-12: prompt-cache stability --------------------------------------


class TestPromptCacheStability:
    async def test_system_block_byte_stable_across_turns(self, wiring) -> None:
        # TLC-12: with no underlying source change between turns, the
        # `system` argument passed into `Provider.complete` must be
        # byte-identical. v0 has no soul/profile, so the system block is
        # most likely empty — we just assert it's stable.
        _, provider, channel, _, _ = wiring
        provider.queue_text("a")
        provider.queue_text("b")
        provider.queue_text("c")

        await channel.inject(_msg("first", mid="m1"))
        await channel.inject(_msg("second", mid="m2"))
        await channel.inject(_msg("third", mid="m3"))

        systems = [c.system for c in provider.calls]
        # All three calls saw the exact same system bytes.
        assert systems[0] == systems[1] == systems[2]
        # Also assert it's a string (not None) — the Provider protocol
        # types `system: str`, not Optional.
        assert isinstance(systems[0], str)


# --- TLC-1: allowlist enforcement (channel-level) -------------------------
#
# Per FC-1 the *channel adapter* enforces TLC-1 before invoking on_message.
# The DiscordChannel skeleton is unimplemented in v0; FakeChannel doesn't
# enforce an allowlist (it's a pass-through). So at the agent-loop level
# we cannot assert "non-allowlisted user is rejected" without either
# (a) a real DiscordChannel impl, which is out of scope, or
# (b) duplicating the allowlist check in the agent loop, which would
#     violate FC-1's "authorization is the adapter's responsibility."
#
# This test pins the contract location: it documents the v0 expectation
# that the agent loop processes whatever the channel hands it, and
# defers TLC-1 to the channel adapter. Once DiscordChannel lands, an
# integration test under tests/integration/test_discord_channel.py
# should re-test TLC-1 against the real adapter.


@pytest.mark.skip(
    reason=(
        "TLC-1 is enforced by the channel adapter (FC-1), not the agent loop. "
        "Cannot test against unimplemented DiscordChannel; FakeChannel does not "
        "enforce. Re-test once DiscordChannel is implemented."
    )
)
async def test_tlc1_non_allowlisted_user_rejected_at_channel() -> None:
    pass
