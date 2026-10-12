"""Sub-agent loop contracts (FC-17, TLC-16, UoW-18).

The sub-agent loop is the inner provider/tool loop spawned by the
`use_skill` tool. TLC-16 §5 says it's bounded by `max_turns`,
`max_tool_calls`, and `timeout_s`. The loop is isolated from the
parent (TLC-16 §1-§4).

The implementer is free to factor this as:

  (a) An inline body inside `UseSkillTool.call`. In that case, these
      tests should fold into `test_use_skill_tool.py` (already done
      there — see the `TestSubagentLoopSurface` class).
  (b) A standalone callable `ember.tools.use_skill.run_subagent(
        *, skill, query, provider, tool_registry, channel, store,
        conversation_id, parent_message_id,
        max_turns, max_tool_calls, timeout_s) -> dict`.
  (c) A class `SubAgentRunner` with a `.run(query)` method.

This file targets factoring (b). If the implementer picks (a) or
(c), these tests collect-fail with `ModuleNotFoundError` /
`ImportError`. That's an interface request: ship a small standalone
runner so the contracts can be tested without the full Tool surface.

Tests here are minimal because the bulk of the loop's contract is
already pinned in `test_use_skill_tool.py::TestSubagentLoopSurface`.
The standalone tests below add only what's awkward to assert through
the tool surface: the bound semantics' precise definition.

If `run_subagent` doesn't ship, every test below skips at collection
time with the lazy-import pattern.
"""

from __future__ import annotations

from typing import Any


from ember.providers.base import CompletionResult, ToolCall, Usage
from ember.skills.base import DeclarativeSkill
from ember.tools.base import ToolResult
from ember.tools.registry import Registry
from tests.fakes.fake_channel import FakeChannel
from tests.fakes.fake_provider import FakeProvider
from tests.fakes.fake_store import FakeStore


def _import_run_subagent():
    """Lazy import. If the implementer doesn't ship a standalone
    `run_subagent` callable, every test in this file will fail with
    ImportError — that's a flag that the factoring is (a) or (c)."""
    from ember.tools.use_skill import run_subagent

    return run_subagent


# --- Recording stub tool ------------------------------------------------------


class _RecordingTool:
    def __init__(self, name: str = "rec") -> None:
        self.name = name
        self.description = "rec"
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


# --- Builders -----------------------------------------------------------------


def _build_skill(
    *,
    name: str = "demo",
    tools: tuple[str, ...] = ("rec",),
    system_prompt: str = "S",
) -> DeclarativeSkill:
    return DeclarativeSkill(
        name=name,
        description="d",
        tools=tools,
        system_prompt=system_prompt,
    )


def _build_call_kwargs(
    *,
    skill: DeclarativeSkill,
    query: str,
    provider: FakeProvider,
    tool_registry: Registry,
    channel: FakeChannel,
    store: FakeStore,
    conversation_id: int = 1,
    parent_message_id: int = 1,
    max_turns: int = 8,
    max_tool_calls: int = 16,
    timeout_s: float = 5,
) -> dict[str, Any]:
    """Centralize the kwarg name set the spec describes. If the
    implementer ships a different signature, tweaks happen here."""
    return {
        "skill": skill,
        "query": query,
        "provider": provider,
        "tool_registry": tool_registry,
        "channel": channel,
        "store": store,
        "conversation_id": conversation_id,
        "parent_message_id": parent_message_id,
        "max_turns": max_turns,
        "max_tool_calls": max_tool_calls,
        "timeout_s": timeout_s,
    }


# --- Tests --------------------------------------------------------------------


class TestProviderArgs:
    async def test_provider_receives_skill_prompt_and_user_query(self) -> None:
        run_subagent = _import_run_subagent()
        skill = _build_skill(system_prompt="SUB_SYS")
        provider = FakeProvider()
        provider.queue_text("done")
        tool_registry = Registry()
        tool_registry.register(_RecordingTool("rec"))

        await run_subagent(
            **_build_call_kwargs(
                skill=skill,
                query="HELLO_QUERY",
                provider=provider,
                tool_registry=tool_registry,
                channel=FakeChannel(),
                store=FakeStore(),
            )
        )

        # TLC-16 §2 / §3: provider.complete saw the skill prompt + a
        # single user message with the query.
        assert len(provider.calls) == 1
        call = provider.calls[0]
        assert "SUB_SYS" in call.system
        assert len(call.messages) == 1
        assert call.messages[0].role == "user"
        assert call.messages[0].content == "HELLO_QUERY"

    async def test_provider_tools_are_skill_filtered(self) -> None:
        run_subagent = _import_run_subagent()
        skill = _build_skill(tools=("rec",))
        provider = FakeProvider()
        provider.queue_text("done")
        tool_registry = Registry()
        tool_registry.register(_RecordingTool("rec"))
        tool_registry.register(_RecordingTool("hidden"))

        await run_subagent(
            **_build_call_kwargs(
                skill=skill,
                query="hi",
                provider=provider,
                tool_registry=tool_registry,
                channel=FakeChannel(),
                store=FakeStore(),
            )
        )
        advertised = provider.calls[0].tools
        assert advertised is not None
        names = [t.name for t in advertised]
        assert names == ["rec"]


class TestBoundsSemantics:
    """TLC-16 §5: max_turns is provider calls; max_tool_calls is
    cumulative across turns; timeout_s is wall-clock from start of
    sub-agent loop."""

    async def test_max_turns_counts_provider_calls_exactly(self) -> None:
        run_subagent = _import_run_subagent()
        skill = _build_skill()

        provider = FakeProvider()
        # Always ask for tool dispatch — the loop will keep cycling.
        for i in range(10):
            provider.queue_response(
                CompletionResult(
                    text="",
                    tool_calls=[ToolCall(id=f"id_{i}", name="rec", args={"text": "x"})],
                    usage=Usage(input_tokens=0, output_tokens=0, subscription=True),
                )
            )

        tool_registry = Registry()
        tool_registry.register(_RecordingTool("rec"))

        result = await run_subagent(
            **_build_call_kwargs(
                skill=skill,
                query="hi",
                provider=provider,
                tool_registry=tool_registry,
                channel=FakeChannel(),
                store=FakeStore(),
                max_turns=4,
                max_tool_calls=1000,
            )
        )
        # Exactly `max_turns` provider calls (not max_turns+1).
        assert len(provider.calls) == 4
        assert result["status"] == "exhausted_turns"

    async def test_max_tool_calls_caps_cumulative_count(self) -> None:
        run_subagent = _import_run_subagent()
        skill = _build_skill()

        provider = FakeProvider()
        for i in range(10):
            provider.queue_response(
                CompletionResult(
                    text="",
                    tool_calls=[
                        ToolCall(id=f"a_{i}", name="rec", args={"text": "a"}),
                        ToolCall(id=f"b_{i}", name="rec", args={"text": "b"}),
                    ],
                    usage=Usage(input_tokens=0, output_tokens=0, subscription=True),
                )
            )

        tool_registry = Registry()
        rec = _RecordingTool("rec")
        tool_registry.register(rec)

        result = await run_subagent(
            **_build_call_kwargs(
                skill=skill,
                query="hi",
                provider=provider,
                tool_registry=tool_registry,
                channel=FakeChannel(),
                store=FakeStore(),
                max_turns=1000,
                max_tool_calls=3,
            )
        )
        # Cumulative tool-call count never exceeds the bound (the loop
        # may dispatch up to that bound, then stop).
        assert len(rec.calls) <= 3
        assert result["status"] == "exhausted_tool_calls"


class TestToolErrorContinues:
    """A tool raising `ToolError` mid-sub-agent should be caught and
    fed back to the model as a tool result; the loop continues until
    the next bound or final text."""

    async def test_tool_error_does_not_crash_loop(self) -> None:
        run_subagent = _import_run_subagent()
        skill = _build_skill()

        from ember.errors import ToolError

        class _RaisingTool:
            name = "rec"
            description = "raises"
            input_schema: dict[str, Any] = {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "additionalProperties": False,
            }
            requires_confirmation = False

            async def call(self, args: dict[str, Any]) -> ToolResult:
                raise ToolError("boom")

        tool_registry = Registry()
        tool_registry.register(_RaisingTool())  # type: ignore[arg-type]

        provider = FakeProvider()
        provider.queue_response(
            CompletionResult(
                text="",
                tool_calls=[ToolCall(id="tu_1", name="rec", args={"text": "x"})],
                usage=Usage(input_tokens=0, output_tokens=0, subscription=True),
            )
        )
        provider.queue_text("recovered")

        result = await run_subagent(
            **_build_call_kwargs(
                skill=skill,
                query="hi",
                provider=provider,
                tool_registry=tool_registry,
                channel=FakeChannel(),
                store=FakeStore(),
            )
        )
        # Loop kept going after ToolError.
        assert result["status"] == "ok"
        assert result["text"] == "recovered"
