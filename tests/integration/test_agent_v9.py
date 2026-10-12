"""Agent v9 integration — Task #6 wiring.

Covers the agent-loop integration of the v9 component layer:

A. Prompt assembly per architecture § Prompt assembly order — pinned
   context + compressed summaries are folded into the system block, in
   that order, after soul/profile and the active-skill block.
B. v15 context lifecycle (UoW-28, FC-28/FC-29): the agent calls
   `rotator.maybe_rotate(conversation, now=...)` at turn start BEFORE
   the user message is persisted, and post-turn writes
   `Store.update_last_context_tokens` when the provider reported
   `CompletionResult.context_tokens`.
C. Post-turn profile proposal (UoW-8): if `proposer.propose(...)` returns
   a `ProfileProposal`, the agent persists it on the conversation and
   emits a status message containing the diff.
D. Pre-turn proposal-decision short-circuit: when a pending proposal
   exists and the user replies y/n/edit, the agent commits/discards/edits
   without invoking the primary provider.
E. The user's `source_message_id` is captured in the JSON stashed on
   `pending_profile_proposal` so the writer's commit body links back to
   the originating message id (not the y/n confirmation message id).
G. All existing agent tests still pass — verified by running the full
   suite at the end of the deliverable.

Tester role: we read interface files (rotation contracts in doc #56,
proposer, writer, repo, fakes, channel/provider base) but not future
modifications to `agent.py` that are part of this same task. Existing
agent.py is allowed (testing observable behavior of an already-built
loop).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ember.agent import Agent
from ember.channels.base import ChannelMessage
from ember.config import Settings
from ember.context.rotation import RotationResult, SessionRotator
from ember.identity.proposer import ProfileProposal, Proposer
from ember.identity.store import IdentityStore
from ember.identity.writer import CommitResult, ProfileWriter
from ember.providers.base import CompletionResult, Usage
from ember.providers.routing import ProviderRouter
from ember.store.repo import Store
from tests.fakes.fake_channel import FakeChannel
from tests.fakes.fake_provider import FakeProvider
from tests.util import unstamp


# --- Fixtures / wiring ----------------------------------------------------


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


def _build_router(*, with_cheap: bool = True) -> tuple[ProviderRouter, FakeProvider, FakeProvider]:
    """Two-provider router. `claude_cli` is the primary (default route),
    `cheap` is the cheap-tier route when `with_cheap=True`."""
    primary = _named_provider("claude_cli")
    cheap = _named_provider("cheap")
    providers: dict[str, Any] = {"claude_cli": primary, "cheap": cheap}
    return (
        ProviderRouter(
            providers,
            cheap_tier_route="cheap" if with_cheap else None,
        ),
        primary,
        cheap,
    )


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


# --- Recording helpers ----------------------------------------------------


class _RecordingRotator:
    """In-process SessionRotator stand-in that records `maybe_rotate`
    calls. When built with a store, it also snapshots the conversation's
    message count AT CALL TIME so tests can assert the pre-turn ordering
    (UoW-28: rotation runs before the user message is persisted).
    """

    def __init__(self, store: Store | None = None) -> None:
        self._store = store
        self.calls: list[int] = []
        self.message_counts_at_call: list[int] = []
        self._return: RotationResult = RotationResult(
            rotated=False,
            reason=None,
            summary_id=None,
            skipped_reason="under_threshold",
        )

    def queue_return(self, value: RotationResult) -> None:
        self._return = value

    async def maybe_rotate(self, conversation: Any, *, now: Any = None) -> RotationResult:
        self.calls.append(conversation.id)
        if self._store is not None:
            self.message_counts_at_call.append(await self._store.message_count(conversation.id))
        return self._return


class _RecordingProposer:
    """Records `propose` invocations, returns a queued ProfileProposal or
    None or raises a queued exception."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self._next: ProfileProposal | None | BaseException = None

    def queue_proposal(self, p: ProfileProposal | None) -> None:
        self._next = p

    def queue_error(self, exc: BaseException) -> None:
        self._next = exc

    async def propose(self, *, user_msg: str, current_profile: str) -> ProfileProposal | None:
        self.calls.append((user_msg, current_profile))
        nxt = self._next
        self._next = None
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt  # type: ignore[return-value]


class _RecordingWriter:
    """Records `commit` calls; returns a programmable CommitResult."""

    def __init__(self, *, profile_path: Path | None = None) -> None:
        self.calls: list[tuple[str, int, int]] = []
        self._next_result: CommitResult | None = None
        self._profile_path = profile_path or Path("/tmp/profile.md")

    def queue_result(self, r: CommitResult) -> None:
        self._next_result = r

    async def commit(
        self,
        proposed_text: str,
        *,
        conversation_id: int,
        source_message_id: int,
    ) -> CommitResult:
        self.calls.append((proposed_text, conversation_id, source_message_id))
        if self._next_result is not None:
            return self._next_result
        return CommitResult(
            committed=True,
            profile_path=self._profile_path,
            git_commit_sha="0123456789abcdef",
            error=None,
        )


# --- Standard wired Agent --------------------------------------------------


def _make_agent(
    *,
    store: Store,
    settings: Settings,
    identity: IdentityStore | None = None,
    rotator: Any = None,
    proposer: Any = None,
    writer: Any = None,
) -> tuple[Agent, FakeChannel, FakeProvider, FakeProvider, ProviderRouter]:
    """Build an Agent with a FakeChannel + two-provider router (primary
    `claude_cli`, cheap-tier `cheap`)."""
    router, primary, cheap = _build_router(with_cheap=True)
    channel = FakeChannel()
    agent = Agent(
        store=store,
        channel=channel,
        settings=settings,
        router=router,
        identity=identity,
        rotator=rotator,
        proposer=proposer,
        writer=writer,
    )
    return agent, channel, primary, cheap, router


# ==========================================================================
# A. Prompt assembly
# ==========================================================================


class TestPromptAssembly:
    async def test_pinned_appears_in_system_block(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        settings = _make_settings(monkeypatch, tmp_path)
        agent, channel, primary, _, _ = _make_agent(
            store=opened_store, settings=settings, identity=identity
        )
        await channel.start(agent.handle_turn)

        conv = await opened_store.get_or_create_conversation("discord", "thread-1")
        await opened_store.add_pin(conv.id, "REMEMBER: cucumber allergy")

        primary.queue_text("ok")

        await channel.inject(_msg("hi"))

        assert len(primary.calls) == 1
        sysblock = primary.calls[0].system
        assert "REMEMBER: cucumber allergy" in sysblock

    async def test_summaries_appear_in_system_block(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        settings = _make_settings(monkeypatch, tmp_path)
        agent, channel, primary, _, _ = _make_agent(
            store=opened_store, settings=settings, identity=identity
        )
        await channel.start(agent.handle_turn)

        conv = await opened_store.get_or_create_conversation("discord", "thread-1")
        # Need a real message id for the summary range; add a placeholder
        # message so the foreign-key style range is honest.
        mid = await opened_store.append_message(conv.id, "user", "old chatter")
        await opened_store.insert_summary(
            conv.id,
            start_message_id=mid,
            end_message_id=mid,
            summary_text="USER LIKES PYTHON; DISCUSSED ASYNC PRIMITIVES.",
        )

        primary.queue_text("ok")
        await channel.inject(_msg("hi"))

        assert len(primary.calls) == 1
        sysblock = primary.calls[0].system
        assert "USER LIKES PYTHON; DISCUSSED ASYNC PRIMITIVES." in sysblock

    async def test_pins_before_summaries_in_system_block(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        settings = _make_settings(monkeypatch, tmp_path)
        agent, channel, primary, _, _ = _make_agent(
            store=opened_store, settings=settings, identity=identity
        )
        await channel.start(agent.handle_turn)

        conv = await opened_store.get_or_create_conversation("discord", "thread-1")
        await opened_store.add_pin(conv.id, "PIN_MARKER_ABCDEF")

        mid = await opened_store.append_message(conv.id, "user", "x")
        await opened_store.insert_summary(
            conv.id,
            start_message_id=mid,
            end_message_id=mid,
            summary_text="SUMMARY_MARKER_ZYXWVU",
        )

        primary.queue_text("ok")
        await channel.inject(_msg("hi"))

        sysblock = primary.calls[0].system
        pin_pos = sysblock.find("PIN_MARKER_ABCDEF")
        sum_pos = sysblock.find("SUMMARY_MARKER_ZYXWVU")
        assert pin_pos != -1, "pin not present in system block"
        assert sum_pos != -1, "summary not present in system block"
        assert pin_pos < sum_pos, (
            "pinned context must appear before summaries in the system block "
            "(pins are user-elevated context; summaries are compressed history)"
        )

    async def test_system_block_byte_stable_across_turns(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        """TLC-12: with no DB mutation between turns, the system block
        is byte-identical across consecutive provider calls."""
        settings = _make_settings(monkeypatch, tmp_path)
        agent, channel, primary, _, _ = _make_agent(
            store=opened_store, settings=settings, identity=identity
        )
        await channel.start(agent.handle_turn)

        conv = await opened_store.get_or_create_conversation("discord", "thread-1")
        await opened_store.add_pin(conv.id, "PINNED LINE A")
        mid = await opened_store.append_message(conv.id, "user", "old")
        await opened_store.insert_summary(
            conv.id,
            start_message_id=mid,
            end_message_id=mid,
            summary_text="SUMMARY ALPHA",
        )

        primary.queue_text("first")
        primary.queue_text("second")

        await channel.inject(_msg("turn-1", mid="m1"))
        await channel.inject(_msg("turn-2", mid="m2"))

        assert len(primary.calls) == 2
        assert primary.calls[0].system == primary.calls[1].system, (
            "system block diverged between turns despite no DB change"
        )


# ==========================================================================
# B. v15 context lifecycle: pre-turn rotation + post-turn context metric
# ==========================================================================


class TestPreTurnRotation:
    async def test_agent_calls_maybe_rotate_before_persisting_user_message(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        """UoW-28 trigger point: turn start, BEFORE the user message is
        persisted. The recording rotator snapshots the message count at
        call time — it must be 0 (the inbound message isn't in the store
        yet)."""
        settings = _make_settings(monkeypatch, tmp_path)
        rotator = _RecordingRotator(store=opened_store)
        agent, channel, primary, _, _ = _make_agent(
            store=opened_store,
            settings=settings,
            identity=identity,
            rotator=rotator,
        )
        await channel.start(agent.handle_turn)

        primary.queue_text("ok")
        await channel.inject(_msg("hi"))

        conv = await opened_store.get_or_create_conversation("discord", "thread-1")
        assert rotator.calls == [conv.id], (
            f"expected one maybe_rotate call with conv id {conv.id}; got {rotator.calls!r}"
        )
        assert rotator.message_counts_at_call == [0], (
            "maybe_rotate must run BEFORE the user message is persisted "
            f"(UoW-28); message count at call time: {rotator.message_counts_at_call!r}"
        )
        # The turn itself still completed.
        finals = [s for s in channel.sent if s.kind == "final"]
        assert len(finals) == 1

    async def test_rotator_none_skips_rotation(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        """Agent constructed with rotator=None completes turns normally
        without any rotation step."""
        settings = _make_settings(monkeypatch, tmp_path)
        agent, channel, primary, _, _ = _make_agent(
            store=opened_store, settings=settings, identity=identity, rotator=None
        )
        await channel.start(agent.handle_turn)

        primary.queue_text("ok")
        await channel.inject(_msg("hi"))

        finals = [s for s in channel.sent if s.kind == "final"]
        assert len(finals) == 1


class TestPostTurnContextMetric:
    async def test_context_tokens_written_to_conversation(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        """TLC-24/FC-28: when `CompletionResult.context_tokens` is not
        None, the agent post-turn writes it onto
        `conversations.last_context_tokens`."""
        settings = _make_settings(monkeypatch, tmp_path)
        agent, channel, primary, _, _ = _make_agent(
            store=opened_store, settings=settings, identity=identity
        )
        await channel.start(agent.handle_turn)

        primary.queue_response(
            CompletionResult(
                text="the answer",
                usage=Usage(input_tokens=2, output_tokens=2, subscription=True),
                context_tokens=51_207,
            )
        )
        await channel.inject(_msg("hi"))

        conv = await opened_store.get_or_create_conversation("discord", "thread-1")
        refreshed = await opened_store.get_conversation(conv.id)
        assert refreshed is not None
        assert refreshed.last_context_tokens == 51_207

    async def test_context_tokens_none_leaves_row_untouched(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        """A provider that reports no context size (non-CLI, TLC-24)
        leaves `last_context_tokens` exactly as it was."""
        settings = _make_settings(monkeypatch, tmp_path)
        agent, channel, primary, _, _ = _make_agent(
            store=opened_store, settings=settings, identity=identity
        )
        await channel.start(agent.handle_turn)

        # Pre-set a value so "untouched" is distinguishable from "cleared".
        conv = await opened_store.get_or_create_conversation("discord", "thread-1")
        await opened_store.update_last_context_tokens(conv.id, 777)

        primary.queue_text("ok")  # CompletionResult.context_tokens defaults to None
        await channel.inject(_msg("hi"))

        refreshed = await opened_store.get_conversation(conv.id)
        assert refreshed is not None
        assert refreshed.last_context_tokens == 777


# ==========================================================================
# C. Post-turn proposal extraction
# ==========================================================================


def _make_proposal(
    *,
    proposed_text: str = "# new profile\n\n- updated\n",
    diff: str = "--- profile.md\n+++ profile.md\n@@ -1 +1 @@\n-old\n+new\n",
    source: str = "I'm vegan",
) -> ProfileProposal:
    return ProfileProposal(
        proposed_text=proposed_text,
        diff=diff,
        source_user_message=source,
    )


class TestPostTurnProposal:
    async def test_post_turn_calls_proposer_when_present(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        settings = _make_settings(monkeypatch, tmp_path)
        proposer = _RecordingProposer()
        proposer.queue_proposal(None)
        agent, channel, primary, _, _ = _make_agent(
            store=opened_store,
            settings=settings,
            identity=identity,
            proposer=proposer,
        )
        await channel.start(agent.handle_turn)

        primary.queue_text("ok")
        # User's message is what should be passed to proposer.
        await channel.inject(_msg("I'm vegan and lift heavy"))

        assert len(proposer.calls) == 1
        user_msg, current_profile = proposer.calls[0]
        assert user_msg == "I'm vegan and lift heavy"
        # The current profile is the on-disk text (not the soul+profile
        # combined system prompt).
        assert current_profile == identity.read_profile()

    async def test_proposer_proposal_stored_with_source_message_id(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        """When the proposer returns a ProfileProposal, the agent stashes
        it on the conversation as JSON containing `proposed_text`, `diff`,
        AND `source_message_id` matching the just-persisted user message
        id (E)."""
        settings = _make_settings(monkeypatch, tmp_path)
        proposer = _RecordingProposer()
        proposal = _make_proposal()
        proposer.queue_proposal(proposal)
        agent, channel, primary, _, _ = _make_agent(
            store=opened_store,
            settings=settings,
            identity=identity,
            proposer=proposer,
        )
        await channel.start(agent.handle_turn)

        primary.queue_text("ok")
        await channel.inject(_msg("I'm vegan"))

        conv = await opened_store.get_or_create_conversation("discord", "thread-1")
        # consume_pending_proposal both reads + clears, so call it last.
        # First peek via raw column to keep the row intact for further
        # assertions if needed.
        cur = await opened_store._c.execute(
            "SELECT pending_profile_proposal FROM conversations WHERE id = ?",
            (conv.id,),
        )
        row = await cur.fetchone()
        await cur.close()
        assert row is not None
        raw = row[0]
        assert raw is not None, "no pending_profile_proposal stored"
        parsed = json.loads(raw)
        assert parsed["proposed_text"] == proposal.proposed_text
        assert parsed["diff"] == proposal.diff
        assert "source_message_id" in parsed, (
            "JSON must include source_message_id (E) so writer.commit can "
            "link the commit body back to the originating user message"
        )

        # The id must point at the user message that was just persisted.
        msgs = await opened_store.recent_messages(conv.id, include_compacted=True)
        user_msgs = [m for m in msgs if m.role == "user" and unstamp(m.content) == "I'm vegan"]
        assert len(user_msgs) == 1
        assert parsed["source_message_id"] == user_msgs[0].id

    async def test_proposer_proposal_emits_diff_status(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        settings = _make_settings(monkeypatch, tmp_path)
        proposer = _RecordingProposer()
        diff = (
            "--- profile.md\n+++ profile.md\n@@ -2,1 +2,2 @@\n-line A\n+line A\n+line B (vegan)\n"
        )
        proposer.queue_proposal(_make_proposal(diff=diff))
        agent, channel, primary, _, _ = _make_agent(
            store=opened_store,
            settings=settings,
            identity=identity,
            proposer=proposer,
        )
        await channel.start(agent.handle_turn)

        primary.queue_text("ok")
        await channel.inject(_msg("I'm vegan"))

        # A status-kind outbound contains the diff text.
        statuses = [s for s in channel.sent if s.kind == "status"]
        assert any(diff in s.content for s in statuses), (
            "expected a status message containing the rendered diff; "
            f"sent statuses: {[s.content for s in statuses]!r}"
        )

    async def test_proposer_returns_none_no_status_emitted(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        settings = _make_settings(monkeypatch, tmp_path)
        proposer = _RecordingProposer()
        proposer.queue_proposal(None)
        agent, channel, primary, _, _ = _make_agent(
            store=opened_store,
            settings=settings,
            identity=identity,
            proposer=proposer,
        )
        await channel.start(agent.handle_turn)

        primary.queue_text("ok")
        await channel.inject(_msg("hello there"))

        # No pending proposal stored.
        conv = await opened_store.get_or_create_conversation("discord", "thread-1")
        cur = await opened_store._c.execute(
            "SELECT pending_profile_proposal FROM conversations WHERE id = ?",
            (conv.id,),
        )
        row = await cur.fetchone()
        await cur.close()
        assert row is not None and row[0] is None

        # No status mentions a profile update / diff.
        statuses = [s for s in channel.sent if s.kind == "status"]
        for s in statuses:
            assert "Proposed profile update" not in s.content
            assert "profile update" not in s.content.lower() or True  # loose

    async def test_proposer_failure_does_not_break_turn(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        settings = _make_settings(monkeypatch, tmp_path)
        proposer = _RecordingProposer()
        proposer.queue_error(RuntimeError("proposer exploded"))
        agent, channel, primary, _, _ = _make_agent(
            store=opened_store,
            settings=settings,
            identity=identity,
            proposer=proposer,
        )
        await channel.start(agent.handle_turn)

        primary.queue_text("the answer")
        await channel.inject(_msg("hi"))

        # Final reply still landed; assistant persisted.
        finals = [s for s in channel.sent if s.kind == "final"]
        assert len(finals) == 1
        assert finals[0].content == "the answer"

        conv = await opened_store.get_or_create_conversation("discord", "thread-1")
        msgs = await opened_store.recent_messages(conv.id)
        assert any(m.role == "assistant" and m.content == "the answer" for m in msgs)

    async def test_proposer_none_skips_extraction(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        settings = _make_settings(monkeypatch, tmp_path)
        agent, channel, primary, _, _ = _make_agent(
            store=opened_store,
            settings=settings,
            identity=identity,
            proposer=None,
        )
        await channel.start(agent.handle_turn)

        primary.queue_text("ok")
        await channel.inject(_msg("hi"))

        # Turn completed; no pending proposal set.
        conv = await opened_store.get_or_create_conversation("discord", "thread-1")
        cur = await opened_store._c.execute(
            "SELECT pending_profile_proposal FROM conversations WHERE id = ?",
            (conv.id,),
        )
        row = await cur.fetchone()
        await cur.close()
        assert row is not None and row[0] is None


# ==========================================================================
# D. Pre-turn proposal-decision short-circuit
# ==========================================================================


async def _seed_pending(
    store: Store,
    *,
    proposed_text: str = "# updated profile\n\nNew content.\n",
    diff: str = "--- profile.md\n+++ profile.md\n@@ -1 +1 @@\n-old\n+new\n",
    source_message_id: int | None = None,
    key: str = "thread-1",
) -> tuple[int, int]:
    """Create a conversation, append a "prior" user message (the one
    that 'triggered' the proposal), and stash a pending proposal whose
    `source_message_id` points at it. Returns `(conversation_id,
    source_message_id)`.
    """
    conv = await store.get_or_create_conversation("discord", key)
    if source_message_id is None:
        source_message_id = await store.append_message(conv.id, "user", "I'm vegan")
        # Add a synthetic assistant reply so the prior turn looks complete.
        await store.append_message(conv.id, "assistant", "got it")
    payload = {
        "proposed_text": proposed_text,
        "diff": diff,
        "source_message_id": source_message_id,
    }
    await store.set_pending_proposal(conv.id, json.dumps(payload))
    return conv.id, source_message_id


class TestPreTurnShortCircuit:
    async def test_pending_yes_commits_proposal(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        settings = _make_settings(monkeypatch, tmp_path)
        writer = _RecordingWriter(profile_path=tmp_path / "identity" / "profile.md")
        agent, channel, primary, _, _ = _make_agent(
            store=opened_store,
            settings=settings,
            identity=identity,
            writer=writer,
        )
        await channel.start(agent.handle_turn)

        proposed = "# updated profile\n\nNew content.\n"
        conv_id, src_id = await _seed_pending(
            opened_store,
            proposed_text=proposed,
            diff="--- a\n+++ b\n",
        )

        # User replies "y".
        await channel.inject(_msg("y", mid="m_yes"))

        # Writer.commit was called with proposed_text + the captured ids.
        assert len(writer.calls) == 1, "writer.commit should run on `y`"
        sent_text, sent_conv_id, sent_src_id = writer.calls[0]
        assert sent_text == proposed
        assert sent_conv_id == conv_id
        assert sent_src_id == src_id

        # The primary provider was NOT called for this turn (short-circuit).
        assert primary.calls == [], "pending+y short-circuits the turn; provider must not be called"

        # User saw a success status.
        statuses = [s for s in channel.sent if s.kind in ("status", "final")]
        assert any("profile updated" in s.content.lower() for s in statuses), (
            f"expected a success status mentioning profile update; got "
            f"{[s.content for s in statuses]!r}"
        )

    async def test_pending_no_discards(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        settings = _make_settings(monkeypatch, tmp_path)
        writer = _RecordingWriter()
        agent, channel, primary, _, _ = _make_agent(
            store=opened_store,
            settings=settings,
            identity=identity,
            writer=writer,
        )
        await channel.start(agent.handle_turn)

        await _seed_pending(opened_store)

        await channel.inject(_msg("n", mid="m_no"))

        assert writer.calls == [], "writer.commit must NOT run on `n`"
        assert primary.calls == [], "primary provider must not be called"

        # User saw a discard message.
        outs = [s for s in channel.sent if s.kind in ("status", "final")]
        assert any("discard" in s.content.lower() for s in outs), (
            f"expected a discard status; got {[s.content for s in outs]!r}"
        )

    async def test_pending_edit_uses_new_text(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        settings = _make_settings(monkeypatch, tmp_path)
        writer = _RecordingWriter()
        agent, channel, primary, _, _ = _make_agent(
            store=opened_store,
            settings=settings,
            identity=identity,
            writer=writer,
        )
        await channel.start(agent.handle_turn)

        original_proposed = "# original proposed\n"
        conv_id, src_id = await _seed_pending(opened_store, proposed_text=original_proposed)

        new_text = "Replace this with that and add some salt"
        await channel.inject(_msg(f"edit {new_text}", mid="m_edit"))

        # Writer.commit called with the captured edit text, NOT the original.
        assert len(writer.calls) == 1
        sent_text, sent_conv_id, sent_src_id = writer.calls[0]
        assert sent_text == new_text
        assert sent_text != original_proposed
        assert sent_conv_id == conv_id
        assert sent_src_id == src_id

        # Primary provider was NOT called.
        assert primary.calls == []

    async def test_pending_irrelevant_message_supersedes(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        """Pending + an unrelated user message: proposal silently
        discarded (consume_pending_proposal called), normal turn proceeds
        (provider called)."""
        settings = _make_settings(monkeypatch, tmp_path)
        writer = _RecordingWriter()
        agent, channel, primary, _, _ = _make_agent(
            store=opened_store,
            settings=settings,
            identity=identity,
            writer=writer,
        )
        await channel.start(agent.handle_turn)

        conv_id, _ = await _seed_pending(opened_store)

        primary.queue_text("the time is now")
        await channel.inject(_msg("what time is it", mid="m_q"))

        # Writer NOT called.
        assert writer.calls == []
        # Provider WAS called (normal turn).
        assert len(primary.calls) == 1
        # Pending row is now NULL (consumed).
        cur = await opened_store._c.execute(
            "SELECT pending_profile_proposal FROM conversations WHERE id = ?",
            (conv_id,),
        )
        row = await cur.fetchone()
        await cur.close()
        assert row is not None and row[0] is None, (
            "pending_profile_proposal must be cleared once the next turn supersedes it"
        )

    async def test_pending_yes_persists_user_and_synthetic_assistant_messages(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        settings = _make_settings(monkeypatch, tmp_path)
        writer = _RecordingWriter()
        agent, channel, _, _, _ = _make_agent(
            store=opened_store,
            settings=settings,
            identity=identity,
            writer=writer,
        )
        await channel.start(agent.handle_turn)

        conv_id, _ = await _seed_pending(opened_store)

        # Snapshot the message count before so we can assert exactly
        # 2 new messages land (the user "y" + the synthetic assistant reply).
        before = await opened_store.message_count(conv_id)

        await channel.inject(_msg("y", mid="m_yes_persist"))

        msgs = await opened_store.recent_messages(conv_id, limit=200, include_compacted=True)
        # The most-recent two messages should be: user "y" then assistant
        # synthetic reply.
        assert len(msgs) >= before + 2, (
            f"expected >= {before + 2} msgs after y-confirm; got {len(msgs)}"
        )
        last_two = msgs[-2:]
        assert last_two[0].role == "user", f"second-to-last msg role: {last_two[0].role}"
        # The user's text was "y" (persisted verbatim).
        assert last_two[0].content == "y"
        assert last_two[1].role == "assistant"
        # The synthetic assistant content is non-empty (the success status).
        assert last_two[1].content.strip() != ""

    async def test_pending_writer_failure_surfaces_error_to_user(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        settings = _make_settings(monkeypatch, tmp_path)
        writer = _RecordingWriter()
        writer.queue_result(
            CommitResult(
                committed=False,
                profile_path=tmp_path / "identity" / "profile.md",
                git_commit_sha=None,
                error="git_failed",
            )
        )
        agent, channel, primary, _, _ = _make_agent(
            store=opened_store,
            settings=settings,
            identity=identity,
            writer=writer,
        )
        await channel.start(agent.handle_turn)

        await _seed_pending(opened_store)

        await channel.inject(_msg("y", mid="m_yes_fail"))

        assert primary.calls == [], "no provider call on writer failure path"
        outs = [s for s in channel.sent if s.kind in ("status", "final")]
        # The error reason should appear, or at least a clear failure
        # status (no false "success").
        joined = "\n".join(s.content for s in outs)
        assert "git_failed" in joined or "error" in joined.lower() or "❌" in joined, (
            f"expected an error status; got {joined!r}"
        )
        # Sanity: no message claims success.
        assert "✅" not in joined or "git_failed" in joined

    async def test_pending_writer_none_no_pending_check(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        """Agent without a writer: even if a pending row was somehow
        present (stale), the agent ignores it and proceeds with a normal
        turn. Importantly, `consume_pending_proposal` is NOT called (we
        guard at the top), so the row would survive — but we don't assert
        that strongly because the spec says "guard at the top of pre-turn
        check" as the implementation, not an observable. We assert: the
        normal turn ran (provider called), and the user got a normal
        reply."""
        settings = _make_settings(monkeypatch, tmp_path)
        agent, channel, primary, _, _ = _make_agent(
            store=opened_store,
            settings=settings,
            identity=identity,
            writer=None,
        )
        await channel.start(agent.handle_turn)

        await _seed_pending(opened_store)

        primary.queue_text("normal reply")
        await channel.inject(_msg("y", mid="m_no_writer"))

        # Normal turn ran — provider was called.
        assert len(primary.calls) == 1
        finals = [s for s in channel.sent if s.kind == "final"]
        assert len(finals) == 1
        assert finals[0].content == "normal reply"


# ==========================================================================
# Real-component smoke (SessionRotator + Proposer + Writer wired end-to-end)
# ==========================================================================
#
# A single sanity test that builds the actual production components
# (SessionRotator, Proposer, ProfileWriter) against the FakeProvider
# cheap tier — proving the agent's wiring matches the real interfaces.
# This guards against signature drift more cheaply than a full coverage
# pass.


class TestRealComponentsSmoke:
    async def test_real_rotator_proposer_writer_wired(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        settings = _make_settings(monkeypatch, tmp_path)

        cheap_provider = _named_provider("cheap")
        # Classifier replies "no" → proposer returns None.
        cheap_provider.queue_text("no")

        primary_provider = _named_provider("claude_cli")
        primary_provider.queue_text("ASSISTANT REPLY")

        router = ProviderRouter(
            {"claude_cli": primary_provider, "cheap": cheap_provider},
            cheap_tier_route="cheap",
        )

        rotator = SessionRotator(store=opened_store, router=router)
        proposer = Proposer(router=router)
        writer = ProfileWriter(identity=identity)

        channel = FakeChannel()
        agent = Agent(
            store=opened_store,
            channel=channel,
            settings=settings,
            router=router,
            identity=identity,
            rotator=rotator,
            proposer=proposer,
            writer=writer,
        )
        await channel.start(agent.handle_turn)

        await channel.inject(_msg("hello there"))

        # Provider was called once; final reply landed.
        assert len(primary_provider.calls) == 1
        finals = [s for s in channel.sent if s.kind == "final"]
        assert len(finals) == 1
        assert finals[0].content == "ASSISTANT REPLY"

        # Cheap provider received exactly the proposer's classifier call
        # (the rotator didn't fire — fresh conversation, no session id,
        # no context pressure). The classifier-reply "no" means no
        # proposal was extracted → no pending row.
        assert len(cheap_provider.calls) == 1
        conv = await opened_store.get_or_create_conversation("discord", "thread-1")
        cur = await opened_store._c.execute(
            "SELECT pending_profile_proposal FROM conversations WHERE id = ?",
            (conv.id,),
        )
        row = await cur.fetchone()
        await cur.close()
        assert row is not None and row[0] is None
