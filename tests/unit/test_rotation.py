"""SessionRotator (v15) — UoW-28 + FC-29 + TLC-25.

Contract surface under test (doc #56, no implementation files read):

- `RotationResult` frozen dataclass: `rotated: bool`,
  `reason: str | None` ("idle_gap" | "cold_cache" | "context_size" |
  "forced" | None),
  `summary_id: int | None`, `skipped_reason: str | None`.
- `SessionRotator(store=..., router=...)` with class constants
  `DEFAULT_IDLE_HOURS = 72`, `DEFAULT_CONTEXT_TOKENS = 60_000`,
  `DEFAULT_COLD_CACHE_MINUTES = 60`, `DEFAULT_COLD_CACHE_TOKENS =
  95_000`; module constant `HANDOFF_PROMPT`.
- `should_rotate(conversation, *, now)` → reason or None.
- `rotate(conversation, *, reason)` / `maybe_rotate(conversation, *, now)`
  never raise out — every failure is a `skipped_reason` ∈
  {under_threshold, nothing_to_rotate, cheap_tier_unavailable,
  provider_error: <T>, empty_summary, mark_compacted_failed: <T>}.
- Auto preconditions: `claude_cli_session_id` set AND (idle gap >
  rotation_idle_hours OR last_context_tokens > rotation_context_tokens
  OR (gap > rotation_cold_cache_minutes AND last_context_tokens >
  rotation_cold_cache_tokens)); size triggers need ≥8 active messages;
  thresholds read from `runtime_config` per check, invalid → defaults.
- Forced preconditions reduce to ≥2 non-compacted messages.
- Success: prior `summaries` rows deleted, exactly one new handoff row
  (span = full non-compacted range), all non-compacted messages marked
  compacted, `claude_cli_session_id` cleared, `last_context_tokens`
  cleared.
- Failure atomicity: cheap tier unavailable / provider error / empty
  summary → no writes at all; `mark_messages_compacted` failure → the
  just-inserted summary row is deleted (all-or-nothing).
- Handoff input: the prior brief (if any) plus the whole non-compacted
  transcript, newest-first within a char budget (omission marker for
  older overflow).
- Handoff text >3000 chars truncated deterministically.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ember.context.rotation import (
    HANDOFF_PROMPT,
    RotationResult,
    SessionRotator,
)
from ember.errors import ConfigError, ProviderError
from ember.providers.routing import ProviderRouter
from ember.store.repo import Store
from tests.fakes.fake_provider import FakeProvider

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


# --- Fixtures / helpers ----------------------------------------------------


@pytest.fixture
async def store(tmp_path: Path):
    s = Store(tmp_path / "conversations.db")
    await s.open()
    try:
        yield s
    finally:
        await s.close()


def _named_provider(name: str) -> FakeProvider:
    fp = FakeProvider()
    fp.name = name
    return fp


def make_router(*, with_cheap: bool = True) -> tuple[ProviderRouter, FakeProvider, FakeProvider]:
    """ProviderRouter with a `claude_cli` primary and (optionally) a
    `cheap` provider as the cheap tier. Returns `(router, primary, cheap)`."""
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


def _iso(dt: datetime) -> str:
    """Match the store's strftime('%Y-%m-%dT%H:%M:%fZ') column format."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + ".000Z"


async def _seed(
    store: Store,
    *,
    key: str = "thread-1",
    session: str | None = "sess-1",
    last_active: datetime | None = None,
    n_messages: int = 4,
    context_tokens: int | None = None,
):
    """Create a conversation with `n_messages` alternating messages,
    optional session id / last_active / last_context_tokens, and return
    the freshly-hydrated Conversation plus the message ids."""
    conv = await store.get_or_create_conversation("discord", key)
    msg_ids: list[int] = []
    for i in range(n_messages):
        mid = await store.append_message(
            conv.id,
            "user" if i % 2 == 0 else "assistant",
            f"msg-{i:03d}",
        )
        msg_ids.append(mid)
    if session is not None:
        await store.update_claude_cli_session(conv.id, session)
    if context_tokens is not None:
        await store.update_last_context_tokens(conv.id, context_tokens)
    if last_active is not None:
        await store._c.execute(
            "UPDATE conversations SET last_active = ? WHERE id = ?",
            (_iso(last_active), conv.id),
        )
        await store._c.commit()
    refreshed = await store.get_conversation(conv.id)
    assert refreshed is not None
    return refreshed, msg_ids


async def _assert_no_writes(
    store: Store,
    conv_id: int,
    *,
    n_messages: int,
    session: str | None = "sess-1",
    n_summaries: int = 0,
) -> None:
    """Zero-writes postcondition: summaries untouched, no messages
    flipped to compacted, session id intact."""
    assert len(await store.list_summaries(conv_id)) == n_summaries
    active = await store.recent_messages(conv_id, limit=100)
    assert len(active) == n_messages
    conv = await store.get_conversation(conv_id)
    assert conv is not None
    assert conv.claude_cli_session_id == session


# --- RotationResult shape --------------------------------------------------


class TestRotationResultShape:
    def test_fields(self) -> None:
        r = RotationResult(
            rotated=False,
            reason=None,
            summary_id=None,
            skipped_reason="under_threshold",
        )
        assert r.rotated is False
        assert r.reason is None
        assert r.summary_id is None
        assert r.skipped_reason == "under_threshold"

    def test_rotated_shape(self) -> None:
        r = RotationResult(rotated=True, reason="idle_gap", summary_id=7, skipped_reason=None)
        assert r.rotated is True
        assert r.reason == "idle_gap"
        assert r.summary_id == 7
        assert r.skipped_reason is None

    def test_frozen(self) -> None:
        r = RotationResult(rotated=False, reason=None, summary_id=None, skipped_reason=None)
        with pytest.raises(dataclasses.FrozenInstanceError):
            r.rotated = True  # type: ignore[misc]


# --- Constants (FC-29) -----------------------------------------------------


class TestConstants:
    def test_default_idle_hours(self) -> None:
        assert SessionRotator.DEFAULT_IDLE_HOURS == 72

    def test_default_context_tokens(self) -> None:
        assert SessionRotator.DEFAULT_CONTEXT_TOKENS == 60_000

    def test_cold_cache_defaults(self) -> None:
        assert SessionRotator.DEFAULT_COLD_CACHE_MINUTES == 60
        assert SessionRotator.DEFAULT_COLD_CACHE_TOKENS == 95_000

    def test_handoff_prompt_is_nonempty_module_constant(self) -> None:
        assert isinstance(HANDOFF_PROMPT, str)
        assert HANDOFF_PROMPT.strip() != ""


# --- Auto trigger (should_rotate / maybe_rotate) ---------------------------


class TestAutoTrigger:
    async def test_idle_gap_over_72h_triggers(self, store: Store) -> None:
        conv, _ = await _seed(store, last_active=NOW - timedelta(hours=73))
        router, _, cheap = make_router()
        cheap.queue_text("HANDOFF")
        rot = SessionRotator(store=store, router=router)

        result = await rot.maybe_rotate(conv, now=NOW)

        assert result.rotated is True
        assert result.reason == "idle_gap"
        assert result.skipped_reason is None

    async def test_idle_gap_exactly_72h_does_not_trigger(self, store: Store) -> None:
        """The threshold is strict: `now - last_active > 72h`."""
        conv, _ = await _seed(store, last_active=NOW - timedelta(hours=72))
        router, primary, cheap = make_router()
        rot = SessionRotator(store=store, router=router)

        result = await rot.maybe_rotate(conv, now=NOW)

        assert result.rotated is False
        assert result.skipped_reason == "under_threshold"
        assert cheap.calls == []
        assert primary.calls == []
        await _assert_no_writes(store, conv.id, n_messages=4)

    async def test_idle_gap_under_72h_does_not_trigger(self, store: Store) -> None:
        conv, _ = await _seed(store, last_active=NOW - timedelta(hours=71))
        router, _, cheap = make_router()
        rot = SessionRotator(store=store, router=router)

        result = await rot.maybe_rotate(conv, now=NOW)

        assert result.rotated is False
        assert result.reason is None
        assert result.summary_id is None
        assert result.skipped_reason == "under_threshold"
        assert cheap.calls == []
        await _assert_no_writes(store, conv.id, n_messages=4)

    async def test_context_over_60k_triggers(self, store: Store) -> None:
        conv, _ = await _seed(
            store,
            last_active=NOW - timedelta(minutes=5),
            n_messages=8,
            context_tokens=60_001,
        )
        router, _, cheap = make_router()
        cheap.queue_text("HANDOFF")
        rot = SessionRotator(store=store, router=router)

        result = await rot.maybe_rotate(conv, now=NOW)

        assert result.rotated is True
        assert result.reason == "context_size"

    async def test_context_exactly_60k_does_not_trigger(self, store: Store) -> None:
        """Strict inequality: `last_context_tokens > 60_000`."""
        conv, _ = await _seed(
            store,
            last_active=NOW - timedelta(minutes=5),
            context_tokens=60_000,
        )
        router, _, cheap = make_router()
        rot = SessionRotator(store=store, router=router)

        result = await rot.maybe_rotate(conv, now=NOW)

        assert result.rotated is False
        assert result.skipped_reason == "under_threshold"
        assert cheap.calls == []

    async def test_no_session_id_never_auto_triggers(self, store: Store) -> None:
        """`claude_cli_session_id is None` → nothing to rotate away from;
        auto path must not fire even with a huge idle gap AND a huge
        context number."""
        conv, _ = await _seed(
            store,
            session=None,
            last_active=NOW - timedelta(hours=1000),
            context_tokens=999_999,
        )
        router, primary, cheap = make_router()
        rot = SessionRotator(store=store, router=router)

        result = await rot.maybe_rotate(conv, now=NOW)

        assert result.rotated is False
        assert result.skipped_reason == "under_threshold"
        assert cheap.calls == []
        assert primary.calls == []
        await _assert_no_writes(store, conv.id, n_messages=4, session=None)

    async def test_should_rotate_returns_reason_strings(self, store: Store) -> None:
        router, _, _ = make_router()
        rot = SessionRotator(store=store, router=router)

        idle_conv, _ = await _seed(store, key="k-idle", last_active=NOW - timedelta(hours=100))
        assert await rot.should_rotate(idle_conv, now=NOW) == "idle_gap"

        ctx_conv, _ = await _seed(
            store,
            key="k-ctx",
            last_active=NOW - timedelta(minutes=1),
            n_messages=8,
            context_tokens=70_000,
        )
        assert await rot.should_rotate(ctx_conv, now=NOW) == "context_size"

    async def test_context_churn_guard_holds_below_min_messages(self, store: Store) -> None:
        """v16.2: one heavy tool turn can re-cross the threshold right
        after a rotation — with fewer than MIN_CONTEXT_ROTATION_MESSAGES
        active messages, a context-size rotation is pure loss and must
        not fire (zero writes). Idle-gap is unaffected."""
        conv, _ = await _seed(
            store,
            last_active=NOW - timedelta(minutes=5),
            n_messages=SessionRotator.MIN_CONTEXT_ROTATION_MESSAGES - 1,
            context_tokens=999_999,
        )
        router, _, cheap = make_router()
        rot = SessionRotator(store=store, router=router)

        result = await rot.maybe_rotate(conv, now=NOW)

        assert result.rotated is False
        assert result.skipped_reason == "under_threshold"
        assert cheap.calls == []
        await _assert_no_writes(
            store, conv.id, n_messages=SessionRotator.MIN_CONTEXT_ROTATION_MESSAGES - 1
        )

    async def test_handoff_call_carries_inlined_transcript(self, store: Store) -> None:
        """v16.2: the transcript is inlined into ONE user message —
        the message-list shape silently dropped the whole conversation
        on claude_cli_cheap (fresh calls send only the trailing user
        run). The cheap call must contain actual conversation content."""
        conv, _ = await _seed(
            store,
            last_active=NOW - timedelta(hours=100),
            n_messages=6,
        )
        router, _, cheap = make_router()
        cheap.queue_text("HANDOFF")
        rot = SessionRotator(store=store, router=router)

        result = await rot.maybe_rotate(conv, now=NOW)

        assert result.rotated is True
        assert len(cheap.calls) == 1
        sent = cheap.calls[0].messages
        assert len(sent) == 1
        assert sent[0].role == "user"
        # Every seeded message body appears in the inlined transcript,
        # role-labelled, oldest first.
        for i in range(6):
            assert f"msg-{i:03d}" in sent[0].content
        assert sent[0].content.index("msg-000") < sent[0].content.index("msg-005")
        assert "User: msg-000" in sent[0].content
        assert "Assistant: msg-001" in sent[0].content

        calm_conv, _ = await _seed(store, key="k-calm", last_active=NOW - timedelta(minutes=1))
        assert await rot.should_rotate(calm_conv, now=NOW) is None


# --- runtime_config overrides (UoW-28) -------------------------------------


class TestRuntimeConfigOverrides:
    async def test_rotation_idle_hours_override_lowers_threshold(self, store: Store) -> None:
        await store.set_runtime_config("rotation_idle_hours", "24")
        conv, _ = await _seed(store, last_active=NOW - timedelta(hours=48))
        router, _, cheap = make_router()
        cheap.queue_text("HANDOFF")
        rot = SessionRotator(store=store, router=router)

        result = await rot.maybe_rotate(conv, now=NOW)

        assert result.rotated is True
        assert result.reason == "idle_gap"

    async def test_rotation_idle_hours_accepts_float(self, store: Store) -> None:
        """Values are parsed as int/float — '0.5' hours means a 1h gap
        triggers."""
        await store.set_runtime_config("rotation_idle_hours", "0.5")
        conv, _ = await _seed(store, last_active=NOW - timedelta(hours=1))
        router, _, cheap = make_router()
        cheap.queue_text("HANDOFF")
        rot = SessionRotator(store=store, router=router)

        result = await rot.maybe_rotate(conv, now=NOW)

        assert result.rotated is True

    async def test_rotation_context_tokens_override_lowers_threshold(self, store: Store) -> None:
        await store.set_runtime_config("rotation_context_tokens", "1000")
        conv, _ = await _seed(
            store,
            last_active=NOW - timedelta(minutes=1),
            n_messages=8,
            context_tokens=5000,
        )
        router, _, cheap = make_router()
        cheap.queue_text("HANDOFF")
        rot = SessionRotator(store=store, router=router)

        result = await rot.maybe_rotate(conv, now=NOW)

        assert result.rotated is True
        assert result.reason == "context_size"

    async def test_invalid_idle_hours_falls_back_to_default(self, store: Store) -> None:
        """'banana' is not a number → code default 72h. A 48h gap must
        NOT trigger."""
        await store.set_runtime_config("rotation_idle_hours", "banana")
        conv, _ = await _seed(store, last_active=NOW - timedelta(hours=48))
        router, _, cheap = make_router()
        rot = SessionRotator(store=store, router=router)

        result = await rot.maybe_rotate(conv, now=NOW)

        assert result.rotated is False
        assert result.skipped_reason == "under_threshold"
        assert cheap.calls == []

    async def test_invalid_context_tokens_falls_back_to_default(self, store: Store) -> None:
        await store.set_runtime_config("rotation_context_tokens", "not-a-number")
        conv, _ = await _seed(
            store,
            last_active=NOW - timedelta(minutes=1),
            context_tokens=5000,  # over a bad override, way under the 60k default
        )
        router, _, cheap = make_router()
        rot = SessionRotator(store=store, router=router)

        result = await rot.maybe_rotate(conv, now=NOW)

        assert result.rotated is False
        assert result.skipped_reason == "under_threshold"
        assert cheap.calls == []


# --- Forced rotation preconditions -----------------------------------------


class TestForcedRotation:
    async def test_zero_messages_skips_nothing_to_rotate(self, store: Store) -> None:
        conv, _ = await _seed(store, n_messages=0)
        router, _, cheap = make_router()
        rot = SessionRotator(store=store, router=router)

        result = await rot.rotate(conv, reason="forced")

        assert result.rotated is False
        assert result.summary_id is None
        assert result.skipped_reason == "nothing_to_rotate"
        assert cheap.calls == []
        await _assert_no_writes(store, conv.id, n_messages=0)

    async def test_one_message_skips_nothing_to_rotate(self, store: Store) -> None:
        """Forced preconditions: ≥2 non-compacted messages."""
        conv, _ = await _seed(store, n_messages=1)
        router, _, cheap = make_router()
        rot = SessionRotator(store=store, router=router)

        result = await rot.rotate(conv, reason="forced")

        assert result.rotated is False
        assert result.skipped_reason == "nothing_to_rotate"
        assert cheap.calls == []
        await _assert_no_writes(store, conv.id, n_messages=1)

    async def test_forced_works_without_session_id(self, store: Store) -> None:
        """Forced rotation drops the session-id precondition — only ≥2
        non-compacted messages are required."""
        conv, _ = await _seed(store, session=None, n_messages=2)
        router, _, cheap = make_router()
        cheap.queue_text("FORCED-HANDOFF")
        rot = SessionRotator(store=store, router=router)

        result = await rot.rotate(conv, reason="forced")

        assert result.rotated is True
        assert result.reason == "forced"
        summaries = await store.list_summaries(conv.id)
        assert len(summaries) == 1
        assert summaries[0].summary_text == "FORCED-HANDOFF"

    async def test_repeat_forced_rotation_is_nothing_to_rotate(self, store: Store) -> None:
        """Idempotency: immediately repeating a forced rotation finds
        everything already compacted → nothing_to_rotate, and the
        existing handoff survives untouched."""
        conv, _ = await _seed(store, n_messages=4)
        router, _, cheap = make_router()
        cheap.queue_text("FIRST-HANDOFF")
        rot = SessionRotator(store=store, router=router)

        first = await rot.rotate(conv, reason="forced")
        assert first.rotated is True

        refreshed = await store.get_conversation(conv.id)
        assert refreshed is not None
        second = await rot.rotate(refreshed, reason="forced")

        assert second.rotated is False
        assert second.summary_id is None
        assert second.skipped_reason == "nothing_to_rotate"
        # Only the first rotation's cheap call happened.
        assert len(cheap.calls) == 1
        # The first handoff row is still there, unreplaced.
        summaries = await store.list_summaries(conv.id)
        assert len(summaries) == 1
        assert summaries[0].summary_text == "FIRST-HANDOFF"


# --- Failure modes (all non-fatal, zero writes) ----------------------------


class TestFailureModes:
    async def test_cheap_tier_unavailable(self, store: Store) -> None:
        conv, _ = await _seed(store, n_messages=4)
        router, primary, cheap = make_router(with_cheap=False)
        # Sanity: the cheap tier really is unconfigured.
        with pytest.raises(ConfigError):
            router.resolve_cheap_tier()
        rot = SessionRotator(store=store, router=router)

        result = await rot.rotate(conv, reason="forced")

        assert result.rotated is False
        assert result.summary_id is None
        assert result.skipped_reason == "cheap_tier_unavailable"
        assert primary.calls == []
        assert cheap.calls == []
        await _assert_no_writes(store, conv.id, n_messages=4)

    async def test_provider_error(self, store: Store) -> None:
        conv, _ = await _seed(store, n_messages=4)
        router, _, cheap = make_router()
        cheap.queue_error(ProviderError("boom"))
        rot = SessionRotator(store=store, router=router)

        result = await rot.rotate(conv, reason="forced")

        assert result.rotated is False
        assert result.summary_id is None
        assert result.skipped_reason is not None
        assert result.skipped_reason.startswith("provider_error")
        await _assert_no_writes(store, conv.id, n_messages=4)

    async def test_arbitrary_exception_is_provider_error(self, store: Store) -> None:
        """Any exception from the cheap tier is caught, not just
        ProviderError — advisory posture (FC-29)."""
        conv, _ = await _seed(store, n_messages=4)
        router, _, cheap = make_router()
        cheap.queue_error(RuntimeError("network nope"))
        rot = SessionRotator(store=store, router=router)

        result = await rot.rotate(conv, reason="forced")

        assert result.rotated is False
        assert result.skipped_reason is not None
        assert result.skipped_reason.startswith("provider_error")
        await _assert_no_writes(store, conv.id, n_messages=4)

    async def test_empty_summary(self, store: Store) -> None:
        conv, _ = await _seed(store, n_messages=4)
        router, _, cheap = make_router()
        cheap.queue_text("")
        rot = SessionRotator(store=store, router=router)

        result = await rot.rotate(conv, reason="forced")

        assert result.rotated is False
        assert result.skipped_reason == "empty_summary"
        await _assert_no_writes(store, conv.id, n_messages=4)

    async def test_whitespace_summary_is_empty(self, store: Store) -> None:
        conv, _ = await _seed(store, n_messages=4)
        router, _, cheap = make_router()
        cheap.queue_text("   \n\t  ")
        rot = SessionRotator(store=store, router=router)

        result = await rot.rotate(conv, reason="forced")

        assert result.rotated is False
        assert result.skipped_reason == "empty_summary"
        await _assert_no_writes(store, conv.id, n_messages=4)

    async def test_mark_compacted_failure_rolls_back_summary(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`mark_messages_compacted` raises after the handoff row was
        inserted → the just-inserted summary row must be deleted
        (all-or-nothing, UoW-12 precedent), messages stay active, the
        session id stays intact."""
        conv, _ = await _seed(store, n_messages=4)
        router, _, cheap = make_router()
        cheap.queue_text("ROLLBACK-ME")
        rot = SessionRotator(store=store, router=router)

        async def boom(_ids: list[int]) -> None:
            raise RuntimeError("simulated mark failure")

        monkeypatch.setattr(store, "mark_messages_compacted", boom)

        result = await rot.rotate(conv, reason="forced")

        assert result.rotated is False
        assert result.summary_id is None
        assert result.skipped_reason is not None
        assert result.skipped_reason.startswith("mark_compacted_failed")

        assert await store.list_summaries(conv.id) == [], (
            "handoff row leaked despite mark_messages_compacted failure — "
            "rotation must be all-or-nothing (UoW-28)"
        )
        active = await store.recent_messages(conv.id, limit=100)
        assert len(active) == 4
        refreshed = await store.get_conversation(conv.id)
        assert refreshed is not None
        assert refreshed.claude_cli_session_id == "sess-1"


# --- Successful rotation ---------------------------------------------------


class TestSuccessfulRotation:
    async def test_full_flow_replaces_summaries_and_clears_state(self, store: Store) -> None:
        """Idle-triggered rotation on a conversation that already carries
        an old summary + a measured context: the old summary row is
        deleted, exactly one new handoff row spans the full non-compacted
        range, every non-compacted message flips to compacted, and both
        `claude_cli_session_id` and `last_context_tokens` are cleared."""
        conv, msg_ids = await _seed(
            store,
            n_messages=6,
            last_active=NOW - timedelta(hours=100),
            context_tokens=12_345,
        )
        # A pre-existing (stale) summary from an earlier era.
        await store.insert_summary(
            conv.id,
            start_message_id=msg_ids[0],
            end_message_id=msg_ids[1],
            summary_text="OLD-STALE-SUMMARY",
        )
        router, primary, cheap = make_router()
        cheap.queue_text("THE-NEW-HANDOFF")
        rot = SessionRotator(store=store, router=router)

        result = await rot.maybe_rotate(conv, now=NOW)

        assert result.rotated is True
        assert result.reason == "idle_gap"
        assert result.skipped_reason is None

        # Exactly one summary row: the new handoff (old one deleted).
        summaries = await store.list_summaries(conv.id)
        assert len(summaries) == 1
        s = summaries[0]
        assert s.summary_text == "THE-NEW-HANDOFF"
        assert s.id == result.summary_id
        # Span = full non-compacted range.
        assert s.start_message_id == msg_ids[0]
        assert s.end_message_id == msg_ids[-1]

        # All non-compacted messages flipped.
        assert await store.recent_messages(conv.id, limit=100) == []
        all_msgs = await store.recent_messages(conv.id, limit=100, include_compacted=True)
        assert len(all_msgs) == 6

        # Session + context metric cleared.
        refreshed = await store.get_conversation(conv.id)
        assert refreshed is not None
        assert refreshed.claude_cli_session_id is None
        assert refreshed.last_context_tokens is None

        # Only the cheap tier was called, never the primary.
        assert len(cheap.calls) == 1
        assert primary.calls == []

    async def test_handoff_input_covers_full_range(self, store: Store) -> None:
        """35 non-compacted messages: the whole range (not just a tail)
        feeds the cheap-tier call, ALL 35 are marked compacted and the
        summary spans the full range."""
        conv, msg_ids = await _seed(store, n_messages=35)
        router, _, cheap = make_router()
        cheap.queue_text("BIG-HANDOFF")
        rot = SessionRotator(store=store, router=router)

        result = await rot.rotate(conv, reason="forced")
        assert result.rotated is True

        assert len(cheap.calls) == 1
        joined = "\n".join(m.content for m in cheap.calls[0].messages)
        for i in range(35):
            assert f"msg-{i:03d}" in joined
        assert "omitted" not in joined

        # But the compaction sweep covers everything.
        assert await store.recent_messages(conv.id, limit=100) == []
        summaries = await store.list_summaries(conv.id)
        assert len(summaries) == 1
        assert summaries[0].start_message_id == msg_ids[0]
        assert summaries[0].end_message_id == msg_ids[-1]

    async def test_second_rotation_supersedes_first_handoff(self, store: Store) -> None:
        """After a rotation, two new messages + another forced rotation
        → the first handoff is replaced (still exactly one row) and the
        new span covers only the newly-compacted messages."""
        conv, _ = await _seed(store, n_messages=4)
        router, _, cheap = make_router()
        cheap.queue_text("HANDOFF-ONE")
        cheap.queue_text("HANDOFF-TWO")
        rot = SessionRotator(store=store, router=router)

        first = await rot.rotate(conv, reason="forced")
        assert first.rotated is True

        new_a = await store.append_message(conv.id, "user", "fresh question")
        new_b = await store.append_message(conv.id, "assistant", "fresh answer")

        refreshed = await store.get_conversation(conv.id)
        assert refreshed is not None
        second = await rot.rotate(refreshed, reason="forced")
        assert second.rotated is True

        summaries = await store.list_summaries(conv.id)
        assert len(summaries) == 1
        assert summaries[0].summary_text == "HANDOFF-TWO"
        assert summaries[0].start_message_id == new_a
        assert summaries[0].end_message_id == new_b

    async def test_handoff_over_3000_chars_truncated_deterministically(self, store: Store) -> None:
        long_text = "".join(f"chunk-{i:04d} " for i in range(400))  # 4400 chars
        assert len(long_text) > 3000

        router, _, cheap = make_router()
        rot = SessionRotator(store=store, router=router)

        stored: list[str] = []
        for key in ("trunc-a", "trunc-b"):
            conv, _ = await _seed(store, key=key, n_messages=2)
            cheap.queue_text(long_text)
            result = await rot.rotate(conv, reason="forced")
            assert result.rotated is True
            summaries = await store.list_summaries(conv.id)
            assert len(summaries) == 1
            stored.append(summaries[0].summary_text)

        for text in stored:
            assert len(text) <= 3000
            # Truncation keeps the head of the model's output.
            assert text.startswith(long_text[:100])
        # Deterministic: identical input → byte-identical stored handoff.
        assert stored[0] == stored[1]

    async def test_cheap_tier_model_passed_through(self, store: Store) -> None:
        """A cheap-tier route with a model resolves to `model=` on the
        provider call (FC-10 precedent)."""
        conv, _ = await _seed(store, n_messages=4)
        primary = _named_provider("claude_cli")
        cheap = _named_provider("cheap")
        router = ProviderRouter(
            {"claude_cli": primary, "cheap": cheap},
            cheap_tier_route="cheap:gemini-2.5-flash",
        )
        cheap.queue_text("HANDOFF")
        rot = SessionRotator(store=store, router=router)

        result = await rot.rotate(conv, reason="forced")

        assert result.rotated is True
        assert len(cheap.calls) == 1
        assert cheap.calls[0].model == "gemini-2.5-flash"


# --- Cold-cache trigger ----------------------------------------------------


class TestColdCacheTrigger:
    """Rotate when the prompt cache has expired (gap > 60m) AND the
    context is already past the cold-cache floor (> 95k): the next turn
    would otherwise re-write the whole context at cache-write price.
    The size threshold is raised to 130k here to mirror production."""

    async def _rot(self, store: Store) -> tuple[SessionRotator, FakeProvider]:
        await store.set_runtime_config("rotation_context_tokens", "130000")
        router, _, cheap = make_router()
        return SessionRotator(store=store, router=router), cheap

    async def test_cold_and_large_triggers(self, store: Store) -> None:
        rot, cheap = await self._rot(store)
        cheap.queue_text("HANDOFF")
        conv, _ = await _seed(
            store,
            last_active=NOW - timedelta(minutes=61),
            n_messages=8,
            context_tokens=95_001,
        )

        result = await rot.maybe_rotate(conv, now=NOW)

        assert result.rotated is True
        assert result.reason == "cold_cache"

    async def test_warm_cache_does_not_trigger(self, store: Store) -> None:
        """Under the size threshold with a warm cache: carrying the
        context is cheap (cache reads), so no rotation."""
        rot, cheap = await self._rot(store)
        conv, _ = await _seed(
            store,
            last_active=NOW - timedelta(minutes=59),
            n_messages=8,
            context_tokens=120_000,
        )

        assert await rot.should_rotate(conv, now=NOW) is None
        assert cheap.calls == []

    async def test_cold_but_small_does_not_trigger(self, store: Store) -> None:
        """Strict inequality on the floor: exactly 95k stays."""
        rot, _ = await self._rot(store)
        conv, _ = await _seed(
            store,
            last_active=NOW - timedelta(hours=5),
            n_messages=8,
            context_tokens=95_000,
        )

        assert await rot.should_rotate(conv, now=NOW) is None

    async def test_cold_with_unmeasured_context_does_not_trigger(self, store: Store) -> None:
        rot, _ = await self._rot(store)
        conv, _ = await _seed(store, last_active=NOW - timedelta(hours=5), n_messages=8)

        assert await rot.should_rotate(conv, now=NOW) is None

    async def test_churn_guard_applies(self, store: Store) -> None:
        rot, cheap = await self._rot(store)
        conv, _ = await _seed(
            store,
            last_active=NOW - timedelta(hours=5),
            n_messages=SessionRotator.MIN_CONTEXT_ROTATION_MESSAGES - 1,
            context_tokens=120_000,
        )

        result = await rot.maybe_rotate(conv, now=NOW)

        assert result.rotated is False
        assert result.skipped_reason == "under_threshold"
        assert cheap.calls == []

    async def test_idle_gap_wins_over_cold_cache(self, store: Store) -> None:
        rot, _ = await self._rot(store)
        conv, _ = await _seed(
            store,
            last_active=NOW - timedelta(hours=100),
            n_messages=8,
            context_tokens=120_000,
        )

        assert await rot.should_rotate(conv, now=NOW) == "idle_gap"

    async def test_overrides_are_honored(self, store: Store) -> None:
        rot, _ = await self._rot(store)
        await store.set_runtime_config("rotation_cold_cache_minutes", "10")
        await store.set_runtime_config("rotation_cold_cache_tokens", "50000")
        conv, _ = await _seed(
            store,
            last_active=NOW - timedelta(minutes=11),
            n_messages=8,
            context_tokens=50_001,
        )

        assert await rot.should_rotate(conv, now=NOW) == "cold_cache"

    async def test_invalid_overrides_fall_back_to_defaults(self, store: Store) -> None:
        rot, _ = await self._rot(store)
        await store.set_runtime_config("rotation_cold_cache_minutes", "soon")
        await store.set_runtime_config("rotation_cold_cache_tokens", "lots")
        conv, _ = await _seed(
            store,
            last_active=NOW - timedelta(minutes=30),
            n_messages=8,
            context_tokens=120_000,
        )

        assert await rot.should_rotate(conv, now=NOW) is None


# --- Cumulative handoff ----------------------------------------------------


class TestCumulativeHandoff:
    async def test_prior_brief_fed_into_next_handoff(self, store: Store) -> None:
        """The previous brief is the only record of the turns it
        condensed; the next handoff must see it so the chain doesn't
        forget everything before its own window."""
        conv, _ = await _seed(store, n_messages=4)
        router, _, cheap = make_router()
        cheap.queue_text("HANDOFF-ONE: decided X")
        cheap.queue_text("HANDOFF-TWO")
        rot = SessionRotator(store=store, router=router)

        assert (await rot.rotate(conv, reason="forced")).rotated is True
        await store.append_message(conv.id, "user", "fresh question")
        await store.append_message(conv.id, "assistant", "fresh answer")
        refreshed = await store.get_conversation(conv.id)
        assert refreshed is not None
        assert (await rot.rotate(refreshed, reason="forced")).rotated is True

        first, second = (c.messages[0].content for c in cheap.calls)
        assert "previous condensation" not in first
        assert "HANDOFF-ONE: decided X" in second
        assert second.index("HANDOFF-ONE") < second.index("fresh question")
        # Only the newly-active messages are in the second transcript.
        assert "msg-000" not in second

    async def test_transcript_budget_keeps_newest(self, store: Store) -> None:
        """Past the char budget the newest messages are kept and the
        older overflow collapses to one omission marker — but every
        message is still compacted and spanned."""
        conv = await store.get_or_create_conversation("discord", "big")
        ids = []
        for i in range(200):
            body = f"m{i:03d} " + "x" * 2000  # sliced to 1500 per message
            ids.append(
                await store.append_message(conv.id, "user" if i % 2 == 0 else "assistant", body)
            )
        router, _, cheap = make_router()
        cheap.queue_text("HANDOFF")
        rot = SessionRotator(store=store, router=router)

        result = await rot.rotate(conv, reason="forced")

        assert result.rotated is True
        sent = cheap.calls[0].messages[0].content
        assert "m199 " in sent
        assert "m000 " not in sent
        assert "older messages omitted]" in sent
        assert sent.index("older messages omitted]") < sent.index("m199 ")
        assert len(sent) < 130_000
        assert await store.recent_messages(conv.id, limit=1000) == []
        s = (await store.list_summaries(conv.id))[0]
        assert (s.start_message_id, s.end_message_id) == (ids[0], ids[-1])
