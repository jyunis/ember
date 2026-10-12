"""`use_skill` host-level tool (FC-17, TLC-16, UoW-18).

The `use_skill` tool dispatches to a named skill in an isolated
sub-agent loop. This file tests the tool surface in isolation: given
fakes for every collaborator, the tool's `call(args)` produces the
expected `ToolResult`.

Tester barrier: we don't read implementation files. We import the
module lazily so the import error gives a useful message at the
expected baseline (the implementer hasn't shipped it yet).

Spec ambiguities flagged in the report:
  - The implementer's exact module path (`ember.tools.use_skill` vs.
    `ember.tools.subagent`) is a guess. Lazy-import keeps the failure
    informative.
  - The constructor parameter names + order. We pass everything by
    keyword and tolerate small naming drift via lazy construction.
  - Result-dict shape on success: the spec lists `status`, `text`,
    `usage` as required-on-ok; `partial_text`, `error_type`, `message`
    as conditional. We assert presence/absence accordingly.
  - On `unknown_skill` / `missing_tool`: the spec says the tool
    returns a typed-error ToolResult with `is_error=True`. We assert
    both shape and `is_error`.
  - The TLC-16 isolation tests rely on the sub-agent's Provider
    receiving a captured `tools` list whose element type has a
    `.name` attribute (ToolSpec) — we use the FakeProvider's
    `RecordedCall.tools`.
  - Cost attribution shape: §6 says "subagent_skill=<name> set on
    the message row" plus tool calls "recorded similarly". We assert
    via `FakeStore.subagent_messages` — tolerant of either a kwarg
    on `append_message` or a separate method that ends up calling
    `append_message` with the kwarg.

If the implementer factors the sub-agent loop differently, several
tests may need to fold in or out — see report §5.
"""

from __future__ import annotations

from typing import Any

import pytest

from ember.errors import AuthError, RateLimitError, TransportError
from ember.providers.base import (
    CompletionResult,
    Message,
    StreamHandler,
    ToolSpec,
    Usage,
)
from ember.providers.routing import ProviderRouter
from ember.skills.base import DeclarativeSkill
from ember.skills.registry import InMemorySkillRegistry
from ember.tools.base import Tool, ToolResult
from ember.tools.registry import Registry
from tests.fakes.fake_channel import FakeChannel
from tests.fakes.fake_provider import FakeProvider
from tests.fakes.fake_store import FakeStore


# --- Lazy import --------------------------------------------------------------


def _import_use_skill_tool():
    """Import the implementer's UseSkillTool. Lazy so tests collect
    even when the module doesn't exist yet (expected baseline)."""
    from ember.tools.use_skill import UseSkillTool

    return UseSkillTool


# --- Stub tools the skills can reference --------------------------------------


class _RecordingTool:
    """A trivial Tool the sub-agent might dispatch."""

    def __init__(self, name: str = "rec") -> None:
        self.name = name
        self.description = f"recording tool {name}"
        self.input_schema: dict[str, Any] = {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "additionalProperties": False,
        }
        self.requires_confirmation: bool = False
        self.calls: list[dict[str, Any]] = []

    async def call(self, args: dict[str, Any]) -> ToolResult:
        self.calls.append(args)
        return ToolResult(output={"echoed": args.get("text")}, is_error=False)


# --- Fixtures -----------------------------------------------------------------


@pytest.fixture
def skill_registry() -> InMemorySkillRegistry:
    sr = InMemorySkillRegistry()
    sr.replace(
        DeclarativeSkill(
            name="demo",
            description="A demo skill for FC-17 tests.",
            tools=("rec",),
            system_prompt="SUBAGENT_SYS_PROMPT_DEMO",
        )
    )
    return sr


@pytest.fixture
def tool_registry() -> Registry:
    """Global tool registry. Always carries `rec` (the skill's allowlisted
    tool) plus another tool the skill does NOT list (`hidden`).
    """
    reg = Registry()
    reg.register(_RecordingTool("rec"))
    reg.register(_RecordingTool("hidden"))
    return reg


@pytest.fixture
def subagent_provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def provider_router(subagent_provider: FakeProvider) -> ProviderRouter:
    """A router with the sub-agent provider registered as both
    `claude_cli` and the `fake` default. Implementer may resolve the
    skill's `preferred_model` (None here) → falls back to whatever the
    router treats as default.
    """
    # Register under multiple aliases so the implementer's lookup
    # succeeds regardless of how it resolves the route.
    subagent_provider.name = "claude_cli"
    return ProviderRouter({"claude_cli": subagent_provider, "fake": subagent_provider})


@pytest.fixture
def fake_store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def fake_channel() -> FakeChannel:
    return FakeChannel()


@pytest.fixture
def make_tool(
    skill_registry: InMemorySkillRegistry,
    tool_registry: Registry,
    provider_router: ProviderRouter,
    fake_store: FakeStore,
    fake_channel: FakeChannel,
):
    """Factory for `UseSkillTool` instances. Centralizes the
    keyword-arg surface the spec describes so each test only overrides
    what it cares about. Lazy import so collection succeeds even when
    the module is absent."""

    def _build(**overrides: Any):
        UseSkillTool = _import_use_skill_tool()
        kwargs: dict[str, Any] = {
            "skill_registry": skill_registry,
            "tool_registry": tool_registry,
            "provider_router": provider_router,
            "channel": fake_channel,
            "store": fake_store,
            "conversation_id": 1,
            "parent_message_id": 1,
            # bounds (small so timeout tests stay fast)
            "max_turns": 8,
            "max_tool_calls": 16,
            "timeout_s": 5,
        }
        kwargs.update(overrides)
        return UseSkillTool(**kwargs)

    return _build


# --- Static surface -----------------------------------------------------------


class TestStaticSurface:
    def test_tool_name_is_exact(self, make_tool) -> None:
        tool = make_tool()
        assert tool.name == "use_skill"

    def test_input_schema_is_jsonschema_object(self, make_tool) -> None:
        tool = make_tool()
        schema = tool.input_schema
        assert isinstance(schema, dict)
        assert schema.get("type") == "object"
        # Both `skill` and `query` are required + string-typed.
        required = schema.get("required") or []
        assert set(required) == {"skill", "query"}
        props = schema.get("properties") or {}
        assert "skill" in props and props["skill"].get("type") == "string"
        assert "query" in props and props["query"].get("type") == "string"

    def test_requires_confirmation_is_false(self, make_tool) -> None:
        # Dispatching to a skill is not destructive in itself; the
        # skill's tools may individually require confirmation.
        tool = make_tool()
        assert tool.requires_confirmation is False

    def test_satisfies_tool_protocol(self, make_tool) -> None:
        tool = make_tool()
        assert isinstance(tool, Tool)


# --- Happy path ---------------------------------------------------------------


class TestHappyPath:
    async def test_status_ok_when_subagent_returns_text(
        self,
        make_tool,
        subagent_provider: FakeProvider,
    ) -> None:
        subagent_provider.queue_text(
            "the sub-agent says hi",
            input_tokens=3,
            output_tokens=4,
            subscription=True,
        )
        tool = make_tool()
        result = await tool.call({"skill": "demo", "query": "hello?"})

        assert isinstance(result, ToolResult)
        assert result.is_error is False
        out = result.output
        assert isinstance(out, dict)
        assert out["status"] == "ok"
        assert out["text"] == "the sub-agent says hi"
        # usage shape: at minimum provider_calls + tool_calls (ints).
        usage = out.get("usage")
        assert isinstance(usage, dict)
        assert isinstance(usage.get("provider_calls"), int)
        assert isinstance(usage.get("tool_calls"), int)
        assert usage["provider_calls"] >= 1

    async def test_subagent_provider_was_called(
        self,
        make_tool,
        subagent_provider: FakeProvider,
    ) -> None:
        subagent_provider.queue_text("ok")
        tool = make_tool()
        await tool.call({"skill": "demo", "query": "hi"})
        assert len(subagent_provider.calls) == 1


# --- TLC-16 isolation ---------------------------------------------------------


class TestIsolation:
    async def test_subagent_tools_excludes_use_skill(
        self,
        make_tool,
        subagent_provider: FakeProvider,
        tool_registry: Registry,
    ) -> None:
        """TLC-16 §4 (no nesting) + §1 (tool scope): even though the
        global tool registry lists `use_skill` (we add it ourselves
        below), the sub-agent's Provider call must NOT see it."""

        # Insert a stub use_skill into the global registry so the
        # filter has something to filter out.
        class _StubUseSkill:
            name = "use_skill"
            description = "stub for isolation test"
            input_schema: dict[str, Any] = {
                "type": "object",
                "properties": {
                    "skill": {"type": "string"},
                    "query": {"type": "string"},
                },
                "required": ["skill", "query"],
            }
            requires_confirmation = False

            async def call(self, args: dict[str, Any]) -> ToolResult:
                raise AssertionError("nested use_skill should never run")

        tool_registry.register(_StubUseSkill())

        subagent_provider.queue_text("ok")
        tool = make_tool()
        await tool.call({"skill": "demo", "query": "hi"})

        # Capture the tools advertised to the sub-agent's provider.
        assert len(subagent_provider.calls) == 1
        advertised = subagent_provider.calls[0].tools
        assert advertised is not None, "sub-agent provider must receive tool specs"
        names = [t.name for t in advertised]
        assert "use_skill" not in names

    async def test_subagent_tools_filtered_to_skill_allowlist(
        self,
        make_tool,
        subagent_provider: FakeProvider,
    ) -> None:
        """TLC-16 §1: the sub-agent only sees the skill's listed tools."""
        subagent_provider.queue_text("ok")
        tool = make_tool()
        await tool.call({"skill": "demo", "query": "hi"})

        advertised = subagent_provider.calls[0].tools
        assert advertised is not None
        names = [t.name for t in advertised]
        # Only `rec` is in the demo skill's allowlist.
        assert names == ["rec"]
        # Confirm it's a ToolSpec (not a Tool).
        for spec in advertised:
            assert isinstance(spec, ToolSpec)
        # Hidden tool from global registry must NOT be present.
        assert "hidden" not in names

    async def test_subagent_system_prompt_is_skill_prompt(
        self,
        make_tool,
        subagent_provider: FakeProvider,
    ) -> None:
        """TLC-16 §2: the sub-agent's `system` is the skill's
        `system_prompt` only. No router prompt inheritance."""
        subagent_provider.queue_text("ok")
        tool = make_tool()
        await tool.call({"skill": "demo", "query": "hi"})

        system = subagent_provider.calls[0].system
        assert isinstance(system, str)
        assert "SUBAGENT_SYS_PROMPT_DEMO" in system
        # No identity / parent-router content should leak in. We can't
        # name the parent's prompt directly but we CAN assert the
        # prompt is essentially the skill prompt — short.
        # If implementer wraps the prompt with a tiny scaffolding
        # ("you are running as a sub-agent...") that's fine; assert no
        # *parent* prompt sentinel.
        assert "router" not in system.lower() or "router" in "SUBAGENT_SYS_PROMPT_DEMO".lower()

    async def test_subagent_history_is_only_query(
        self,
        make_tool,
        subagent_provider: FakeProvider,
    ) -> None:
        """TLC-16 §3: the sub-agent receives `[user(query)]` and nothing
        else. No parent conversation messages."""
        subagent_provider.queue_text("ok")
        tool = make_tool()
        await tool.call({"skill": "demo", "query": "the_query_sentinel"})

        msgs = subagent_provider.calls[0].messages
        assert len(msgs) == 1
        assert isinstance(msgs[0], Message)
        assert msgs[0].role == "user"
        assert msgs[0].content == "the_query_sentinel"


# --- Error / status paths -----------------------------------------------------


class TestUnknownSkill:
    async def test_unknown_skill_returns_typed_error(
        self,
        make_tool,
        subagent_provider: FakeProvider,
    ) -> None:
        tool = make_tool()
        result = await tool.call({"skill": "no_such_skill", "query": "hi"})
        assert isinstance(result, ToolResult)
        assert result.is_error is True
        out = result.output
        assert out["status"] == "unknown_skill"
        assert "error_type" in out
        assert "message" in out
        # No provider call should happen for an unknown skill.
        assert subagent_provider.calls == []


class TestMissingTool:
    async def test_skill_referencing_missing_tool_short_circuits(
        self,
        make_tool,
        subagent_provider: FakeProvider,
        skill_registry: InMemorySkillRegistry,
    ) -> None:
        # Replace `demo` with a skill that lists a tool not in the
        # global registry.
        skill_registry.replace(
            DeclarativeSkill(
                name="demo",
                description="d",
                tools=("rec", "missing_tool"),
                system_prompt="x",
            )
        )
        tool = make_tool()
        result = await tool.call({"skill": "demo", "query": "hi"})
        assert result.is_error is True
        out = result.output
        assert out["status"] == "missing_tool"
        # The error message names the missing tool.
        assert "missing_tool" in out.get("message", "")
        # No provider call.
        assert subagent_provider.calls == []


class TestProviderErrors:
    async def test_rate_limit_error_surfaces(
        self,
        make_tool,
        subagent_provider: FakeProvider,
    ) -> None:
        subagent_provider.queue_error(RateLimitError("slow down"))
        tool = make_tool()
        result = await tool.call({"skill": "demo", "query": "hi"})
        out = result.output
        assert out["status"] == "provider_error"
        assert out.get("error_type") == "rate_limit"
        assert "message" in out
        assert result.is_error is True

    async def test_auth_error_surfaces(
        self,
        make_tool,
        subagent_provider: FakeProvider,
    ) -> None:
        subagent_provider.queue_error(AuthError("bad key"))
        tool = make_tool()
        result = await tool.call({"skill": "demo", "query": "hi"})
        out = result.output
        assert out["status"] == "provider_error"
        assert out.get("error_type") == "auth"
        assert result.is_error is True

    async def test_transport_error_surfaces(
        self,
        make_tool,
        subagent_provider: FakeProvider,
    ) -> None:
        subagent_provider.queue_error(TransportError("conn reset"))
        tool = make_tool()
        result = await tool.call({"skill": "demo", "query": "hi"})
        out = result.output
        assert out["status"] == "provider_error"
        assert out.get("error_type") == "transport"
        assert result.is_error is True


# --- Bound exhaustion ---------------------------------------------------------


def _tool_call_response(tool_name: str, args: dict[str, Any], call_id: str) -> CompletionResult:
    """A CompletionResult that asks the sub-agent to dispatch a tool.

    The sub-agent loop is expected to read `tool_calls` (per the
    Provider Protocol's CompletionResult shape) and dispatch each.
    Once tool results are fed back, the next provider call returns
    final text.
    """
    from ember.providers.base import ToolCall

    return CompletionResult(
        text="",
        tool_calls=[ToolCall(id=call_id, name=tool_name, args=args)],
        usage=Usage(input_tokens=0, output_tokens=0, subscription=True),
    )


class TestBoundExhaustion:
    async def test_max_turns_exhausted(
        self,
        make_tool,
        subagent_provider: FakeProvider,
    ) -> None:
        # Queue more tool-call turns than `max_turns` so the loop never
        # sees a final-text response.
        for i in range(20):
            subagent_provider.queue_response(
                _tool_call_response("rec", {"text": f"turn_{i}"}, f"tu_{i}")
            )
        tool = make_tool(max_turns=3)
        result = await tool.call({"skill": "demo", "query": "hi"})
        out = result.output
        assert out["status"] == "exhausted_turns"
        # `partial_text` is populated with the most recent assistant
        # text. May be empty string if all turns were tool-call-only.
        assert "partial_text" in out
        assert result.is_error is True

    async def test_max_tool_calls_exhausted(
        self,
        make_tool,
        subagent_provider: FakeProvider,
    ) -> None:
        # Queue many turns, each with a tool call. With max_tool_calls
        # very low, the cumulative count breaks the bound first.
        for i in range(20):
            subagent_provider.queue_response(
                _tool_call_response("rec", {"text": f"t_{i}"}, f"tc_{i}")
            )
        tool = make_tool(max_turns=50, max_tool_calls=2)
        result = await tool.call({"skill": "demo", "query": "hi"})
        out = result.output
        assert out["status"] == "exhausted_tool_calls"
        assert "partial_text" in out
        assert result.is_error is True

    async def test_timeout_exhausted(
        self,
        make_tool,
        subagent_provider: FakeProvider,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Force the sub-agent provider to "sleep" longer than the
        # bound by monkey-patching its `complete` to await a long sleep.
        import asyncio

        original_complete = subagent_provider.complete

        async def slow_complete(
            system: str,
            messages: list[Message],
            tools: list[ToolSpec] | None = None,
            model: str | None = None,
            stream_handler: StreamHandler | None = None,
            session_id: str | None = None,
        ) -> CompletionResult:
            await asyncio.sleep(2.0)  # 2s, well beyond timeout_s=0.1
            return await original_complete(
                system,
                messages,
                tools,
                model,
                stream_handler,
                session_id,
            )

        # We DO want a queued response in case the sleep is somehow not
        # respected; default queue is empty so empty result returns.
        subagent_provider.queue_text("late")

        monkeypatch.setattr(subagent_provider, "complete", slow_complete)

        tool = make_tool(timeout_s=0.1)
        result = await tool.call({"skill": "demo", "query": "hi"})
        out = result.output
        assert out["status"] == "exhausted_timeout"
        assert result.is_error is True


# --- Cost attribution (TLC-16 §6) --------------------------------------------


class TestCostAttribution:
    async def test_subagent_messages_attributed_to_parent_with_skill_name(
        self,
        make_tool,
        subagent_provider: FakeProvider,
        fake_store: FakeStore,
    ) -> None:
        """A successful sub-agent run produces at least one append on
        the FakeStore that carries the `subagent_skill="demo"` kwarg
        and the parent's conversation_id."""
        subagent_provider.queue_text("done", input_tokens=10, output_tokens=20)
        tool = make_tool(conversation_id=42, parent_message_id=99)
        result = await tool.call({"skill": "demo", "query": "hi"})
        assert result.output["status"] == "ok"

        records = fake_store.subagent_messages
        assert records, (
            "expected at least one append_message attributed to the parent "
            "with subagent_skill set; got: "
            f"{[c.kwargs for c in fake_store.append_calls]}"
        )
        # All sub-agent records reference the parent's conversation_id.
        for r in records:
            assert r.conversation_id == 42
            assert r.kwargs.get("subagent_skill") == "demo"


# ============================================================================
# TLC-16 §5 sub-agent loop contracts (folded in here per the prompt's
# offer that subagent_loop tests can collapse into this file).
#
# These exercise the loop-level behaviors via the tool surface, since
# we can't see the implementer's factoring. If the implementer ships a
# distinct `run_subagent(...)` callable, copy these into
# tests/unit/test_subagent_loop.py and call it directly.
# ============================================================================


class TestSubagentLoopSurface:
    async def test_final_text_exits_cleanly_with_status_ok(
        self,
        make_tool,
        subagent_provider: FakeProvider,
    ) -> None:
        # No tool calls; immediate final text → status="ok".
        subagent_provider.queue_text("done")
        tool = make_tool()
        result = await tool.call({"skill": "demo", "query": "hi"})
        assert result.output["status"] == "ok"
        assert result.output["text"] == "done"

    async def test_loop_dispatches_tool_calls_through_filtered_registry(
        self,
        make_tool,
        subagent_provider: FakeProvider,
        tool_registry: Registry,
    ) -> None:
        """When the sub-agent provider returns a tool call for `rec`,
        the loop dispatches it through the (filtered) registry, then
        feeds the result back. The next turn's final text exits.
        """
        # Turn 1: tool call. Turn 2: final text.
        subagent_provider.queue_response(_tool_call_response("rec", {"text": "round-trip"}, "tu_1"))
        subagent_provider.queue_text("after-tool")

        tool = make_tool()
        result = await tool.call({"skill": "demo", "query": "hi"})

        # Status is ok and final text from turn 2 is returned.
        assert result.output["status"] == "ok"
        assert result.output["text"] == "after-tool"

        # The recording tool was actually invoked.
        rec_tool: _RecordingTool = tool_registry.get("rec")  # type: ignore[assignment]
        assert rec_tool.calls == [{"text": "round-trip"}]

        # provider_calls counts both turns.
        usage = result.output.get("usage", {})
        assert usage.get("provider_calls", 0) >= 2
        assert usage.get("tool_calls", 0) >= 1
