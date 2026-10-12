"""ContextCommand (`/context`) — FC-10, TLC-5.

Per the architecture doc § Token tracker + auto-compactor:
- `/context` shows tokens used, % of window, $ cost, subscription
  turn count, oldest message age.
- Counters live in `messages` rows; `/context` queries are SQL
  aggregations (FC-10).

TLC-5 — cost transparency: every Provider completion records
`(input_tokens, output_tokens, cached_tokens, cost_usd or None,
subscription bool)`. `/context` always reflects the current state and
must NEVER combine subscription + metered into a single number — they
have to be reported separately.

These tests wire a real `Store` (file-backed on tmp_path), populate a
synthetic conversation, and assert the rendered `/context` output
contains the expected aggregates.

We don't read implementation files — these tests assert the contract
via the interface in `src/ember/commands/context.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ember.commands.base import CommandContext
from ember.commands.context import ContextCommand
from ember.commands.registry import CommandRegistry
from ember.config import Settings
from ember.providers.base import Usage
from ember.store.repo import Store


# --- Fixtures -------------------------------------------------------------


def _make_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")
    monkeypatch.setenv("DISCORD_CONVERSATIONS_CHANNEL_ID", "2")
    monkeypatch.setenv("DISCORD_SCHEDULER_CHANNEL_ID", "3")
    return Settings()  # type: ignore[call-arg]


@pytest.fixture
async def wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Real Store + Settings + empty registry. Tests populate a
    conversation themselves so they control the input data."""
    settings = _make_settings(monkeypatch, tmp_path)
    store = Store(tmp_path / "conversations.db")
    await store.open()
    registry = CommandRegistry()
    try:
        yield store, settings, registry
    finally:
        await store.close()


def _ctx(
    store: Store,
    settings: Settings,
    registry: CommandRegistry,
    *,
    conversation_key: str | None = "thread-1",
) -> CommandContext:
    return CommandContext(
        conversation_key=conversation_key,
        user_id="1000",
        store=store,
        settings=settings,
        registry=registry,
    )


async def _populate_mixed_conversation(store: Store, conversation_key: str) -> None:
    """Synthetic conversation with two complete turns: one subscription,
    one metered. Used by the bulk of the /context tests so they share a
    known-shape input.

    Turn 1 (subscription):  user → assistant w/ Usage(10, 20, sub=True)
    Turn 2 (metered):       user → assistant w/ Usage(15, 25, cost=0.42, sub=False)

    Total input tokens:  25
    Total output tokens: 45
    Total tokens:        70
    Subscription turns:  1
    Metered $:           0.42
    """
    conv = await store.get_or_create_conversation("discord", conversation_key)

    await store.append_message(conv.id, "user", "first user")
    await store.append_message(
        conv.id,
        "assistant",
        "first assistant",
        usage=Usage(input_tokens=10, output_tokens=20, subscription=True),
    )
    await store.append_message(conv.id, "user", "second user")
    await store.append_message(
        conv.id,
        "assistant",
        "second assistant",
        usage=Usage(
            input_tokens=15,
            output_tokens=25,
            cost_usd=0.42,
            subscription=False,
        ),
    )


# --- Reports total tokens, subscription/metered split --------------------


class TestContextOutput:
    async def test_returns_string(self, wired) -> None:
        store, settings, registry = wired
        await _populate_mixed_conversation(store, "thread-1")
        cmd = ContextCommand()
        out = await cmd.run(_ctx(store, settings, registry))
        assert isinstance(out, str)
        assert out != ""

    async def test_reports_total_tokens(self, wired) -> None:
        # Lifetime accounting = input and output sums, reported
        # separately (v16 closeout: the old `used / token_budget`
        # percentage was retired — a cumulative billing sum divided by
        # the dead compactor's budget column is not a context gauge,
        # TLC-24). With our fixture: in 10+15=25, out 20+25=45.
        store, settings, registry = wired
        await _populate_mixed_conversation(store, "thread-1")
        cmd = ContextCommand()
        out = await cmd.run(_ctx(store, settings, registry))
        assert "in 25" in out
        assert "out 45" in out
        # The budget percentage is gone for good.
        assert "%" not in out
        assert "150,000" not in out

    async def test_reports_subscription_turn_count(self, wired) -> None:
        # TLC-5: subscription turns are reported as a count, separate
        # from $ cost. Our fixture has exactly 1 subscription turn.
        store, settings, registry = wired
        await _populate_mixed_conversation(store, "thread-1")
        cmd = ContextCommand()
        out = await cmd.run(_ctx(store, settings, registry))
        # The output should mention the count "1" alongside something
        # identifying it as subscription turns.
        assert "1" in out
        # Some indication that subscription is being reported separately.
        assert "subscription" in out.lower()

    async def test_reports_metered_cost(self, wired) -> None:
        # TLC-5: metered cost reported as $; our fixture has exactly $0.42.
        store, settings, registry = wired
        await _populate_mixed_conversation(store, "thread-1")
        cmd = ContextCommand()
        out = await cmd.run(_ctx(store, settings, registry))
        # The exact formatting may be "$0.42" or "0.42" — assert the
        # numeric portion appears.
        assert "0.42" in out

    async def test_does_not_combine_subscription_and_metered(self, wired) -> None:
        # TLC-5 invariant: never collapse subscription turns and metered
        # cost into one number. The output must show them separately.
        # Indirect check: the rendered output should mention both
        # "subscription" (or similar) and a $ amount, not just a single
        # combined cost number.
        store, settings, registry = wired
        await _populate_mixed_conversation(store, "thread-1")
        cmd = ContextCommand()
        out = await cmd.run(_ctx(store, settings, registry))
        lowered = out.lower()
        # Both reports must be present for this contract to hold.
        assert "subscription" in lowered
        assert "0.42" in out

    async def test_includes_message_count_or_age_indicator(self, wired) -> None:
        # Architecture: /context shows oldest message age and/or message
        # count. Our fixture has 4 messages; assert some indicator appears.
        # We can't pin exact wording, so accept either the count "4" or
        # any occurrence of the words "message" / "age" / "oldest".
        store, settings, registry = wired
        await _populate_mixed_conversation(store, "thread-1")
        cmd = ContextCommand()
        out = await cmd.run(_ctx(store, settings, registry))
        lowered = out.lower()
        has_count = "4" in out
        has_age = "age" in lowered or "oldest" in lowered or "message" in lowered
        assert has_count or has_age


# --- Outside-conversation invocation -------------------------------------


class TestContextOutsideConversation:
    async def test_no_conversation_key_returns_clear_message(self, wired) -> None:
        # /context invoked outside a conversation thread. The command
        # decides whether that's OK — for /context, the architecture says
        # it's observational on the current conversation, so it must
        # surface a clear "must be in a conversation" message.
        store, settings, registry = wired
        cmd = ContextCommand()
        # Must NOT raise.
        out = await cmd.run(_ctx(store, settings, registry, conversation_key=None))
        assert isinstance(out, str)
        assert out != ""
        # Some indication that this is the wrong place to invoke it.
        lowered = out.lower()
        assert "conversation" in lowered or "thread" in lowered or "not" in lowered
