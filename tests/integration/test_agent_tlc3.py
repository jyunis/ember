"""Agent TLC-3 confirmation enforcement (v11).

Wires the registry's confirmation gate (returns marker) into the agent
loop:

  - The `_StreamObserver` watches `tool_use` + `tool_result` event
    pairs. When a tool_result content parses as
    `{"error": "confirmation_required", "action": ...}`, the observer
    persists `pending_tool_confirmation` on the conversation and emits
    a `🔒 Confirm: <action>. Reply 'yes' or 'no'.` status message.
  - On the next turn, BEFORE persisting the user message, the agent
    consumes `pending_tool_confirmation`:
      * `y(es)` → execute via `tool_registry.call_tool(name, args,
        allow_unconfirmed=True)`, persist user msg + synthetic
        assistant reply, send "final" status. Provider NOT called.
      * `n(o)`  → persist user msg + "🗑️ cancelled" reply. Provider
        NOT called.
      * Anything else → silently discard pending; proceed with normal
        turn.
  - The pre-turn ordering when BOTH `pending_profile_proposal` AND
    `pending_tool_confirmation` are set: profile-proposal handler runs
    first (existing behavior); tool-confirmation handler runs second.

Tester barrier: we read interface files (registry.py at v=4 surface,
agent.py at v9 surface, store/repo.py, store/types.py, fakes). We do
NOT read the new implementation pieces (allow_unconfirmed kwarg,
pending_tool_confirmation repo methods, observer extension, pre-turn
handler). ImportError / AttributeError on first run is expected.

Spec ambiguities the tester picked sensible defaults for:
  - Action-string format: spec v0 says `f"{name}({args_summary})"`.
    We assert tool name appears as substring; we don't pin the args
    formatting.
  - Status-message kind for the 🔒 Confirm prompt: we assert kind in
    {"status", "final"} since the spec says "emit a status to the
    channel" without pinning the kind. Both are reasonable.
  - tool_registry=None pre-turn flow: spec says "degrade by treating
    'no registry' as if no pending state — proceed with normal turn".
    We do NOT assert whether the row was consumed or left intact;
    only that the provider was called and a normal final reply landed.
  - "🗑️ cancelled" exact wording: we accept any reply containing
    "cancel" (case-insensitive). Implementer can refine the emoji or
    capitalization without breaking the test.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ember.agent import Agent
from ember.channels.base import ChannelMessage
from ember.config import Settings
from ember.identity.store import IdentityStore
from ember.providers.routing import ProviderRouter
from ember.store.repo import Store
from ember.tools.base import ToolResult
from ember.tools.registry import Registry
from tests.fakes.fake_channel import FakeChannel
from tests.fakes.fake_provider import FakeProvider
from tests.util import unstamp


# --- Wiring helpers -------------------------------------------------------


def _make_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")
    monkeypatch.setenv("DISCORD_CONVERSATIONS_CHANNEL_ID", "2")
    monkeypatch.setenv("DISCORD_SCHEDULER_CHANNEL_ID", "3")
    return Settings()  # type: ignore[call-arg]


def _msg(
    text: str = "hi",
    *,
    key: str = "thread-1",
    mid: str = "m1",
    user: str = "1000",
) -> ChannelMessage:
    return ChannelMessage(
        channel="discord",
        conversation_key=key,
        user_id=user,
        text=text,
        message_id=mid,
    )


def _named_provider(name: str) -> FakeProvider:
    fp = FakeProvider()
    fp.name = name
    return fp


@pytest.fixture
async def opened_store(tmp_path: Path):
    store = Store(tmp_path / "conversations.db")
    await store.open()
    try:
        yield store
    finally:
        await store.close()


@pytest.fixture
async def identity(tmp_path: Path) -> IdentityStore:
    s = IdentityStore(tmp_path / "identity")
    await s.ensure_initialized()
    return s


# --- Fake tools -----------------------------------------------------------


class _Destructive:
    """Confirmation-required tool. `call()` records every invocation
    so the test can assert whether the agent (via the pre-turn yes
    handler) actually executed the pending call."""

    name: str = "destructive_tool"
    description: str = "Pretends to delete things."
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "to": {"type": "string"},
        },
        "required": ["to"],
        "additionalProperties": False,
    }
    requires_confirmation: bool = True

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._raise: BaseException | None = None

    def queue_error(self, exc: BaseException) -> None:
        self._raise = exc

    async def call(self, args: dict[str, Any]) -> ToolResult:
        self.calls.append(args)
        if self._raise is not None:
            exc = self._raise
            self._raise = None
            raise exc
        return ToolResult(
            output={"deleted": args.get("to")},
            is_error=False,
        )


class _Harmless:
    """Confirmation-not-required tool — exercises the parallel-flow case."""

    name: str = "harmless_tool"
    description: str = "Returns a benign value."
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {"x": {"type": "string"}},
        "additionalProperties": False,
    }
    requires_confirmation: bool = False

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def call(self, args: dict[str, Any]) -> ToolResult:
        self.calls.append(args)
        return ToolResult(output={"echo": args.get("x")}, is_error=False)


def _build_registry(*tools: Any) -> Registry:
    reg = Registry()
    for t in tools:
        reg.register(t)
    return reg


def _make_agent(
    *,
    store: Store,
    settings: Settings,
    identity: IdentityStore | None = None,
    tool_registry: Registry | None = None,
    writer: Any = None,
) -> tuple[Agent, FakeChannel, FakeProvider, ProviderRouter]:
    primary = _named_provider("claude_cli")
    router = ProviderRouter({"claude_cli": primary})
    channel = FakeChannel()
    agent = Agent(
        store=store,
        channel=channel,
        settings=settings,
        router=router,
        identity=identity,
        tool_registry=tool_registry,
        writer=writer,
    )
    return agent, channel, primary, router


# --- Helpers to construct stream events -----------------------------------


def _tool_use_event(tool_use_id: str, name: str, input_args: dict[str, Any]) -> dict:
    return {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": name,
                    "input": input_args,
                },
            ],
        },
    }


def _confirm_marker_result_event(
    tool_use_id: str,
    *,
    action: str,
) -> dict:
    """A tool_result event whose content is the registry's
    confirmation_required marker (JSON-serialized)."""
    marker = json.dumps({"error": "confirmation_required", "action": action})
    return {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": marker,
                },
            ],
        },
    }


# ==========================================================================
# Stream observer detection (12, 13, 14)
# ==========================================================================


class TestStreamObserverDetectsMarker:
    async def test_observer_sets_pending_tool_confirmation(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        """tool_use(name, input) followed by tool_result(marker) →
        observer persists pending_tool_confirmation row with the
        right tool_name + args."""
        settings = _make_settings(monkeypatch, tmp_path)
        registry = _build_registry(_Destructive())
        agent, channel, primary, _ = _make_agent(
            store=opened_store,
            settings=settings,
            identity=identity,
            tool_registry=registry,
        )
        await channel.start(agent.handle_turn)

        primary.queue_text_with_events(
            "ok",
            events=[
                _tool_use_event(
                    "tu_1",
                    "destructive_tool",
                    {"to": "x@y.com"},
                ),
                _confirm_marker_result_event("tu_1", action="destructive_tool(to='x@y.com')"),
            ],
        )

        await channel.inject(_msg("delete that thing", mid="m1"))

        conv = await opened_store.get_or_create_conversation("discord", "thread-1")
        cur = await opened_store._c.execute(
            "SELECT pending_tool_confirmation FROM conversations WHERE id = ?",
            (conv.id,),
        )
        row = await cur.fetchone()
        await cur.close()
        assert row is not None and row[0] is not None, (
            "expected pending_tool_confirmation set after marker tool_result"
        )
        parsed = json.loads(row[0])
        assert parsed["tool_name"] == "destructive_tool"
        assert parsed["args"] == {"to": "x@y.com"}
        assert "action" in parsed and isinstance(parsed["action"], str)
        # set_at_iso is part of the spec'd payload.
        assert "set_at_iso" in parsed

    async def test_multiline_action_renders_in_diff_code_block(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        """A multi-line `action` summary (e.g. profile_update's unified
        diff) must be rendered as `🔒 Confirm: <head>\\n```diff\\n<body>\\n```\\nReply 'yes' or 'no'.`
        so Discord shows +/- coloring and the prompt sits on its own
        line. Single-line actions stay inline (covered by other tests)."""
        settings = _make_settings(monkeypatch, tmp_path)
        registry = _build_registry(_Destructive())
        agent, channel, primary, _ = _make_agent(
            store=opened_store,
            settings=settings,
            identity=identity,
            tool_registry=registry,
        )
        await channel.start(agent.handle_turn)

        multiline_action = (
            "profile_update:\n"
            "--- profile.md (current)\n"
            "+++ profile.md (proposed)\n"
            "@@ -1,1 +1,2 @@\n"
            " original\n"
            "+added line"
        )
        primary.queue_text_with_events(
            "ok",
            events=[
                _tool_use_event("tu_diff", "destructive_tool", {"to": "x@y.com"}),
                _confirm_marker_result_event("tu_diff", action=multiline_action),
            ],
        )

        await channel.inject(_msg("multiline confirm", mid="m_diff"))

        outs = [s.content for s in channel.sent if s.kind in ("status", "final")]
        joined = "\n".join(outs)
        assert "🔒 Confirm: profile_update:" in joined
        assert "```diff" in joined
        assert "+added line" in joined
        # The yes/no prompt is on its own line, not stuck to the diff.
        assert "\nReply 'yes' or 'no'." in joined

    async def test_observer_strips_bridge_prefix_from_tool_name(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        """Regression: when claude_cli reports a tool_use under its
        MCP-namespaced name (e.g. `mcp__agent__destructive_tool`), the
        observer must strip the `mcp__agent__` prefix before persisting,
        otherwise the redeem-side `registry.call_tool` raises KeyError.
        """
        settings = _make_settings(monkeypatch, tmp_path)
        registry = _build_registry(_Destructive())
        agent, channel, primary, _ = _make_agent(
            store=opened_store,
            settings=settings,
            identity=identity,
            tool_registry=registry,
        )
        await channel.start(agent.handle_turn)

        primary.queue_text_with_events(
            "ok",
            events=[
                _tool_use_event(
                    "tu_pref",
                    "mcp__agent__destructive_tool",  # claude_cli's name for it
                    {"to": "z@y.com"},
                ),
                _confirm_marker_result_event("tu_pref", action="destructive_tool(to='z@y.com')"),
            ],
        )

        await channel.inject(_msg("delete the prefixed thing", mid="m_pref"))

        conv = await opened_store.get_or_create_conversation("discord", "thread-1")
        cur = await opened_store._c.execute(
            "SELECT pending_tool_confirmation FROM conversations WHERE id = ?",
            (conv.id,),
        )
        row = await cur.fetchone()
        await cur.close()
        assert row is not None and row[0] is not None
        parsed = json.loads(row[0])
        assert parsed["tool_name"] == "destructive_tool", (
            f"expected the bridge prefix to be stripped; got {parsed['tool_name']!r}"
        )

    async def test_observer_emits_confirm_status(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        settings = _make_settings(monkeypatch, tmp_path)
        registry = _build_registry(_Destructive())
        agent, channel, primary, _ = _make_agent(
            store=opened_store,
            settings=settings,
            identity=identity,
            tool_registry=registry,
        )
        await channel.start(agent.handle_turn)

        primary.queue_text_with_events(
            "ok",
            events=[
                _tool_use_event("tu_2", "destructive_tool", {"to": "y@z.com"}),
                _confirm_marker_result_event("tu_2", action="destructive_tool(to='y@z.com')"),
            ],
        )

        await channel.inject(_msg("zap", mid="m_zap"))

        # Look for a status (or final) message containing "Confirm" or
        # the lock emoji + "yes"/"no".
        outs = [s for s in channel.sent if s.kind in ("status", "final")]
        joined = "\n".join(s.content for s in outs)
        assert "Confirm" in joined or "🔒" in joined, (
            f"expected a confirm prompt; got {[s.content for s in outs]!r}"
        )
        assert "yes" in joined.lower() or "y" in joined.lower(), (
            f"expected the prompt to mention reply with yes; got {joined!r}"
        )

    async def test_observer_parallel_to_memory_writes(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        """Both flows fire correctly when memory_store_memory tool_use
        AND a confirmation-required tool_use happen in the same turn.
        Memory-write announcement still emits, AND the confirmation
        marker still sets pending_tool_confirmation."""
        from ember.commands.memory import MemoryRevertTracker

        settings = _make_settings(monkeypatch, tmp_path)
        registry = _build_registry(_Destructive())
        tracker = MemoryRevertTracker()
        primary = _named_provider("claude_cli")
        router = ProviderRouter({"claude_cli": primary})
        channel = FakeChannel()
        agent = Agent(
            store=opened_store,
            channel=channel,
            settings=settings,
            router=router,
            identity=identity,
            tool_registry=registry,
            memory_tracker=tracker,
        )
        await channel.start(agent.handle_turn)

        # Two parallel announcements: memory write + confirmation gate.
        primary.queue_text_with_events(
            "ok",
            events=[
                # 1. assistant decides to call memory store
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "tu_mem",
                                "name": "memory__store_memory",
                                "input": {"text": "remember XYZ"},
                            }
                        ]
                    },
                },
                # 2. memory store result (fake id)
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "tu_mem",
                                "content": json.dumps({"id": 123}),
                            }
                        ]
                    },
                },
                # 3. assistant decides to call destructive
                _tool_use_event("tu_dest", "destructive_tool", {"to": "boss@corp.com"}),
                # 4. registry returns marker
                _confirm_marker_result_event(
                    "tu_dest", action="destructive_tool(to='boss@corp.com')"
                ),
            ],
        )

        await channel.inject(_msg("do both", mid="m_both"))

        # Memory tracker recorded the write.
        assert len(tracker) >= 1, "memory revert tracker should have recorded the memory write"

        # pending_tool_confirmation was set.
        conv = await opened_store.get_or_create_conversation("discord", "thread-1")
        cur = await opened_store._c.execute(
            "SELECT pending_tool_confirmation FROM conversations WHERE id = ?",
            (conv.id,),
        )
        row = await cur.fetchone()
        await cur.close()
        assert row is not None and row[0] is not None
        parsed = json.loads(row[0])
        assert parsed["tool_name"] == "destructive_tool"


# ==========================================================================
# Pre-turn yes / no / unrelated (15, 16, 17, 18)
# ==========================================================================


async def _seed_pending_tool_confirmation(
    store: Store,
    *,
    tool_name: str = "destructive_tool",
    args: dict[str, Any] | None = None,
    action: str = "destructive_tool(to='x@y.com')",
    key: str = "thread-1",
) -> int:
    """Set up a conversation with a pending tool confirmation row.
    Returns the conversation id."""
    if args is None:
        args = {"to": "x@y.com"}
    conv = await store.get_or_create_conversation("discord", key)
    payload = json.dumps(
        {
            "tool_name": tool_name,
            "args": args,
            "action": action,
            "set_at_iso": "2026-04-27T12:00:00Z",
        }
    )
    await store.set_pending_tool_confirmation(conv.id, payload)
    return conv.id


class TestPreTurnYes:
    async def test_yes_executes_tool_with_allow_unconfirmed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        """User reply 'y' → tool_registry.call_tool was invoked with
        allow_unconfirmed=True; the tool actually executed; provider
        was NOT called; a synthetic assistant reply was persisted."""
        settings = _make_settings(monkeypatch, tmp_path)
        dest = _Destructive()
        registry = _build_registry(dest)
        agent, channel, primary, _ = _make_agent(
            store=opened_store,
            settings=settings,
            identity=identity,
            tool_registry=registry,
        )
        await channel.start(agent.handle_turn)

        conv_id = await _seed_pending_tool_confirmation(opened_store, args={"to": "x@y.com"})

        await channel.inject(_msg("y", mid="m_yes"))

        # Tool was executed exactly once with the captured args.
        assert dest.calls == [{"to": "x@y.com"}], (
            f"expected destructive_tool to run once; got {dest.calls!r}"
        )

        # Provider was NOT called (short-circuit).
        assert primary.calls == [], "pre-turn yes must short-circuit; provider must not be called"

        # User's "y" + a synthetic assistant reply landed.
        msgs = await opened_store.recent_messages(conv_id, limit=200, include_compacted=True)
        last_two = msgs[-2:]
        assert last_two[0].role == "user" and last_two[0].content == "y"
        assert last_two[1].role == "assistant"
        assert last_two[1].content.strip() != ""

        # A "final" message was sent to the channel.
        finals = [s for s in channel.sent if s.kind == "final"]
        assert len(finals) == 1

    @pytest.mark.parametrize(
        "reply",
        [
            "y",
            "yes",
            "Yes",
            "YES",
            "y.",
            "yes!",
            " yes ",
        ],
    )
    async def test_yes_variants_all_execute(
        self,
        reply: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        settings = _make_settings(monkeypatch, tmp_path)
        dest = _Destructive()
        registry = _build_registry(dest)
        agent, channel, primary, _ = _make_agent(
            store=opened_store,
            settings=settings,
            identity=identity,
            tool_registry=registry,
        )
        await channel.start(agent.handle_turn)

        await _seed_pending_tool_confirmation(opened_store)

        await channel.inject(_msg(reply, mid=f"m_{reply.strip()}"))

        assert dest.calls == [{"to": "x@y.com"}], (
            f"reply {reply!r} should have executed the pending tool; calls={dest.calls!r}"
        )
        assert primary.calls == []


class TestPreTurnNo:
    @pytest.mark.parametrize(
        "reply",
        [
            "n",
            "no",
            "No",
            "NO",
            "n.",
            "no!",
            " no ",
        ],
    )
    async def test_no_discards_without_executing(
        self,
        reply: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        """User reply 'n' → tool NOT executed; cancelled reply
        persisted; provider NOT called."""
        settings = _make_settings(monkeypatch, tmp_path)
        dest = _Destructive()
        registry = _build_registry(dest)
        agent, channel, primary, _ = _make_agent(
            store=opened_store,
            settings=settings,
            identity=identity,
            tool_registry=registry,
        )
        await channel.start(agent.handle_turn)

        conv_id = await _seed_pending_tool_confirmation(opened_store)

        await channel.inject(_msg(reply, mid=f"m_no_{reply.strip()}"))

        assert dest.calls == [], f"reply {reply!r} must NOT execute the pending tool"
        assert primary.calls == [], "provider must not be called on cancel"

        # Persisted: user msg + synthetic cancel reply.
        msgs = await opened_store.recent_messages(conv_id, limit=200, include_compacted=True)
        last_two = msgs[-2:]
        assert last_two[0].role == "user" and last_two[0].content == reply
        assert last_two[1].role == "assistant"

        # Reply mentions cancellation.
        outs = [s for s in channel.sent if s.kind in ("status", "final")]
        joined = "\n".join(s.content for s in outs)
        assert "cancel" in joined.lower(), f"expected cancellation message; got {joined!r}"


class TestPreTurnUnrelated:
    async def test_unrelated_message_supersedes_pending(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        """Pending + 'actually let me think' → pending is silently
        discarded (consume_pending_tool_confirmation cleared the row);
        normal turn proceeds; provider IS called with user's text."""
        settings = _make_settings(monkeypatch, tmp_path)
        dest = _Destructive()
        registry = _build_registry(dest)
        agent, channel, primary, _ = _make_agent(
            store=opened_store,
            settings=settings,
            identity=identity,
            tool_registry=registry,
        )
        await channel.start(agent.handle_turn)

        conv_id = await _seed_pending_tool_confirmation(opened_store)

        primary.queue_text("ok thinking together")
        await channel.inject(_msg("actually let me think", mid="m_q"))

        # Tool NOT executed.
        assert dest.calls == []
        # Provider was called normally.
        assert len(primary.calls) == 1
        # The user's message reached the provider's history.
        last_user_in_history = [m for m in primary.calls[0].messages if m.role == "user"][-1]
        assert unstamp(last_user_in_history.content) == "actually let me think"

        # Pending row consumed.
        cur = await opened_store._c.execute(
            "SELECT pending_tool_confirmation FROM conversations WHERE id = ?",
            (conv_id,),
        )
        row = await cur.fetchone()
        await cur.close()
        assert row is not None and row[0] is None, (
            "pending_tool_confirmation must be cleared once next turn supersedes it"
        )

        # Normal final reply landed.
        finals = [s for s in channel.sent if s.kind == "final"]
        assert len(finals) == 1
        assert finals[0].content == "ok thinking together"


# ==========================================================================
# Pre-turn ordering with profile-proposal coexistence (19)
# ==========================================================================


class TestPreTurnOrdering:
    async def test_profile_proposal_handled_first_when_both_pending(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        """Both pending_profile_proposal AND pending_tool_confirmation
        set, user says 'y' → profile-proposal flow runs first
        (writer.commit invoked); the tool-confirmation handler does
        NOT fire on the same turn (destructive_tool NOT executed)."""
        from ember.identity.writer import CommitResult

        settings = _make_settings(monkeypatch, tmp_path)

        class _RecordingWriter:
            def __init__(self) -> None:
                self.calls: list[tuple[str, int, int]] = []

            async def commit(
                self,
                proposed_text: str,
                *,
                conversation_id: int,
                source_message_id: int,
            ) -> CommitResult:
                self.calls.append((proposed_text, conversation_id, source_message_id))
                return CommitResult(
                    committed=True,
                    profile_path=tmp_path / "profile.md",
                    git_commit_sha="0123456789ab",
                    error=None,
                )

        writer = _RecordingWriter()
        dest = _Destructive()
        registry = _build_registry(dest)
        agent, channel, primary, _ = _make_agent(
            store=opened_store,
            settings=settings,
            identity=identity,
            tool_registry=registry,
            writer=writer,
        )
        await channel.start(agent.handle_turn)

        # Seed both: a pending profile proposal + a pending tool
        # confirmation on the same conversation.
        conv = await opened_store.get_or_create_conversation("discord", "thread-1")
        # A prior user message to source the proposal.
        src_id = await opened_store.append_message(conv.id, "user", "I'm vegan")
        await opened_store.set_pending_proposal(
            conv.id,
            json.dumps(
                {
                    "proposed_text": "# updated\n",
                    "diff": "+vegan",
                    "source_message_id": src_id,
                }
            ),
        )
        await opened_store.set_pending_tool_confirmation(
            conv.id,
            json.dumps(
                {
                    "tool_name": "destructive_tool",
                    "args": {"to": "x@y.com"},
                    "action": "destructive_tool(to='x@y.com')",
                    "set_at_iso": "2026-04-27T12:00:00Z",
                }
            ),
        )

        await channel.inject(_msg("y", mid="m_both_y"))

        # Profile-proposal handler ran (writer.commit invoked).
        assert len(writer.calls) == 1, (
            f"expected writer.commit to run once on `y`; got {writer.calls!r}"
        )
        # Tool-confirmation handler did NOT fire — destructive_tool
        # was never executed on this turn.
        assert dest.calls == [], (
            "tool-confirmation handler must not fire when profile-proposal "
            "handler short-circuits the turn"
        )
        # Provider was NOT called either (proposal short-circuit).
        assert primary.calls == []


# ==========================================================================
# tool_registry None pre-turn yes (20)
# ==========================================================================


class TestPreTurnYesWithNoRegistry:
    async def test_no_registry_degrades_to_normal_turn(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        """Agent has tool_registry=None; a stale
        pending_tool_confirmation row exists; user says 'y'. Spec:
        degrade by treating 'no registry' as if no pending state —
        proceed with normal turn flow.

        We assert: provider IS called normally with the user's 'y' as
        the latest user message; a final reply lands. We do NOT assert
        whether the row was consumed (spec leaves this open — both
        "consume + skip" and "leave row sitting" are defensible)."""
        settings = _make_settings(monkeypatch, tmp_path)
        agent, channel, primary, _ = _make_agent(
            store=opened_store,
            settings=settings,
            identity=identity,
            tool_registry=None,
        )
        await channel.start(agent.handle_turn)

        await _seed_pending_tool_confirmation(opened_store)

        primary.queue_text("normal reply")
        await channel.inject(_msg("y", mid="m_no_reg"))

        # Provider was called normally.
        assert len(primary.calls) == 1, (
            "agent without tool_registry must fall through to normal turn (no-degrade-into-error)"
        )
        finals = [s for s in channel.sent if s.kind == "final"]
        assert len(finals) == 1
        assert finals[0].content == "normal reply"


# ==========================================================================
# Tool execution failure during pre-turn yes (21)
# ==========================================================================


class TestPreTurnYesToolFailure:
    async def test_tool_raises_surfaces_clean_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        """Pre-turn yes path: the executed tool raises. Agent surfaces
        a clean failure status, persists user msg + an error reply
        (no unhandled exception out of handle_turn)."""
        settings = _make_settings(monkeypatch, tmp_path)
        dest = _Destructive()
        dest.queue_error(RuntimeError("disk full"))
        registry = _build_registry(dest)
        agent, channel, primary, _ = _make_agent(
            store=opened_store,
            settings=settings,
            identity=identity,
            tool_registry=registry,
        )
        await channel.start(agent.handle_turn)

        conv_id = await _seed_pending_tool_confirmation(opened_store)
        # The failure now triggers one follow-up provider turn so the
        # model can explain / propose the next step (2026-09-27).
        primary.queue_text("Sorry — the disk is full; want me to clean up first?")

        # handle_turn must catch — it has a top-level try/except in
        # the agent loop that converts to user-visible status.
        await channel.inject(_msg("y", mid="m_yes_fail"))

        # Tool was attempted (so failure path is exercised).
        assert dest.calls == [{"to": "x@y.com"}]
        # Provider called exactly once, for the follow-up.
        assert len(primary.calls) == 1
        # Some failure-y output landed on the channel.
        outs = [s for s in channel.sent if s.kind in ("status", "final")]
        joined = "\n".join(s.content for s in outs)
        assert (
            "❌" in joined
            or "error" in joined.lower()
            or "fail" in joined.lower()
            or "disk full" in joined.lower()
        ), f"expected failure-flavored status; got {joined!r}"

        # User's "y" was persisted.
        msgs = await opened_store.recent_messages(conv_id, limit=200, include_compacted=True)
        # Find the user "y" message.
        assert any(m.role == "user" and m.content == "y" for m in msgs)


class TestMultipleGatedCallsPerTurn:
    async def test_second_marker_does_not_overwrite_first(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        """Two gated calls in one turn → the pending row lists both
        (`calls`), the top-level fields still describe the first, and a
        single `y` executes both in order (2026-09-27 vault incident:
        the second prompt silently replaced the first)."""
        settings = _make_settings(monkeypatch, tmp_path)
        dest = _Destructive()
        registry = _build_registry(dest)
        agent, channel, primary, _ = _make_agent(
            store=opened_store,
            settings=settings,
            identity=identity,
            tool_registry=registry,
        )
        await channel.start(agent.handle_turn)

        primary.queue_text_with_events(
            "ok",
            events=[
                _tool_use_event("tu_1", "destructive_tool", {"to": "a@y.com"}),
                _confirm_marker_result_event("tu_1", action="destructive_tool(to='a@y.com')"),
                _tool_use_event("tu_2", "destructive_tool", {"to": "b@y.com"}),
                _confirm_marker_result_event("tu_2", action="destructive_tool(to='b@y.com')"),
            ],
        )
        await channel.inject(_msg("do both", mid="m1"))

        conv = await opened_store.get_or_create_conversation("discord", "thread-1")
        cur = await opened_store._c.execute(
            "SELECT pending_tool_confirmation FROM conversations WHERE id = ?",
            (conv.id,),
        )
        row = await cur.fetchone()
        await cur.close()
        parsed = json.loads(row[0])
        assert parsed["args"] == {"to": "a@y.com"}, "top-level fields must describe the FIRST call"
        assert [c["args"] for c in parsed["calls"]] == [{"to": "a@y.com"}, {"to": "b@y.com"}]
        prompts = [s.content for s in channel.sent if "🔒" in s.content]
        assert len(prompts) == 2
        assert "2 pending actions" in prompts[1]

        await channel.inject(_msg("y", mid="m2"))
        assert dest.calls == [{"to": "a@y.com"}, {"to": "b@y.com"}]
        finals = [s for s in channel.sent if s.kind == "final"]
        assert finals[-1].content.count("✅") == 2

    async def test_legacy_single_payload_still_redeems(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        """A row written before `calls` existed (no such key) still runs."""
        settings = _make_settings(monkeypatch, tmp_path)
        dest = _Destructive()
        registry = _build_registry(dest)
        agent, channel, primary, _ = _make_agent(
            store=opened_store,
            settings=settings,
            identity=identity,
            tool_registry=registry,
        )
        await channel.start(agent.handle_turn)
        await _seed_pending_tool_confirmation(opened_store, args={"to": "x@y.com"})
        await channel.inject(_msg("y", mid="m_yes"))
        assert dest.calls == [{"to": "x@y.com"}]


class TestFailedRedemptionFollowsUp:
    async def test_failure_runs_a_follow_up_turn(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        """A confirmed call that fails must not dead-end at ❌: the model
        gets a follow-up turn carrying the failure and replies."""
        from ember.errors import ToolError

        settings = _make_settings(monkeypatch, tmp_path)
        dest = _Destructive()
        dest.queue_error(ToolError("vault: not found: X (field 'username' is empty)"))
        registry = _build_registry(dest)
        agent, channel, primary, _ = _make_agent(
            store=opened_store,
            settings=settings,
            identity=identity,
            tool_registry=registry,
        )
        await channel.start(agent.handle_turn)
        conv_id = await _seed_pending_tool_confirmation(opened_store)
        primary.queue_text("That field is empty; want Client ID instead?")

        await channel.inject(_msg("y", mid="m_yes"))

        finals = [s.content for s in channel.sent if s.kind == "final"]
        assert any("❌" in f and "username" in f for f in finals), finals
        assert finals[-1] == "That field is empty; want Client ID instead?"
        assert len(primary.calls) == 1, "exactly one follow-up provider turn"
        msgs = await opened_store.recent_messages(conv_id, limit=200, include_compacted=True)
        users = [m.content for m in msgs if m.role == "user"]
        assert users[-2] == "y"
        assert "[Automatic follow-up]" in users[-1] and "username" in users[-1]
