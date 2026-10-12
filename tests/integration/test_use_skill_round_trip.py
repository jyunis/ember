"""End-to-end `use_skill` dispatch (FC-17, TLC-16, TLC-17).

A full Agent in router mode (no active skill) processes a user
message; the parent provider returns a `use_skill` tool call; the
sub-agent's fake provider returns a final assistant text; the user-
facing reply contains that text.

What we pin:
  - Router mode (active_skill IS NULL) exposes `use_skill` to the
    parent provider's `tools` list.
  - Pinned mode (active_skill = "demo") does NOT expose `use_skill`
    (TLC-17).
  - Round-trip reply: parent's final assistant text reaches the
    channel's `final` message after a sub-agent completes.
  - Status forwarding: status messages from the sub-agent reach the
    parent's channel with a `[demo]` prefix (FC-17 status forwarding).
  - No-nesting (TLC-16 §4): a sub-agent cannot itself call
    `use_skill` — the dispatch is refused (or absent from the
    sub-agent's registry).

Tester barrier: we read the Agent class's `__init__` signature only
(see report); we do not read the body. The `use_skill` tool itself
isn't read at all.

Spec ambiguities flagged in report:
  - Whether `use_skill` is registered in the global tool registry by
    the implementer (in `main.py`) or whether the agent injects it
    itself when in router mode. We assume the user pre-registers it
    in the global Registry passed to Agent. The tests register it
    explicitly.
  - The exact signal the agent uses to decide router-vs-pinned: the
    spec says "router mode = active_skill IS NULL". We test both
    settings and assert tool exposure.
  - Status-forwarding prefix format. Spec says "[<skill>] prefix".
    We accept any sub-agent status whose content contains "demo" —
    if the implementer picks a different prefix shape, this surfaces
    the spec gap.
  - Round-trip dispatch path: the parent agent might dispatch the
    `use_skill` tool call via `CompletionResult.tool_calls` OR via
    a stream-event-driven dispatch path. Either way, the integration
    point is observable: the `use_skill` tool is invoked and the
    sub-agent's text bubbles up. We don't pin the path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ember.agent import Agent
from ember.channels.base import ChannelMessage
from ember.config import Settings
from ember.identity.store import IdentityStore
from ember.providers.base import CompletionResult, ToolCall, Usage
from ember.providers.routing import ProviderRouter
from ember.skills.base import DeclarativeSkill
from ember.skills.registry import InMemorySkillRegistry
from ember.store.repo import Store
from ember.tools.base import Tool, ToolResult
from ember.tools.registry import Registry
from tests.fakes.fake_channel import FakeChannel
from tests.fakes.fake_provider import FakeProvider


# --- Settings / msg helpers ---------------------------------------------------


def _make_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")
    monkeypatch.setenv("DISCORD_CONVERSATIONS_CHANNEL_ID", "2")
    monkeypatch.setenv("DISCORD_SCHEDULER_CHANNEL_ID", "3")
    return Settings()  # type: ignore[call-arg]


def _msg(text: str = "hi", *, key: str = "thread-1", mid: str = "m1") -> ChannelMessage:
    return ChannelMessage(
        channel="discord",
        conversation_key=key,
        user_id="1000",
        text=text,
        message_id=mid,
    )


# --- Stub tools the demo skill references -------------------------------------


class _StubTool:
    """A trivial Tool the skill could list. We don't need it to do
    anything — the sub-agent's provider returns final text directly."""

    def __init__(self, name: str = "rec") -> None:
        self.name = name
        self.description = f"stub {name}"
        self.input_schema: dict[str, Any] = {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }
        self.requires_confirmation: bool = False

    async def call(self, args: dict[str, Any]) -> ToolResult:
        return ToolResult(output={}, is_error=False)


# --- UseSkill registration helper ---------------------------------------------


def _register_use_skill_or_skip(
    *,
    tool_registry: Registry,
    skill_registry: InMemorySkillRegistry,
    subagent_router: ProviderRouter,
    channel: FakeChannel,
    store: Store,
    parent_conversation_id: int = 1,
    parent_message_id: int = 1,
) -> Tool:
    """Construct + register the `use_skill` tool. If the implementer
    hasn't shipped the module yet, skip the test rather than collect-
    error the whole file. The unit-level tests still drive the
    primary baseline failure.
    """
    pytest.importorskip("ember.tools.use_skill")
    from ember.tools.use_skill import UseSkillTool  # type: ignore[import-not-found]

    tool = UseSkillTool(
        skill_registry=skill_registry,
        tool_registry=tool_registry,
        provider_router=subagent_router,
        channel=channel,
        store=store,
        conversation_id=parent_conversation_id,
        parent_message_id=parent_message_id,
        max_turns=8,
        max_tool_calls=16,
        timeout_s=5,
    )
    tool_registry.register(tool)
    return tool


# --- TLC-17: tool exposure based on conversation mode -------------------------


class TestRouterModeExposesUseSkill:
    async def test_router_mode_provider_call_includes_use_skill_tool(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        settings = _make_settings(monkeypatch, tmp_path)
        store = Store(tmp_path / "conversations.db")
        await store.open()
        try:
            identity = IdentityStore(tmp_path / "identity")
            await identity.ensure_initialized()

            skill_registry = InMemorySkillRegistry()
            skill_registry.replace(
                DeclarativeSkill(
                    name="demo",
                    description="d",
                    tools=("rec",),
                    system_prompt="DEMO_SUB_PROMPT",
                )
            )
            tool_registry = Registry()
            tool_registry.register(_StubTool("rec"))

            subagent_provider = FakeProvider()
            subagent_provider.name = "claude_cli"
            subagent_router = ProviderRouter({"claude_cli": subagent_provider})

            channel = FakeChannel()

            # Ensure conversation exists so we can pin it as parent.
            conv = await store.get_or_create_conversation("discord", "thread-1")

            _register_use_skill_or_skip(
                tool_registry=tool_registry,
                skill_registry=skill_registry,
                subagent_router=subagent_router,
                channel=channel,
                store=store,
                parent_conversation_id=conv.id,
            )

            # Parent provider — returns plain text (we don't go round-
            # trip here; only verify exposure).
            parent_provider = FakeProvider()
            parent_provider.name = "claude_cli"
            parent_provider.queue_text("ok")

            agent = Agent(
                store=store,
                provider=parent_provider,
                channel=channel,
                settings=settings,
                identity=identity,
                skill_registry=skill_registry,
                tool_registry=tool_registry,
            )
            await channel.start(agent.handle_turn)

            await channel.inject(_msg("hi"))

            # The provider was called once. With no active_skill
            # (router mode), the agent must advertise `use_skill` in
            # the parent's tools list.
            assert len(parent_provider.calls) == 1
            advertised = parent_provider.calls[0].tools
            # Implementer may pass tools=None when registry empty, or
            # build a list including use_skill — we want the latter.
            # If tools=None, we treat that as a spec-gap failure.
            assert advertised is not None, (
                "expected router mode to advertise tools (incl. use_skill); got None"
            )
            names = [t.name for t in advertised]
            assert "use_skill" in names
        finally:
            await store.close()


class TestPinnedModeHidesUseSkill:
    async def test_pinned_mode_provider_call_excludes_use_skill(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        settings = _make_settings(monkeypatch, tmp_path)
        store = Store(tmp_path / "conversations.db")
        await store.open()
        try:
            identity = IdentityStore(tmp_path / "identity")
            await identity.ensure_initialized()

            skill_registry = InMemorySkillRegistry()
            skill_registry.replace(
                DeclarativeSkill(
                    name="demo",
                    description="d",
                    tools=("rec",),
                    system_prompt="DEMO_SUB_PROMPT",
                )
            )
            tool_registry = Registry()
            tool_registry.register(_StubTool("rec"))

            subagent_provider = FakeProvider()
            subagent_provider.name = "claude_cli"
            subagent_router = ProviderRouter({"claude_cli": subagent_provider})

            channel = FakeChannel()
            conv = await store.get_or_create_conversation("discord", "thread-1")
            _register_use_skill_or_skip(
                tool_registry=tool_registry,
                skill_registry=skill_registry,
                subagent_router=subagent_router,
                channel=channel,
                store=store,
                parent_conversation_id=conv.id,
            )

            # Pin demo skill on the conversation.
            await store._c.execute(  # type: ignore[attr-defined]
                "UPDATE conversations SET active_skill = ? WHERE id = ?",
                ("demo", conv.id),
            )
            await store._c.commit()  # type: ignore[attr-defined]

            parent_provider = FakeProvider()
            parent_provider.name = "claude_cli"
            parent_provider.queue_text("ok")

            agent = Agent(
                store=store,
                provider=parent_provider,
                channel=channel,
                settings=settings,
                identity=identity,
                skill_registry=skill_registry,
                tool_registry=tool_registry,
            )
            await channel.start(agent.handle_turn)

            await channel.inject(_msg("hi"))

            assert len(parent_provider.calls) == 1
            advertised = parent_provider.calls[0].tools
            # Pinned mode filters tools to the skill's allowlist
            # (`rec` only). `use_skill` MUST be absent.
            assert advertised is not None
            names = [t.name for t in advertised]
            assert "use_skill" not in names
            assert "rec" in names
        finally:
            await store.close()


# --- Round-trip dispatch ------------------------------------------------------


class TestRoundTrip:
    async def test_use_skill_call_drives_subagent_and_returns_text(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Parent provider returns a `use_skill` tool call → sub-agent
        runs → sub-agent returns final text → parent fed tool result
        → parent returns final text → user sees it."""
        settings = _make_settings(monkeypatch, tmp_path)
        store = Store(tmp_path / "conversations.db")
        await store.open()
        try:
            identity = IdentityStore(tmp_path / "identity")
            await identity.ensure_initialized()

            skill_registry = InMemorySkillRegistry()
            skill_registry.replace(
                DeclarativeSkill(
                    name="demo",
                    description="d",
                    tools=("rec",),
                    system_prompt="DEMO_SUB_PROMPT",
                )
            )
            tool_registry = Registry()
            tool_registry.register(_StubTool("rec"))

            subagent_provider = FakeProvider()
            subagent_provider.name = "claude_cli"
            subagent_router = ProviderRouter({"claude_cli": subagent_provider})

            channel = FakeChannel()
            conv = await store.get_or_create_conversation("discord", "thread-1")
            _register_use_skill_or_skip(
                tool_registry=tool_registry,
                skill_registry=skill_registry,
                subagent_router=subagent_router,
                channel=channel,
                store=store,
                parent_conversation_id=conv.id,
            )

            # Sub-agent's provider: returns final text immediately.
            subagent_provider.queue_text("SUBAGENT_FINAL_TEXT")

            # Parent provider:
            #   Turn 1: `use_skill(skill="demo", query="hello")`.
            #   Turn 2: final text wrapping the sub-agent's reply.
            parent_provider = FakeProvider()
            parent_provider.name = "claude_cli"
            parent_provider.queue_response(
                CompletionResult(
                    text="",
                    tool_calls=[
                        ToolCall(
                            id="tu_use_skill_1",
                            name="use_skill",
                            args={"skill": "demo", "query": "hello"},
                        )
                    ],
                    usage=Usage(input_tokens=1, output_tokens=1, subscription=True),
                )
            )
            parent_provider.queue_text("PARENT_WRAPPER: SUBAGENT_FINAL_TEXT")

            agent = Agent(
                store=store,
                provider=parent_provider,
                channel=channel,
                settings=settings,
                identity=identity,
                skill_registry=skill_registry,
                tool_registry=tool_registry,
            )
            await channel.start(agent.handle_turn)

            await channel.inject(_msg("hi"))

            # The user-facing `final` message contains the parent
            # wrapper text (which includes the sub-agent's reply).
            finals = [s for s in channel.sent if s.kind == "final"]
            assert len(finals) >= 1, "expected at least one `final` channel message"
            # We don't pin exact wording — the parent could echo or
            # reformat. We assert the sub-agent's text appears
            # somewhere in the channel-bound output.
            all_outbound = "\n".join(s.content for s in channel.sent)
            assert "SUBAGENT_FINAL_TEXT" in all_outbound

            # The sub-agent's provider was called.
            assert len(subagent_provider.calls) >= 1
            # And its system prompt was the skill's, not the parent's.
            sub_call = subagent_provider.calls[0]
            assert "DEMO_SUB_PROMPT" in sub_call.system
            # And the sub-agent's history is just the query.
            assert len(sub_call.messages) == 1
            assert sub_call.messages[0].content == "hello"
        finally:
            await store.close()


# --- Status forwarding (FC-17) ------------------------------------------------


class TestStatusForwarding:
    async def test_subagent_tool_call_status_carries_skill_prefix(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When the sub-agent dispatches a tool, its status message
        reaches the parent channel with a `[demo]` prefix (or some
        marker mentioning the skill). Pattern after
        test_agent_loop's stream-handler status assertion: stream
        events drive status messages; the implementer's sub-agent
        loop forwards them with a prefix."""
        settings = _make_settings(monkeypatch, tmp_path)
        store = Store(tmp_path / "conversations.db")
        await store.open()
        try:
            identity = IdentityStore(tmp_path / "identity")
            await identity.ensure_initialized()

            skill_registry = InMemorySkillRegistry()
            skill_registry.replace(
                DeclarativeSkill(
                    name="demo",
                    description="d",
                    tools=("rec",),
                    system_prompt="DEMO_SUB_PROMPT",
                )
            )
            tool_registry = Registry()
            tool_registry.register(_StubTool("rec"))

            subagent_provider = FakeProvider()
            subagent_provider.name = "claude_cli"
            subagent_router = ProviderRouter({"claude_cli": subagent_provider})

            channel = FakeChannel()
            conv = await store.get_or_create_conversation("discord", "thread-1")
            _register_use_skill_or_skip(
                tool_registry=tool_registry,
                skill_registry=skill_registry,
                subagent_router=subagent_router,
                channel=channel,
                store=store,
                parent_conversation_id=conv.id,
            )

            # Sub-agent emits a tool_use stream event, then a final
            # text response. The implementer's sub-agent loop should
            # forward the tool_use status with a [demo] prefix.
            subagent_provider.queue_text_with_events(
                "SUB_FINAL",
                events=[
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "tu_inner_1",
                                    "name": "rec",
                                    "input": {"text": "hi"},
                                }
                            ]
                        },
                    }
                ],
            )

            parent_provider = FakeProvider()
            parent_provider.name = "claude_cli"
            parent_provider.queue_response(
                CompletionResult(
                    text="",
                    tool_calls=[
                        ToolCall(
                            id="tu_use_skill_1",
                            name="use_skill",
                            args={"skill": "demo", "query": "hello"},
                        )
                    ],
                    usage=Usage(input_tokens=1, output_tokens=1, subscription=True),
                )
            )
            parent_provider.queue_text("ok")

            agent = Agent(
                store=store,
                provider=parent_provider,
                channel=channel,
                settings=settings,
                identity=identity,
                skill_registry=skill_registry,
                tool_registry=tool_registry,
            )
            await channel.start(agent.handle_turn)

            await channel.inject(_msg("hi"))

            statuses = [s for s in channel.sent if s.kind == "status"]
            # At least one status carries a sub-agent marker — either
            # the literal "[demo]" prefix or a string mentioning
            # `demo`.
            demo_statuses = [s for s in statuses if "demo" in s.content.lower()]
            assert demo_statuses, (
                "expected at least one status mentioning 'demo' from the "
                "sub-agent's tool dispatch; got: "
                f"{[s.content for s in statuses]}"
            )
        finally:
            await store.close()


# --- TLC-16 §4: no nesting at the system level --------------------------------


class TestNoNesting:
    async def test_subagent_cannot_invoke_use_skill(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The sub-agent attempting to call `use_skill` must NOT
        result in a recursive sub-agent run. Two acceptable paths:

          (a) The sub-agent's tool registry simply doesn't have
              `use_skill` (TLC-16 §4 strict filter), so the dispatch
              raises ToolValidationError or KeyError and the loop
              feeds an error back to the model.
          (b) The implementer registers a guard tool that returns the
              hardcoded "use_skill not available inside a skill
              invocation" tool error.

        We assert the *systemic* effect: the parent agent never
        receives more than one round-trip use_skill call (no recursion)
        AND the sub-agent's provider eventually returns a final text
        that closes the loop.
        """
        settings = _make_settings(monkeypatch, tmp_path)
        store = Store(tmp_path / "conversations.db")
        await store.open()
        try:
            identity = IdentityStore(tmp_path / "identity")
            await identity.ensure_initialized()

            skill_registry = InMemorySkillRegistry()
            skill_registry.replace(
                DeclarativeSkill(
                    name="demo",
                    description="d",
                    tools=("rec",),
                    system_prompt="DEMO_SUB_PROMPT",
                )
            )
            tool_registry = Registry()
            tool_registry.register(_StubTool("rec"))

            subagent_provider = FakeProvider()
            subagent_provider.name = "claude_cli"
            subagent_router = ProviderRouter({"claude_cli": subagent_provider})

            channel = FakeChannel()
            conv = await store.get_or_create_conversation("discord", "thread-1")
            _register_use_skill_or_skip(
                tool_registry=tool_registry,
                skill_registry=skill_registry,
                subagent_router=subagent_router,
                channel=channel,
                store=store,
                parent_conversation_id=conv.id,
            )

            # Sub-agent's provider attempts a nested use_skill, then
            # gives up and returns final text.
            subagent_provider.queue_response(
                CompletionResult(
                    text="",
                    tool_calls=[
                        ToolCall(
                            id="tu_inner_use_skill",
                            name="use_skill",
                            args={"skill": "demo", "query": "nested"},
                        )
                    ],
                    usage=Usage(input_tokens=0, output_tokens=0, subscription=True),
                )
            )
            subagent_provider.queue_text("SUB_FINAL_AFTER_NESTED_REJECT")

            parent_provider = FakeProvider()
            parent_provider.name = "claude_cli"
            # Parent calls use_skill ONCE.
            parent_provider.queue_response(
                CompletionResult(
                    text="",
                    tool_calls=[
                        ToolCall(
                            id="tu_use_skill_1",
                            name="use_skill",
                            args={"skill": "demo", "query": "go"},
                        )
                    ],
                    usage=Usage(input_tokens=1, output_tokens=1, subscription=True),
                )
            )
            parent_provider.queue_text("PARENT_FINAL: SUB_FINAL_AFTER_NESTED_REJECT")

            agent = Agent(
                store=store,
                provider=parent_provider,
                channel=channel,
                settings=settings,
                identity=identity,
                skill_registry=skill_registry,
                tool_registry=tool_registry,
            )
            await channel.start(agent.handle_turn)

            await channel.inject(_msg("hi"))

            # The sub-agent's tools advertised to its provider must
            # NOT include `use_skill` (TLC-16 §4).
            assert len(subagent_provider.calls) >= 1
            advertised = subagent_provider.calls[0].tools
            assert advertised is not None
            assert "use_skill" not in [t.name for t in advertised]

            # The parent dispatched use_skill exactly once — there
            # was no recursive dispatch.
            use_skill_calls_in_parent = [
                args
                for args in [
                    {"name": "use_skill"}  # placeholder; actual logic below
                ]
            ]
            # Concretely: the parent dispatched only one use_skill
            # tool call (we queued only one). We can't directly count
            # the agent's dispatch; we use the side effect that the
            # parent provider was called exactly twice (turn 1 with
            # the use_skill call, turn 2 with the final text), not
            # more.
            del use_skill_calls_in_parent
            assert len(parent_provider.calls) == 2, (
                f"expected exactly 2 parent provider calls (use_skill + final); "
                f"got {len(parent_provider.calls)}"
            )

            # And the user-facing reply still landed.
            finals = [s for s in channel.sent if s.kind == "final"]
            assert finals, "expected a final reply"
        finally:
            await store.close()
