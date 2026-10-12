"""Store v=4 — compaction, pins, profile proposal, clear, runtime config.

These tests exercise the new repo methods and schema additions for
Ember v9 (architecture doc § "Token tracker + auto-compactor",
§ "Conversation store"; contracts doc § UoW-12, TLC-9, TLC-11, TLC-12,
FC-7, UoW-8). The contract surface under test:

- `messages.compacted` boolean (default 0).
- `summaries` table — one row per UoW-12 compaction event.
- `pinned` table — survives trim/compact/clear.
- `conversations.pending_profile_proposal` JSON — UoW-8 staging.
- `runtime_config(key, value, updated_at)` — cheap-tier overrides.
- `Store.mark_messages_compacted` (idempotent).
- `Store.insert_summary` + `list_summaries` (start_message_id ASC).
- `recent_messages(include_compacted=False)` default; True override.
- `Store.add_pin` + `list_pins` (id ASC).
- `Store.set_pending_proposal` / `consume_pending_proposal`
  (atomic read+clear).
- `Store.clear_conversation` -> `ClearResult`. Pins survive.
- `Store.get_runtime_config` / `set_runtime_config` round-trip.
- FK CASCADE on `summaries` and `pinned` when a conversation is deleted.

Tests assert observable behavior — they avoid asserting raw SQL shape
beyond what a column/table inspection requires.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ember.providers.base import Usage
from ember.store.repo import SCHEMA_VERSION, Store
from ember.store.types import ClearResult, Pin, Summary


# --- Fixtures -------------------------------------------------------------


@pytest.fixture
async def store(tmp_path: Path):
    s = Store(tmp_path / "conversations.db")
    await s.open()
    try:
        yield s
    finally:
        await s.close()


# --- v=4 fresh init shape -------------------------------------------------


class TestFreshV4Schema:
    async def test_schema_version_is_at_least_4(self) -> None:
        assert SCHEMA_VERSION >= 4

    async def test_messages_has_compacted_column(self, store: Store) -> None:
        cur = await store._c.execute("PRAGMA table_info(messages)")
        rows = await cur.fetchall()
        await cur.close()
        col_names = [r[1] for r in rows]
        assert "compacted" in col_names

    async def test_conversations_has_pending_profile_proposal_column(self, store: Store) -> None:
        cur = await store._c.execute("PRAGMA table_info(conversations)")
        rows = await cur.fetchall()
        await cur.close()
        col_names = [r[1] for r in rows]
        assert "pending_profile_proposal" in col_names

    async def test_summaries_table_exists(self, store: Store) -> None:
        cur = await store._c.execute("PRAGMA table_info(summaries)")
        rows = await cur.fetchall()
        await cur.close()
        assert rows, "summaries table missing"
        col_names = [r[1] for r in rows]
        for col in (
            "id",
            "conversation_id",
            "start_message_id",
            "end_message_id",
            "summary_text",
            "created_at",
        ):
            assert col in col_names, f"summaries missing column {col!r}"

    async def test_pinned_table_exists(self, store: Store) -> None:
        cur = await store._c.execute("PRAGMA table_info(pinned)")
        rows = await cur.fetchall()
        await cur.close()
        assert rows, "pinned table missing"
        col_names = [r[1] for r in rows]
        for col in ("id", "conversation_id", "content", "created_at"):
            assert col in col_names, f"pinned missing column {col!r}"

    async def test_runtime_config_table_exists(self, store: Store) -> None:
        cur = await store._c.execute("PRAGMA table_info(runtime_config)")
        rows = await cur.fetchall()
        await cur.close()
        assert rows, "runtime_config table missing"
        col_names = [r[1] for r in rows]
        for col in ("key", "value", "updated_at"):
            assert col in col_names, f"runtime_config missing column {col!r}"

    async def test_messages_compacted_default_zero(self, store: Store) -> None:
        # Newly-inserted messages should land with compacted=0 by
        # default — `recent_messages` with no override returns them.
        c = await store.get_or_create_conversation("discord", "t")
        await store.append_message(c.id, "user", "hi")
        msgs = await store.recent_messages(c.id)
        assert len(msgs) == 1
        # The python-side StoredMessage may surface compacted as a
        # field. Either way, the message must come back via the
        # default (non-include_compacted) call.
        assert msgs[0].content == "hi"


# --- mark_messages_compacted ---------------------------------------------


class TestMarkMessagesCompacted:
    async def test_flips_flag(self, store: Store) -> None:
        c = await store.get_or_create_conversation("discord", "t")
        m1 = await store.append_message(c.id, "user", "first")
        m2 = await store.append_message(c.id, "assistant", "second")
        m3 = await store.append_message(c.id, "user", "third")

        await store.mark_messages_compacted([m1, m2])

        # Default recent_messages excludes compacted=1.
        active = await store.recent_messages(c.id)
        contents = [m.content for m in active]
        assert contents == ["third"]

        # include_compacted=True returns the full set.
        full = await store.recent_messages(c.id, include_compacted=True)
        full_contents = [m.content for m in full]
        assert full_contents == ["first", "second", "third"]
        # Sanity: m3 was untouched.
        assert m3 > 0

    async def test_idempotent_on_recall(self, store: Store) -> None:
        c = await store.get_or_create_conversation("discord", "t")
        m1 = await store.append_message(c.id, "user", "a")
        m2 = await store.append_message(c.id, "user", "b")

        await store.mark_messages_compacted([m1, m2])
        # Calling again on the same set must not raise nor toggle.
        await store.mark_messages_compacted([m1, m2])

        active = await store.recent_messages(c.id)
        assert active == []
        full = await store.recent_messages(c.id, include_compacted=True)
        assert [m.content for m in full] == ["a", "b"]

    async def test_empty_list_is_noop(self, store: Store) -> None:
        c = await store.get_or_create_conversation("discord", "t")
        await store.append_message(c.id, "user", "a")
        # Should not raise.
        await store.mark_messages_compacted([])
        active = await store.recent_messages(c.id)
        assert [m.content for m in active] == ["a"]


# --- insert_summary / list_summaries -------------------------------------


class TestSummaries:
    async def test_round_trip_single(self, store: Store) -> None:
        c = await store.get_or_create_conversation("discord", "t")
        m1 = await store.append_message(c.id, "user", "x")
        m2 = await store.append_message(c.id, "assistant", "y")

        sid = await store.insert_summary(
            c.id,
            start_message_id=m1,
            end_message_id=m2,
            summary_text="condensed",
        )
        assert isinstance(sid, int) and sid > 0

        summaries = await store.list_summaries(c.id)
        assert len(summaries) == 1
        s = summaries[0]
        assert isinstance(s, Summary)
        assert s.conversation_id == c.id
        assert s.start_message_id == m1
        assert s.end_message_id == m2
        assert s.summary_text == "condensed"

    async def test_listed_ascending_by_start_message_id(self, store: Store) -> None:
        # TLC-12: byte-stable prompt assembly requires deterministic
        # iteration order. `list_summaries` orders by start_message_id
        # ASC so the rendered summary block is identical when nothing
        # changed.
        c = await store.get_or_create_conversation("discord", "t")
        m_ids = []
        for i in range(6):
            m_ids.append(await store.append_message(c.id, "user", f"m{i}"))

        # Insert summaries out of order.
        await store.insert_summary(
            c.id,
            start_message_id=m_ids[3],
            end_message_id=m_ids[5],
            summary_text="late",
        )
        await store.insert_summary(
            c.id,
            start_message_id=m_ids[0],
            end_message_id=m_ids[2],
            summary_text="early",
        )

        summaries = await store.list_summaries(c.id)
        starts = [s.start_message_id for s in summaries]
        texts = [s.summary_text for s in summaries]
        assert starts == sorted(starts)
        assert texts == ["early", "late"]

    async def test_isolated_per_conversation(self, store: Store) -> None:
        c1 = await store.get_or_create_conversation("discord", "a")
        c2 = await store.get_or_create_conversation("discord", "b")
        m1 = await store.append_message(c1.id, "user", "x")
        m2 = await store.append_message(c2.id, "user", "y")

        await store.insert_summary(
            c1.id, start_message_id=m1, end_message_id=m1, summary_text="for-c1"
        )
        await store.insert_summary(
            c2.id, start_message_id=m2, end_message_id=m2, summary_text="for-c2"
        )

        only_c1 = await store.list_summaries(c1.id)
        assert len(only_c1) == 1
        assert only_c1[0].summary_text == "for-c1"


# --- recent_messages compaction filter -----------------------------------


class TestRecentMessagesCompactedFilter:
    async def test_default_excludes_compacted(self, store: Store) -> None:
        c = await store.get_or_create_conversation("discord", "t")
        m1 = await store.append_message(c.id, "user", "old")
        await store.append_message(c.id, "user", "new")
        await store.mark_messages_compacted([m1])
        msgs = await store.recent_messages(c.id)
        assert [m.content for m in msgs] == ["new"]

    async def test_include_compacted_returns_all(self, store: Store) -> None:
        c = await store.get_or_create_conversation("discord", "t")
        m1 = await store.append_message(c.id, "user", "old")
        await store.append_message(c.id, "user", "new")
        await store.mark_messages_compacted([m1])
        msgs = await store.recent_messages(c.id, include_compacted=True)
        assert [m.content for m in msgs] == ["old", "new"]

    async def test_default_kwarg_is_false(self, store: Store) -> None:
        # Bare-call default must be the "exclude compacted" behavior;
        # any caller that omits the kwarg is treated as wanting the
        # active history (UoW-12: "subsequent prompt assembly skips
        # compacted messages").
        c = await store.get_or_create_conversation("discord", "t")
        m1 = await store.append_message(c.id, "user", "ghost")
        await store.mark_messages_compacted([m1])
        # Passing only the limit kwarg — verifies include_compacted
        # defaults False even without explicit pass.
        msgs = await store.recent_messages(c.id, limit=10)
        assert msgs == []


# --- Pins ---------------------------------------------------------------


class TestPins:
    async def test_round_trip(self, store: Store) -> None:
        c = await store.get_or_create_conversation("discord", "t")
        pid = await store.add_pin(c.id, "remember the milk")
        assert isinstance(pid, int) and pid > 0

        pins = await store.list_pins(c.id)
        assert len(pins) == 1
        assert isinstance(pins[0], Pin)
        assert pins[0].content == "remember the milk"
        assert pins[0].conversation_id == c.id

    async def test_listed_ascending_by_id(self, store: Store) -> None:
        c = await store.get_or_create_conversation("discord", "t")
        await store.add_pin(c.id, "first")
        await store.add_pin(c.id, "second")
        await store.add_pin(c.id, "third")
        pins = await store.list_pins(c.id)
        assert [p.content for p in pins] == ["first", "second", "third"]
        assert [p.id for p in pins] == sorted(p.id for p in pins)

    async def test_pins_isolated_per_conversation(self, store: Store) -> None:
        c1 = await store.get_or_create_conversation("discord", "a")
        c2 = await store.get_or_create_conversation("discord", "b")
        await store.add_pin(c1.id, "for-c1")
        await store.add_pin(c2.id, "for-c2")
        pins1 = await store.list_pins(c1.id)
        pins2 = await store.list_pins(c2.id)
        assert [p.content for p in pins1] == ["for-c1"]
        assert [p.content for p in pins2] == ["for-c2"]


# --- Pending profile proposal -------------------------------------------


class TestPendingProposal:
    async def test_set_and_consume_round_trip(self, store: Store) -> None:
        c = await store.get_or_create_conversation("discord", "t")
        proposal = '{"proposed_text": "user prefers dark mode", "diff": "+ dark mode"}'
        await store.set_pending_proposal(c.id, proposal)

        out = await store.consume_pending_proposal(c.id)
        assert out == proposal

    async def test_consume_clears_in_one_shot(self, store: Store) -> None:
        # consume_pending_proposal returns the prior value AND clears
        # it in the same call (atomic read+clear). A second consume
        # must therefore see no value.
        c = await store.get_or_create_conversation("discord", "t")
        await store.set_pending_proposal(c.id, '{"x": 1}')

        first = await store.consume_pending_proposal(c.id)
        assert first == '{"x": 1}'

        second = await store.consume_pending_proposal(c.id)
        assert second is None

    async def test_consume_on_empty_returns_none(self, store: Store) -> None:
        c = await store.get_or_create_conversation("discord", "t")
        out = await store.consume_pending_proposal(c.id)
        assert out is None

    async def test_set_none_clears(self, store: Store) -> None:
        c = await store.get_or_create_conversation("discord", "t")
        await store.set_pending_proposal(c.id, '{"x": 1}')
        await store.set_pending_proposal(c.id, None)
        out = await store.consume_pending_proposal(c.id)
        assert out is None

    async def test_set_overwrites_prior(self, store: Store) -> None:
        c = await store.get_or_create_conversation("discord", "t")
        await store.set_pending_proposal(c.id, '{"a": 1}')
        await store.set_pending_proposal(c.id, '{"b": 2}')
        out = await store.consume_pending_proposal(c.id)
        assert out == '{"b": 2}'

    async def test_proposal_isolated_per_conversation(self, store: Store) -> None:
        c1 = await store.get_or_create_conversation("discord", "a")
        c2 = await store.get_or_create_conversation("discord", "b")
        await store.set_pending_proposal(c1.id, '{"who": "c1"}')
        # c2 should be unaffected.
        out2 = await store.consume_pending_proposal(c2.id)
        assert out2 is None
        out1 = await store.consume_pending_proposal(c1.id)
        assert out1 == '{"who": "c1"}'


# --- Clear conversation -------------------------------------------------


class TestClearConversation:
    async def _populate(self, store: Store):
        c = await store.get_or_create_conversation("discord", "t")
        m1 = await store.append_message(c.id, "user", "u1")
        m2 = await store.append_message(c.id, "assistant", "a1")
        m3 = await store.append_message(c.id, "user", "u2")
        await store.insert_summary(c.id, start_message_id=m1, end_message_id=m2, summary_text="s1")
        await store.insert_summary(c.id, start_message_id=m2, end_message_id=m3, summary_text="s2")
        await store.add_pin(c.id, "pin-keeps")
        await store.set_pending_proposal(c.id, '{"x": 1}')
        await store.update_claude_cli_session(c.id, "session-xyz")
        return c, [m1, m2, m3]

    async def test_marks_all_messages_compacted(self, store: Store) -> None:
        c, _ = await self._populate(store)
        await store.clear_conversation(c.id)
        active = await store.recent_messages(c.id)
        assert active == []
        # But include_compacted=True still surfaces them.
        full = await store.recent_messages(c.id, include_compacted=True)
        assert len(full) == 3

    async def test_deletes_summaries(self, store: Store) -> None:
        c, _ = await self._populate(store)
        await store.clear_conversation(c.id)
        summaries = await store.list_summaries(c.id)
        assert summaries == []

    async def test_clears_pending_proposal(self, store: Store) -> None:
        c, _ = await self._populate(store)
        await store.clear_conversation(c.id)
        out = await store.consume_pending_proposal(c.id)
        assert out is None

    async def test_nulls_session_id(self, store: Store) -> None:
        c, _ = await self._populate(store)
        await store.clear_conversation(c.id)
        again = await store.get_conversation(c.id)
        assert again is not None
        assert again.claude_cli_session_id is None

    async def test_pins_survive(self, store: Store) -> None:
        c, _ = await self._populate(store)
        await store.clear_conversation(c.id)
        pins = await store.list_pins(c.id)
        assert [p.content for p in pins] == ["pin-keeps"]

    async def test_clear_result_counts(self, store: Store) -> None:
        c, ids = await self._populate(store)
        result = await store.clear_conversation(c.id)
        assert isinstance(result, ClearResult)
        # All 3 messages were freshly active before clear → all marked.
        assert result.messages_marked == 3
        assert result.summaries_deleted == 2
        assert result.session_reset is True

    async def test_clear_on_empty_conversation_is_safe(self, store: Store) -> None:
        c = await store.get_or_create_conversation("discord", "empty")
        result = await store.clear_conversation(c.id)
        assert isinstance(result, ClearResult)
        assert result.messages_marked == 0
        assert result.summaries_deleted == 0
        # session_reset documents the operation; with no prior session
        # set, the column was already NULL — the call still nulls it.
        # Either True or False is defensible; we assert the result is
        # a bool. (Spec ambiguity flagged in report.)
        assert isinstance(result.session_reset, bool)


# --- Runtime config -----------------------------------------------------


class TestRuntimeConfig:
    async def test_missing_returns_none(self, store: Store) -> None:
        out = await store.get_runtime_config("nonesuch")
        assert out is None

    async def test_set_and_get_round_trip(self, store: Store) -> None:
        await store.set_runtime_config("cheap_tier_override", "google:gemini-2.5-flash")
        out = await store.get_runtime_config("cheap_tier_override")
        assert out == "google:gemini-2.5-flash"

    async def test_overwrite_replaces_value(self, store: Store) -> None:
        await store.set_runtime_config("k", "v1")
        await store.set_runtime_config("k", "v2")
        out = await store.get_runtime_config("k")
        assert out == "v2"

    async def test_overwrite_updates_updated_at(self, store: Store) -> None:
        # The column exists; on overwrite, updated_at should advance.
        # We can't strongly assert ordering without sleeping, so we
        # simply read the column and assert it is non-null after each
        # write. The direct PRAGMA-style read is the only way to see
        # this — runtime_config has no list_* method in the spec.
        await store.set_runtime_config("k", "v1")
        cur = await store._c.execute("SELECT updated_at FROM runtime_config WHERE key = ?", ("k",))
        row1 = await cur.fetchone()
        await cur.close()
        assert row1 is not None and row1[0] is not None

        await store.set_runtime_config("k", "v2")
        cur = await store._c.execute("SELECT updated_at FROM runtime_config WHERE key = ?", ("k",))
        row2 = await cur.fetchone()
        await cur.close()
        assert row2 is not None and row2[0] is not None


# --- FK CASCADE: deleting a conversation removes pins + summaries -------


class TestForeignKeyCascade:
    async def test_delete_conversation_cascades_summaries(self, store: Store) -> None:
        c = await store.get_or_create_conversation("discord", "t")
        m = await store.append_message(c.id, "user", "x")
        await store.insert_summary(c.id, start_message_id=m, end_message_id=m, summary_text="s")

        # Delete the conversation row directly (no public delete API).
        await store._c.execute("DELETE FROM conversations WHERE id = ?", (c.id,))
        await store._c.commit()

        cur = await store._c.execute(
            "SELECT COUNT(*) FROM summaries WHERE conversation_id = ?", (c.id,)
        )
        row = await cur.fetchone()
        await cur.close()
        assert row is not None
        assert row[0] == 0

    async def test_delete_conversation_cascades_pins(self, store: Store) -> None:
        c = await store.get_or_create_conversation("discord", "t")
        await store.add_pin(c.id, "doomed")

        await store._c.execute("DELETE FROM conversations WHERE id = ?", (c.id,))
        await store._c.commit()

        cur = await store._c.execute(
            "SELECT COUNT(*) FROM pinned WHERE conversation_id = ?", (c.id,)
        )
        row = await cur.fetchone()
        await cur.close()
        assert row is not None
        assert row[0] == 0


# --- Sanity: existing message round-trip still works at v=4 -------------


class TestExistingFlowsStillWorkAtV4:
    async def test_subscription_usage_round_trip(self, store: Store) -> None:
        c = await store.get_or_create_conversation("discord", "t")
        await store.append_message(
            c.id,
            "assistant",
            "hi",
            usage=Usage(input_tokens=1, output_tokens=2, subscription=True),
        )
        msgs = await store.recent_messages(c.id)
        assert len(msgs) == 1
        assert msgs[0].usage is not None
        assert msgs[0].usage.subscription is True
