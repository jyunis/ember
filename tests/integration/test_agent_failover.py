"""Agent rate-limit failover (UoW-15, TLC-7).

Per UoW-15:
- When the conversation's `auto_failover_on_rate_limit` flag is on AND
  the configured `deepseek_api` failover provider is in the router,
  a primary `RateLimitError` triggers a transparent retry on
  `deepseek_api`. The retry's assistant message is persisted with
  `failover=True`.
- When the flag is OFF, the primary `RateLimitError` is surfaced as a
  status message ("rate limited"); no fallback attempted; the user
  message stays in history but no assistant message is persisted
  (TLC-7 / UoW-1 atomicity).
- When the failover provider isn't in the router, the same
  user-visible "rate limited" message lands regardless of the flag.

The agent loop body for v2 is implemented; we treat it as production
code and assert the observable contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ember.agent import Agent
from ember.channels.base import ChannelMessage
from ember.config import Settings
from ember.errors import AuthError, RateLimitError
from ember.providers.base import CompletionResult, Usage
from ember.providers.routing import ProviderRouter
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


def _named_provider(name: str) -> FakeProvider:
    fp = FakeProvider()
    # FakeProvider's `name` is a regular class attribute. Override per-instance
    # so we can run two distinct providers behind the router.
    fp.name = name
    return fp


def _msg(text: str = "hi", *, key: str = "thread-1") -> ChannelMessage:
    return ChannelMessage(
        channel="discord",
        conversation_key=key,
        user_id="1000",
        text=text,
        message_id="m1",
    )


async def _seed_conversation(
    store: Store,
    *,
    key: str = "thread-1",
    provider_route: str = "primary",
    failover_on: bool = False,
) -> None:
    """Create a conversation row with the desired route + failover flag."""
    conv = await store.get_or_create_conversation("discord", key)
    await store.update_provider_route(conv.id, provider_route)
    if failover_on:
        await store.update_failover_flag(conv.id, True)


@pytest.fixture
async def store_fixture(tmp_path: Path):
    store = Store(tmp_path / "conversations.db")
    await store.open()
    try:
        yield store
    finally:
        await store.close()


# --- UoW-15: failover ON, both providers present -------------------------


class TestFailoverHappyPath:
    async def test_failover_returns_deepseek_api_text_to_user(
        self,
        store_fixture: Store,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        settings = _make_settings(monkeypatch, tmp_path)
        primary = _named_provider("primary")
        api = _named_provider("deepseek_api")
        primary.queue_error(RateLimitError("subscription rate limited"))
        api.queue_response(
            CompletionResult(
                text="API took over",
                usage=Usage(input_tokens=10, output_tokens=20, cost_usd=0.01),
            )
        )
        router = ProviderRouter({"primary": primary, "deepseek_api": api})
        channel = FakeChannel()
        agent = Agent(store=store_fixture, channel=channel, settings=settings, router=router)
        await channel.start(agent.handle_turn)

        await _seed_conversation(store_fixture, failover_on=True)
        await channel.inject(_msg("hi"))

        finals = [s for s in channel.sent if s.kind == "final"]
        assert len(finals) == 1
        assert finals[0].content == "API took over"

        # User is told a fallback happened (status notice), naming the route.
        notices = [s for s in channel.sent if s.kind == "status" and "falling back" in s.content]
        assert len(notices) == 1
        assert "deepseek_api" in notices[0].content

    async def test_failover_persists_assistant_with_failover_true(
        self,
        store_fixture: Store,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        settings = _make_settings(monkeypatch, tmp_path)
        primary = _named_provider("primary")
        api = _named_provider("deepseek_api")
        primary.queue_error(RateLimitError("subscription rate limited"))
        api.queue_response(
            CompletionResult(
                text="API took over",
                usage=Usage(input_tokens=10, output_tokens=20, cost_usd=0.01),
            )
        )
        router = ProviderRouter({"primary": primary, "deepseek_api": api})
        channel = FakeChannel()
        agent = Agent(store=store_fixture, channel=channel, settings=settings, router=router)
        await channel.start(agent.handle_turn)

        await _seed_conversation(store_fixture, failover_on=True)
        await channel.inject(_msg("hi"))

        conv = await store_fixture.get_or_create_conversation("discord", "thread-1")
        msgs = await store_fixture.recent_messages(conv.id)

        user_msg = next(m for m in msgs if m.role == "user")
        assistant_msg = next(m for m in msgs if m.role == "assistant")

        assert assistant_msg.content == "API took over"
        assert assistant_msg.failover is True
        # The assistant's recorded route should be the failover provider.
        assert assistant_msg.provider_route is not None
        assert "deepseek_api" in assistant_msg.provider_route

        # User message persists regardless of which provider answered;
        # `failover` only marks the assistant turn that was rerouted.
        assert user_msg.failover is False


# --- UoW-15: failover OFF — primary rate-limit surfaces, no fallback ----


class TestFailoverOffSurface:
    async def test_user_sees_rate_limited_status_message(
        self,
        store_fixture: Store,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        settings = _make_settings(monkeypatch, tmp_path)
        primary = _named_provider("primary")
        api = _named_provider("deepseek_api")
        primary.queue_error(RateLimitError("subscription rate limited"))
        # API is in the router but should NOT be called.
        api.queue_text("should not be sent")
        router = ProviderRouter({"primary": primary, "deepseek_api": api})
        channel = FakeChannel()
        agent = Agent(store=store_fixture, channel=channel, settings=settings, router=router)
        await channel.start(agent.handle_turn)

        await _seed_conversation(store_fixture, failover_on=False)
        await channel.inject(_msg("hi"))

        # API was NOT consulted.
        assert len(api.calls) == 0

        # User saw something containing "rate limited".
        all_content = "\n".join(s.content for s in channel.sent)
        assert "rate limited" in all_content.lower()
        # Nothing was sent as the canonical final reply.
        finals = [s for s in channel.sent if s.kind == "final"]
        assert finals == []

    async def test_no_assistant_message_persisted(
        self,
        store_fixture: Store,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # TLC-7 / UoW-1 atomicity: failed turn → user message stays;
        # no assistant message is committed.
        settings = _make_settings(monkeypatch, tmp_path)
        primary = _named_provider("primary")
        primary.queue_error(RateLimitError("subscription rate limited"))
        router = ProviderRouter({"primary": primary})
        channel = FakeChannel()
        agent = Agent(store=store_fixture, channel=channel, settings=settings, router=router)
        await channel.start(agent.handle_turn)

        await _seed_conversation(store_fixture, failover_on=False)
        await channel.inject(_msg("hi"))

        conv = await store_fixture.get_or_create_conversation("discord", "thread-1")
        msgs = await store_fixture.recent_messages(conv.id)
        roles = [m.role for m in msgs]
        assert "user" in roles
        assert "assistant" not in roles


# --- UoW-15: failover provider missing — graceful fallback to status -----


class TestFailoverMissingProvider:
    async def test_no_failover_attempt_when_provider_missing_even_if_flag_on(
        self,
        store_fixture: Store,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Router has only `primary` — no `deepseek_api`. Even though
        # the flag is on, there's nothing to fail over to.
        settings = _make_settings(monkeypatch, tmp_path)
        primary = _named_provider("primary")
        primary.queue_error(RateLimitError("subscription rate limited"))
        router = ProviderRouter({"primary": primary})
        channel = FakeChannel()
        agent = Agent(store=store_fixture, channel=channel, settings=settings, router=router)
        await channel.start(agent.handle_turn)

        await _seed_conversation(store_fixture, failover_on=True)
        await channel.inject(_msg("hi"))

        # User saw the rate-limited status; no assistant message persisted.
        all_content = "\n".join(s.content for s in channel.sent)
        assert "rate limited" in all_content.lower()
        finals = [s for s in channel.sent if s.kind == "final"]
        assert finals == []

        conv = await store_fixture.get_or_create_conversation("discord", "thread-1")
        msgs = await store_fixture.recent_messages(conv.id)
        assert "assistant" not in [m.role for m in msgs]


# --- Auth-down failover: unconditional, with a proactive re-auth alert ----


def _ok(text: str) -> CompletionResult:
    return CompletionResult(
        text=text,
        usage=Usage(input_tokens=1, output_tokens=1, cost_usd=0.0),
    )


class TestAuthFailover:
    async def test_claude_auth_down_answers_on_deepseek_and_alerts(
        self,
        store_fixture: Store,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        settings = _make_settings(monkeypatch, tmp_path)
        primary = _named_provider("claude_cli")
        api = _named_provider("deepseek_api")
        primary.queue_error(AuthError("Failed to authenticate. 401"))
        api.queue_response(_ok("deepseek answered"))
        router = ProviderRouter({"claude_cli": primary, "deepseek_api": api})
        channel = FakeChannel()
        agent = Agent(store=store_fixture, channel=channel, settings=settings, router=router)
        await channel.start(agent.handle_turn)

        # failover flag OFF — auth failover is unconditional, not gated.
        await _seed_conversation(store_fixture, provider_route="claude_cli", failover_on=False)
        await channel.inject(_msg("hi"))

        # deepseek carried the turn.
        finals = [s for s in channel.sent if s.kind == "final"]
        assert len(finals) == 1
        assert finals[0].content == "deepseek answered"

        # In-thread status told the user auth is down + names the fallback.
        statuses = [s for s in channel.sent if s.kind == "status"]
        assert any("auth is down" in s.content and "deepseek_api" in s.content for s in statuses)

        # Assistant row persisted as a failover turn.
        conv = await store_fixture.get_or_create_conversation("discord", "thread-1")
        msgs = await store_fixture.recent_messages(conv.id)
        assistant = next(m for m in msgs if m.role == "assistant")
        assert assistant.failover is True
        assert "deepseek_api" in (assistant.provider_route or "")

        # Proactive re-auth alert posted to the scheduler channel, with the
        # command on its own bare-text message (mobile-copyable).
        cmd_msgs = [m for m in channel.scheduler_notifications if "setup-token" in m]
        assert len(cmd_msgs) == 1
        # The command message is JUST the command — no prose, no code fence.
        assert (
            cmd_msgs[0] == "cd /srv/ember && docker compose exec agent claude setup-token"
        )

    async def test_auth_alert_debounced_across_turns(
        self,
        store_fixture: Store,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        settings = _make_settings(monkeypatch, tmp_path)
        primary = _named_provider("claude_cli")
        api = _named_provider("deepseek_api")
        # Two consecutive auth-down turns.
        primary.queue_error(AuthError("401"))
        primary.queue_error(AuthError("401"))
        api.queue_response(_ok("a"))
        api.queue_response(_ok("b"))
        router = ProviderRouter({"claude_cli": primary, "deepseek_api": api})
        channel = FakeChannel()
        agent = Agent(store=store_fixture, channel=channel, settings=settings, router=router)
        await channel.start(agent.handle_turn)

        await _seed_conversation(store_fixture, provider_route="claude_cli")
        await channel.inject(_msg("one"))
        await channel.inject(_msg("two", key="thread-1"))

        # Both turns answered, but only ONE re-auth command was posted.
        finals = [s for s in channel.sent if s.kind == "final"]
        assert len(finals) == 2
        cmd_msgs = [m for m in channel.scheduler_notifications if "setup-token" in m]
        assert len(cmd_msgs) == 1

    async def test_auth_alert_clears_on_recovery_and_realerts(
        self,
        store_fixture: Store,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        settings = _make_settings(monkeypatch, tmp_path)
        primary = _named_provider("claude_cli")
        api = _named_provider("deepseek_api")
        # down → recovered → down again.
        primary.queue_error(AuthError("401"))
        primary.queue_response(_ok("claude back"))
        primary.queue_error(AuthError("401"))
        api.queue_response(_ok("fallback 1"))
        api.queue_response(_ok("fallback 2"))
        router = ProviderRouter({"claude_cli": primary, "deepseek_api": api})
        channel = FakeChannel()
        agent = Agent(store=store_fixture, channel=channel, settings=settings, router=router)
        await channel.start(agent.handle_turn)

        await _seed_conversation(store_fixture, provider_route="claude_cli")
        await channel.inject(_msg("t1"))  # auth down → alert #1
        await channel.inject(_msg("t2"))  # claude healthy → flag cleared
        await channel.inject(_msg("t3"))  # auth down again → alert #2

        cmd_msgs = [m for m in channel.scheduler_notifications if "setup-token" in m]
        assert len(cmd_msgs) == 2

    async def test_non_claude_auth_error_does_not_post_reauth_alert(
        self,
        store_fixture: Store,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A non-claude primary failing auth still fails over, but the
        # claude-specific re-auth alert must NOT fire.
        settings = _make_settings(monkeypatch, tmp_path)
        primary = _named_provider("anthropic_api")
        api = _named_provider("deepseek_api")
        primary.queue_error(AuthError("bad key"))
        api.queue_response(_ok("deepseek answered"))
        router = ProviderRouter({"anthropic_api": primary, "deepseek_api": api})
        channel = FakeChannel()
        agent = Agent(store=store_fixture, channel=channel, settings=settings, router=router)
        await channel.start(agent.handle_turn)

        await _seed_conversation(store_fixture, provider_route="anthropic_api")
        await channel.inject(_msg("hi"))

        finals = [s for s in channel.sent if s.kind == "final"]
        assert len(finals) == 1
        assert channel.scheduler_notifications == []
