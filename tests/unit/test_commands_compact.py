"""CompactCommand (`/compact`) + SummarizeCommand (`/summarize`) — v15 FC-29.

TLC-25 retires the auto-compactor; `/compact` (and its `/summarize`
alias) become MANUAL FORCED ROTATION: the command calls
`rotator.rotate(conv, reason="forced")` and reports the
`RotationResult` back to the user:

- success → a reply with no "skipped" wording; the conversation's
  non-compacted messages are collapsed into exactly one handoff summary
  and the claude CLI session id is cleared (fresh session next turn).
- skipped (nothing_to_rotate / cheap_tier_unavailable / …) → an
  informational reply naming the skip.

Out-of-thread invocation returns an informational message.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ember.commands.base import CommandContext
from ember.commands.registry import CommandRegistry
from ember.config import Settings
from ember.context.rotation import SessionRotator
from ember.providers.routing import ProviderRouter
from ember.store.repo import Store
from tests.fakes.fake_provider import FakeProvider


def _make_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")
    monkeypatch.setenv("DISCORD_CONVERSATIONS_CHANNEL_ID", "2")
    monkeypatch.setenv("DISCORD_SCHEDULER_CHANNEL_ID", "3")
    return Settings()  # type: ignore[call-arg]


def _named_provider(name: str) -> FakeProvider:
    fp = FakeProvider()
    fp.name = name
    return fp


@pytest.fixture
async def wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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
    args: dict | None = None,
) -> CommandContext:
    return CommandContext(
        conversation_key=conversation_key,
        user_id="1000",
        store=store,
        settings=settings,
        registry=registry,
        args=args or {},
    )


def _make_router_with_cheap(*, with_cheap: bool = True) -> tuple[ProviderRouter, FakeProvider]:
    primary = _named_provider("claude_cli")
    cheap = _named_provider("cheap")
    providers = {"claude_cli": primary, "cheap": cheap}
    router = ProviderRouter(
        providers,
        cheap_tier_route="cheap" if with_cheap else None,
    )
    return router, cheap


def _make_rotator(store: Store, router: ProviderRouter) -> SessionRotator:
    return SessionRotator(store=store, router=router)


async def _populate(store: Store, *, n: int = 6, session: str | None = "sess-1") -> int:
    """Add a conversation with `n` messages and an active CLI session."""
    conv = await store.get_or_create_conversation("discord", "thread-1")
    for i in range(n):
        await store.append_message(conv.id, "user" if i % 2 == 0 else "assistant", f"m{i}")
    if session is not None:
        await store.update_claude_cli_session(conv.id, session)
    return conv.id


# --- /compact static surface -----------------------------------------------


class TestCompactStaticSurface:
    def test_name(self) -> None:
        from ember.commands.compact import CompactCommand

        assert CompactCommand.name == "compact"

    def test_description_present(self) -> None:
        from ember.commands.compact import CompactCommand

        assert isinstance(CompactCommand.description, str)
        assert CompactCommand.description.strip() != ""

    def test_no_options(self) -> None:
        from ember.commands.compact import CompactCommand

        assert CompactCommand.options == []


# --- /summarize static surface ---------------------------------------------


class TestSummarizeStaticSurface:
    def test_name(self) -> None:
        from ember.commands.compact import SummarizeCommand

        assert SummarizeCommand.name == "summarize"

    def test_description_present(self) -> None:
        from ember.commands.compact import SummarizeCommand

        assert isinstance(SummarizeCommand.description, str)
        assert SummarizeCommand.description.strip() != ""

    def test_no_options(self) -> None:
        from ember.commands.compact import SummarizeCommand

        assert SummarizeCommand.options == []


# --- Behavior ---------------------------------------------------------------


class TestOutOfThread:
    async def test_compact_no_thread_returns_message(self, wired) -> None:
        from ember.commands.compact import CompactCommand

        store, settings, registry = wired
        router, _ = _make_router_with_cheap()
        cmd = CompactCommand(rotator=_make_rotator(store, router))
        out = await cmd.run(_ctx(store, settings, registry, conversation_key=None))
        assert isinstance(out, str)
        lowered = out.lower()
        assert "conversation" in lowered or "thread" in lowered or "must" in lowered

    async def test_summarize_no_thread_returns_message(self, wired) -> None:
        from ember.commands.compact import SummarizeCommand

        store, settings, registry = wired
        router, _ = _make_router_with_cheap()
        cmd = SummarizeCommand(rotator=_make_rotator(store, router))
        out = await cmd.run(_ctx(store, settings, registry, conversation_key=None))
        assert isinstance(out, str)
        lowered = out.lower()
        assert "conversation" in lowered or "thread" in lowered or "must" in lowered


class TestForcedRotation:
    async def test_compact_forces_a_rotation(self, wired) -> None:
        """`/compact` rotates regardless of thresholds: all non-compacted
        messages collapse into one handoff summary, the session id is
        cleared, and the reply is not a skip."""
        from ember.commands.compact import CompactCommand

        store, settings, registry = wired
        conv_id = await _populate(store, n=6, session="sess-1")

        router, cheap = _make_router_with_cheap()
        cheap.queue_text("FORCED-HANDOFF")

        cmd = CompactCommand(rotator=_make_rotator(store, router))
        out = await cmd.run(_ctx(store, settings, registry))

        # Cheap-tier was actually called.
        assert len(cheap.calls) == 1
        # Every message is now compacted; exactly one handoff row.
        active = await store.recent_messages(conv_id, limit=100)
        assert active == []
        summaries = await store.list_summaries(conv_id)
        assert len(summaries) == 1
        assert summaries[0].summary_text == "FORCED-HANDOFF"
        # Fresh session next turn (UoW-28 step 5).
        conv = await store.get_conversation(conv_id)
        assert conv is not None
        assert conv.claude_cli_session_id is None
        # Reply is a success, not a skip.
        assert isinstance(out, str)
        assert out != ""
        assert "skipped" not in out.lower()

    async def test_summarize_is_alias(self, wired) -> None:
        """`/summarize` exercises the same forced-rotation machinery."""
        from ember.commands.compact import SummarizeCommand

        store, settings, registry = wired
        conv_id = await _populate(store, n=6)

        router, cheap = _make_router_with_cheap()
        cheap.queue_text("ALIAS-HANDOFF")

        cmd = SummarizeCommand(rotator=_make_rotator(store, router))
        out = await cmd.run(_ctx(store, settings, registry))

        assert len(cheap.calls) == 1
        summaries = await store.list_summaries(conv_id)
        assert len(summaries) == 1
        assert summaries[0].summary_text == "ALIAS-HANDOFF"
        assert "skipped" not in out.lower()

    async def test_forced_rotation_replaces_prior_summary(self, wired) -> None:
        """A second `/compact` after new messages supersedes the first
        handoff — never accumulates summary rows (TLC-25)."""
        from ember.commands.compact import CompactCommand

        store, settings, registry = wired
        conv_id = await _populate(store, n=4)

        router, cheap = _make_router_with_cheap()
        cheap.queue_text("HANDOFF-ONE")
        cheap.queue_text("HANDOFF-TWO")

        cmd = CompactCommand(rotator=_make_rotator(store, router))
        await cmd.run(_ctx(store, settings, registry))

        await store.append_message(conv_id, "user", "new question")
        await store.append_message(conv_id, "assistant", "new answer")
        await cmd.run(_ctx(store, settings, registry))

        summaries = await store.list_summaries(conv_id)
        assert len(summaries) == 1
        assert summaries[0].summary_text == "HANDOFF-TWO"


class TestCheapTierUnavailable:
    async def test_returns_skipped_message_when_no_cheap_tier(self, wired) -> None:
        from ember.commands.compact import CompactCommand

        store, settings, registry = wired
        conv_id = await _populate(store, n=6)

        router, _ = _make_router_with_cheap(with_cheap=False)
        cmd = CompactCommand(rotator=_make_rotator(store, router))
        out = await cmd.run(_ctx(store, settings, registry))

        # Skipped indicator.
        lowered = out.lower()
        assert "skipped" in lowered or "ℹ" in out or "unavailable" in lowered
        # Zero writes: history untouched, no summary rows.
        assert len(await store.recent_messages(conv_id, limit=100)) == 6
        assert await store.list_summaries(conv_id) == []


class TestNothingToRotate:
    async def test_empty_conversation_skips(self, wired) -> None:
        """No messages → `nothing_to_rotate`, reported, no crash."""
        from ember.commands.compact import CompactCommand

        store, settings, registry = wired
        await store.get_or_create_conversation("discord", "thread-1")

        router, cheap = _make_router_with_cheap()
        cmd = CompactCommand(rotator=_make_rotator(store, router))
        out = await cmd.run(_ctx(store, settings, registry))

        assert cheap.calls == []
        assert isinstance(out, str)
        assert out != ""
        lowered = out.lower()
        assert "skipped" in lowered or "nothing" in lowered or "ℹ" in out

    async def test_single_message_skips(self, wired) -> None:
        """Forced rotation needs ≥2 non-compacted messages (UoW-28)."""
        from ember.commands.compact import CompactCommand

        store, settings, registry = wired
        conv = await store.get_or_create_conversation("discord", "thread-1")
        await store.append_message(conv.id, "user", "just one")

        router, cheap = _make_router_with_cheap()
        cmd = CompactCommand(rotator=_make_rotator(store, router))
        out = await cmd.run(_ctx(store, settings, registry))

        assert cheap.calls == []
        lowered = out.lower()
        assert "skipped" in lowered or "nothing" in lowered or "ℹ" in out
        assert await store.list_summaries(conv.id) == []
