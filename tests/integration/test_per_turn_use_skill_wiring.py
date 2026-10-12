"""Per-turn `use_skill` wiring (FC-17 follow-ups, closed by FC-18).

The FC-17 implementer flagged two production-relevant gaps that the
FC-17 fixture-based tests didn't cover. FC-18 closes them at the
agent-loop level since FC-18 is the last touch on per-turn assembly:

**Wiring A** — per-turn `UseSkillTool` instantiation:
  Each turn must construct a fresh `UseSkillTool` parameterized with
  `parent_message_id` = the inbound user message's persisted ID.
  Two sequential turns must use two distinct parent_message_id values.

**Wiring B** — real `conversation_key` on sub-agent status messages:
  When a sub-agent dispatches a tool and the channel sends a status
  message, the `conversation_key` on the FakeChannel.send call must
  be the parent conversation's key — not a synthetic
  `"__subagent__"`.

Tester barrier: we don't read `agent.py` body or `use_skill.py`. We
observe wiring via:
  - The persisted `messages.parent_message_id` column (read via raw
    SQL — same pattern as `test_agent_active_skill_injects_system_prompt.py`).
  - `FakeChannel.sent` records, where each entry carries the
    `conversation_key` the agent passed to `send()`.

Spec ambiguity flagged in report:

  - The implementer's UseSkillTool factory: we don't know whether the
    implementer instantiates UseSkillTool inside `agent.handle_turn`
    once per turn, or wires a factory. Either is acceptable as long
    as `parent_message_id` differs per turn. We assert the observable
    side effect: sub-agent message rows carry distinct
    `parent_message_id` values across two turns.

  - In wiring B: status messages may be emitted as kind="status" or
    as another marker. We pin only that the `conversation_key` field
    on every recorded `send` call equals the parent's real key, never
    a synthetic value.
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
from ember.tools.base import ToolResult
from ember.tools.registry import Registry
from tests.fakes.fake_channel import FakeChannel
from tests.fakes.fake_provider import FakeProvider


# --- Helpers ------------------------------------------------------------------


def _make_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")
    monkeypatch.setenv("DISCORD_CONVERSATIONS_CHANNEL_ID", "2")
    monkeypatch.setenv("DISCORD_SCHEDULER_CHANNEL_ID", "3")
    return Settings()  # type: ignore[call-arg]


def _msg(text: str, *, key: str = "thread-1", mid: str = "m1") -> ChannelMessage:
    return ChannelMessage(
        channel="discord",
        conversation_key=key,
        user_id="1000",
        text=text,
        message_id=mid,
    )


class _StubTool:
    """Trivial Tool the demo skill could list."""

    def __init__(self, name: str = "rec") -> None:
        self.name = name
        self.description = f"stub {name}"
        self.input_schema: dict[str, Any] = {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "additionalProperties": False,
        }
        self.requires_confirmation: bool = False

    async def call(self, args: dict[str, Any]) -> ToolResult:
        return ToolResult(output={"echoed": args.get("text", "")}, is_error=False)


def _use_skill_tool_call(call_id: str, *, query: str = "hello") -> CompletionResult:
    return CompletionResult(
        text="",
        tool_calls=[
            ToolCall(
                id=call_id,
                name="use_skill",
                args={"skill": "demo", "query": query},
            )
        ],
        usage=Usage(input_tokens=1, output_tokens=1, subscription=True),
    )


async def _read_subagent_message_rows(store: Store) -> list[dict[str, Any]]:
    """Read `messages` rows that look like sub-agent rows (have a
    non-NULL `subagent_skill`). Returns dicts with id +
    parent_message_id for assertions."""
    cur = await store._c.execute(  # type: ignore[attr-defined]
        """
        SELECT id, conversation_id, role, content,
               subagent_skill, parent_message_id
        FROM messages
        WHERE subagent_skill IS NOT NULL
        ORDER BY id ASC
        """
    )
    rows = await cur.fetchall()
    await cur.close()
    return [
        {
            "id": r["id"],
            "conversation_id": r["conversation_id"],
            "role": r["role"],
            "content": r["content"],
            "subagent_skill": r["subagent_skill"],
            "parent_message_id": r["parent_message_id"],
        }
        for r in rows
    ]


async def _read_user_message_ids(store: Store, conversation_id: int) -> list[int]:
    """All `messages.id` values for `role='user'` rows on the given
    conversation, in insertion order. Used to identify which user
    message ID drove which turn."""
    cur = await store._c.execute(  # type: ignore[attr-defined]
        """
        SELECT id FROM messages
        WHERE conversation_id = ? AND role = 'user'
        ORDER BY id ASC
        """,
        (conversation_id,),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [int(r["id"]) for r in rows]


def _has_use_skill_module() -> bool:
    """Check whether `ember.tools.use_skill` is importable. Required for
    these tests; they skip cleanly if absent at the baseline."""
    try:
        import ember.tools.use_skill  # noqa: F401

        return True
    except ImportError:
        return False


# --- Wiring A — per-turn parent_message_id -----------------------------------


class TestPerTurnParentMessageId:
    """Each turn that triggers a sub-agent must run with a fresh
    `UseSkillTool` whose `parent_message_id` matches the persisted ID
    of *that* turn's inbound user message.

    We can't observe constructor params directly without reading
    `agent.py`. Instead we observe the side effect: the sub-agent
    persists messages with `parent_message_id=<that turn's user msg>`.
    Two sequential turns → two distinct parent_message_id values.
    """

    async def test_two_turns_produce_two_distinct_parent_message_ids(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        if not _has_use_skill_module():
            pytest.skip(
                "ember.tools.use_skill not yet shipped — wiring tests need the FC-17 module."
            )

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
                    system_prompt="DEMO_SYS",
                )
            )

            tool_registry = Registry()
            tool_registry.register(_StubTool("rec"))

            # Sub-agent provider — returns final text immediately for
            # both runs.
            subagent_provider = FakeProvider()
            subagent_provider.name = "claude_cli"
            subagent_provider.queue_text("SUB_FINAL_1")
            subagent_provider.queue_text("SUB_FINAL_2")
            subagent_router = ProviderRouter({"claude_cli": subagent_provider})

            channel = FakeChannel()

            # Parent provider — turn 1: use_skill, turn 1.5: final
            #                    turn 2: use_skill, turn 2.5: final
            parent_provider = FakeProvider()
            parent_provider.name = "claude_cli"
            parent_provider.queue_response(_use_skill_tool_call("tu_1"))
            parent_provider.queue_text("PARENT_FINAL_1")
            parent_provider.queue_response(_use_skill_tool_call("tu_2"))
            parent_provider.queue_text("PARENT_FINAL_2")

            # The agent must wire a fresh UseSkillTool per turn. Until
            # that wiring exists, the implementer must not pre-register
            # a global UseSkillTool — but the test still needs the tool
            # available somehow. The expected production wiring is:
            # `Agent` constructs UseSkillTool internally per turn,
            # passing `parent_message_id` from the just-persisted user
            # message. So we do NOT pre-register `use_skill` here.
            #
            # If the implementer's design pre-registers a sentinel
            # use_skill in the global registry that the per-turn loop
            # *replaces*, this test still observes the sub-agent
            # message rows correctly.

            agent = Agent(
                store=store,
                provider=parent_provider,
                channel=channel,
                settings=settings,
                identity=identity,
                skill_registry=skill_registry,
                tool_registry=tool_registry,
                router=subagent_router,
            )
            await channel.start(agent.handle_turn)

            # Two sequential inbound messages.
            await channel.inject(_msg("first", mid="m1"))
            await channel.inject(_msg("second", mid="m2"))

            # Pull all sub-agent message rows and group by
            # parent_message_id.
            rows = await _read_subagent_message_rows(store)
            assert rows, (
                "expected sub-agent rows persisted with subagent_skill set; "
                "got no rows. Either FC-17 cost-attribution wiring is missing "
                "or the sub-agent never ran."
            )

            parent_ids = {r["parent_message_id"] for r in rows}
            # None is not a valid parent_message_id for a sub-agent row —
            # the wiring contract demands it points at a real user msg.
            assert None not in parent_ids, (
                f"sub-agent rows must carry a real parent_message_id; got None among: {parent_ids}"
            )
            assert len(parent_ids) >= 2, (
                f"expected at least two distinct parent_message_id values "
                f"across two sequential sub-agent runs; got {parent_ids}"
            )

            # Cross-check: the parent_message_ids should match the
            # persisted user-message ids for the conversation.
            conv = await store.get_or_create_conversation("discord", "thread-1")
            user_ids = await _read_user_message_ids(store, conv.id)
            assert len(user_ids) >= 2, f"expected two persisted user messages; got {user_ids}"
            for pid in parent_ids:
                assert pid in user_ids, (
                    f"sub-agent row's parent_message_id={pid} doesn't match "
                    f"any persisted user message id {user_ids} — wiring is "
                    f"using a stale or synthetic id."
                )
        finally:
            await store.close()


# --- Wiring B — sub-agent status messages target parent's conversation_key ---


class TestSubagentStatusUsesParentConversationKey:
    """When a sub-agent's tool dispatch emits a status message, the
    channel.send call must target the parent conversation's
    `conversation_key`, never a synthetic value like `"__subagent__"`.
    """

    async def test_status_messages_carry_parent_conversation_key(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        if not _has_use_skill_module():
            pytest.skip(
                "ember.tools.use_skill not yet shipped — wiring tests need the FC-17 module."
            )

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
                    system_prompt="DEMO_SYS",
                )
            )

            tool_registry = Registry()
            tool_registry.register(_StubTool("rec"))

            # Sub-agent provider: emit a tool_use stream event (forces a
            # status forward) then return final text.
            subagent_provider = FakeProvider()
            subagent_provider.name = "claude_cli"
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
            subagent_router = ProviderRouter({"claude_cli": subagent_provider})

            channel = FakeChannel()

            parent_provider = FakeProvider()
            parent_provider.name = "claude_cli"
            parent_provider.queue_response(_use_skill_tool_call("tu_use_skill_1"))
            parent_provider.queue_text("PARENT_FINAL")

            agent = Agent(
                store=store,
                provider=parent_provider,
                channel=channel,
                settings=settings,
                identity=identity,
                skill_registry=skill_registry,
                tool_registry=tool_registry,
                router=subagent_router,
            )
            await channel.start(agent.handle_turn)

            # Drive one inbound message in conversation key="thread-42".
            await channel.inject(_msg("hi", key="thread-42", mid="m1"))

            # Every recorded send must target the parent's real
            # conversation_key, never a synthetic value.
            assert channel.sent, "expected at least one channel.send call"
            forbidden_keys = {"__subagent__", "subagent", ""}
            for s in channel.sent:
                assert s.conversation_key not in forbidden_keys, (
                    f"sub-agent status leaked synthetic conversation_key "
                    f"{s.conversation_key!r} on a send: {s}"
                )
                assert s.conversation_key == "thread-42", (
                    f"every send during this turn must target the parent's "
                    f"conversation_key='thread-42'; got "
                    f"{s.conversation_key!r} on: {s}"
                )

            # Secondary check: at least one status message exists from
            # the sub-agent's tool dispatch — confirms FC-17 status
            # forwarding still fires under the new wiring.
            statuses = [s for s in channel.sent if s.kind == "status"]
            assert statuses, (
                "expected at least one status message from the sub-agent's "
                "tool dispatch (FC-17 status-forwarding contract)"
            )
            # Optional: at least one status mentions the skill name
            # (FC-17's `[<skill>]` prefix contract; we accept any
            # mention of 'demo').
            demo_statuses = [s for s in statuses if "demo" in s.content.lower()]
            assert demo_statuses, (
                "expected at least one status message to mention the "
                "skill name (FC-17 [<skill>] prefix); got: "
                f"{[s.content for s in statuses]}"
            )
        finally:
            await store.close()
