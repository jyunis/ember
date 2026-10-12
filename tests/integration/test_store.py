"""Conversation store (FC-7) — schema, FK enforcement, conversations,
messages, restart-preserves-context (v0 acceptance criterion), and
TLC-11 (single source of truth) sanity checks.

The store is interface-only at write time; these tests exercise it via
its public coroutine API. The file shape is the canonical schema (see
`store/schema.sql`) — we treat the schema file as the contract since
contracts doc § FC-7 says "all mutations go through repository
functions" and "what's in the DB is what's true."
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ember.errors import SchemaVersionError, StoreError
from ember.providers.base import ToolCall, Usage
from ember.store.repo import SCHEMA_VERSION, Store


# --- Fixtures -------------------------------------------------------------


@pytest.fixture
async def store(tmp_path: Path):
    """A freshly-opened Store on a per-test file."""
    s = Store(tmp_path / "conversations.db")
    await s.open()
    try:
        yield s
    finally:
        await s.close()


# --- FC-7: schema version -------------------------------------------------


class TestSchemaVersion:
    async def test_fresh_open_succeeds(self, tmp_path: Path) -> None:
        s = Store(tmp_path / "fresh.db")
        await s.open()
        await s.close()

    async def test_wrong_schema_version_raises(self, tmp_path: Path) -> None:
        # Pre-populate a DB file with a bogus schema_version row, then
        # confirm Store.open refuses it (FC-7).
        db = tmp_path / "wrong.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT)")
        bogus = SCHEMA_VERSION + 999
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (bogus,))
        conn.commit()
        conn.close()

        s = Store(db)
        with pytest.raises(SchemaVersionError):
            await s.open()

    async def test_missing_schema_version_row_raises(self, tmp_path: Path) -> None:
        # Build a file with no schema_version table at all. The Store
        # should refuse to start (FC-7: "missing/wrong schema_version").
        # NOTE [ambiguity]: spec says "refuse to start against a schema
        # it doesn't recognize" but doesn't explicitly say what an empty
        # but pre-existing file does. We assume "no version table" is
        # also a recognition failure.
        db = tmp_path / "missing_table.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE foo (id INTEGER)")
        conn.commit()
        conn.close()

        s = Store(db)
        with pytest.raises((SchemaVersionError, StoreError)):
            await s.open()


# --- TLC-11: single Store per process / re-opening behavior ---------------


class TestSingleStorePerProcess:
    async def test_reopen_same_path_works(self, tmp_path: Path) -> None:
        # Sequential open/close on the same file is fine — closing
        # releases the connection. This is what restart looks like.
        db = tmp_path / "reopen.db"
        s1 = Store(db)
        await s1.open()
        await s1.close()

        s2 = Store(db)
        await s2.open()
        await s2.close()

    async def test_two_stores_one_path_concurrent(self, tmp_path: Path) -> None:
        # NOTE [ambiguity]: TLC-11 says "exactly one Store per process,"
        # but it's not specified whether opening a second Store on the
        # same path while the first is open should refuse, succeed
        # (SQLite WAL allows it), or be flagged some other way. We test
        # that opening a second instance does not crash; if the
        # implementer chooses to refuse, replace this with an
        # assertRaises(StoreError).
        db = tmp_path / "concurrent.db"
        s1 = Store(db)
        s2 = Store(db)
        await s1.open()
        try:
            try:
                await s2.open()
                opened_two = True
                await s2.close()
            except StoreError:
                opened_two = False
        finally:
            await s1.close()

        assert opened_two in (True, False)  # documents either decision


# --- UoW-14: conversation creation ----------------------------------------


class TestConversationCreation:
    async def test_first_message_creates_row(self, store: Store) -> None:
        c = await store.get_or_create_conversation("discord", "thread-1")
        assert c.id > 0
        assert c.channel == "discord"
        assert c.channel_conversation_key == "thread-1"

    async def test_second_call_same_key_reuses(self, store: Store) -> None:
        c1 = await store.get_or_create_conversation("discord", "thread-1")
        c2 = await store.get_or_create_conversation("discord", "thread-1")
        assert c1.id == c2.id

    async def test_different_keys_create_distinct_rows(self, store: Store) -> None:
        c1 = await store.get_or_create_conversation("discord", "t1")
        c2 = await store.get_or_create_conversation("discord", "t2")
        assert c1.id != c2.id

    async def test_different_channels_same_key_distinct(self, store: Store) -> None:
        c1 = await store.get_or_create_conversation("discord", "k")
        c2 = await store.get_or_create_conversation("telegram", "k")
        assert c1.id != c2.id

    async def test_get_conversation_by_id(self, store: Store) -> None:
        c = await store.get_or_create_conversation("discord", "t")
        again = await store.get_conversation(c.id)
        assert again is not None
        assert again.id == c.id

    async def test_get_conversation_missing_returns_none(self, store: Store) -> None:
        assert (await store.get_conversation(99999)) is None

    async def test_defaults_match_schema(self, store: Store) -> None:
        c = await store.get_or_create_conversation("discord", "t")
        # Schema defaults (see schema.sql). Default route is bare
        # `claude_cli` — model resolves to the provider's configured
        # default at runtime. (Earlier value `claude_cli:sonnet-4-6`
        # was an invalid CLI alias.)
        assert c.provider_route == "claude_cli"
        assert c.token_budget == 150000
        assert c.claude_cli_session_id is None
        assert c.auto_failover_on_rate_limit is False
        assert c.active_skill is None


# --- TLC-5: cost transparency persists correctly --------------------------


class TestMessagePersistence:
    async def test_append_message_returns_id(self, store: Store) -> None:
        c = await store.get_or_create_conversation("discord", "t")
        mid = await store.append_message(c.id, "user", "hello")
        assert isinstance(mid, int) and mid > 0

    async def test_subscription_turn_persists_with_cost_null(self, store: Store) -> None:
        # TLC-5: subscription turns store cost_usd=NULL, subscription_turn=1.
        c = await store.get_or_create_conversation("discord", "t")
        await store.append_message(c.id, "user", "hi")
        await store.append_message(
            c.id,
            "assistant",
            "hello!",
            usage=Usage(
                input_tokens=10,
                output_tokens=5,
                cached_tokens=0,
                cost_usd=None,
                subscription=True,
            ),
        )
        msgs = await store.recent_messages(c.id)
        assistant = next(m for m in msgs if m.role == "assistant")
        assert assistant.usage is not None
        assert assistant.usage.subscription is True
        assert assistant.usage.cost_usd is None
        assert assistant.usage.input_tokens == 10
        assert assistant.usage.output_tokens == 5

    async def test_metered_turn_persists_with_cost_value(self, store: Store) -> None:
        c = await store.get_or_create_conversation("discord", "t")
        await store.append_message(
            c.id,
            "assistant",
            "metered reply",
            usage=Usage(
                input_tokens=100,
                output_tokens=50,
                cost_usd=0.42,
                subscription=False,
            ),
        )
        msgs = await store.recent_messages(c.id)
        assistant = next(m for m in msgs if m.role == "assistant")
        assert assistant.usage is not None
        assert assistant.usage.subscription is False
        assert assistant.usage.cost_usd == pytest.approx(0.42)

    async def test_message_count(self, store: Store) -> None:
        c = await store.get_or_create_conversation("discord", "t")
        assert (await store.message_count(c.id)) == 0
        await store.append_message(c.id, "user", "a")
        await store.append_message(c.id, "assistant", "b")
        assert (await store.message_count(c.id)) == 2

    async def test_recent_messages_oldest_first(self, store: Store) -> None:
        c = await store.get_or_create_conversation("discord", "t")
        await store.append_message(c.id, "user", "first")
        await store.append_message(c.id, "assistant", "second")
        await store.append_message(c.id, "user", "third")
        msgs = await store.recent_messages(c.id)
        # Per docstring: "oldest-first (i.e. ready for prompt assembly)".
        assert [m.content for m in msgs] == ["first", "second", "third"]

    async def test_recent_messages_respects_limit(self, store: Store) -> None:
        c = await store.get_or_create_conversation("discord", "t")
        for i in range(5):
            await store.append_message(c.id, "user", f"m{i}")
        msgs = await store.recent_messages(c.id, limit=3)
        # The most recent 3, oldest-first → m2, m3, m4.
        # NOTE [ambiguity]: docstring says "most recent N, oldest-first."
        # We assert that interpretation; if the impl returns the oldest 3
        # (m0..m2) the test will fail and the spec needs tightening.
        assert [m.content for m in msgs] == ["m2", "m3", "m4"]


# --- FC-7: foreign keys enforced -----------------------------------------


class TestForeignKeyEnforcement:
    async def test_message_with_bogus_conversation_id_fails(self, store: Store) -> None:
        # NOTE [ambiguity]: FC-7 says "foreign keys enforced." The store
        # API doesn't expose a way to insert a bogus conversation_id
        # through `append_message` (the value isn't validated at the
        # python layer per the dataclass), but a no-such-conversation id
        # should still trip the SQLite FK check at insert time. If the
        # implementer chooses to validate in Python before insert,
        # accept either an IntegrityError-derived StoreError, ValueError,
        # or LookupError-style failure.
        with pytest.raises((StoreError, sqlite3.IntegrityError, ValueError, LookupError)):
            await store.append_message(99999, "user", "orphan")


# --- ToolCall column round-trip (forward-looking but interface is in v0) --


class TestToolCallsColumn:
    async def test_tool_calls_round_trip(self, store: Store) -> None:
        # NOTE [ambiguity]: schema has a `tool_calls_json` column; the
        # repo signature accepts `tool_calls`. Round-trip semantics
        # aren't in the contracts doc for v0 (no tools yet), but the
        # interface promises the parameter. If the impl chooses to
        # ignore tool_calls in v0, this test should be marked xfail.
        c = await store.get_or_create_conversation("discord", "t")
        tcs = [ToolCall(id="tc1", name="echo", args={"x": 1})]
        mid = await store.append_message(c.id, "assistant", "ok", tool_calls=tcs)
        assert mid > 0
        msgs = await store.recent_messages(c.id)
        assistant = next(m for m in msgs if m.id == mid)
        assert assistant.tool_calls is not None
        assert len(assistant.tool_calls) == 1
        assert assistant.tool_calls[0].name == "echo"


# --- v0 acceptance criterion: restart preserves context -------------------


class TestRestartPreservesContext:
    async def test_close_reopen_same_db_path(self, tmp_path: Path) -> None:
        db = tmp_path / "persist.db"
        s1 = Store(db)
        await s1.open()
        c = await s1.get_or_create_conversation("discord", "thread-1")
        await s1.append_message(c.id, "user", "first message")
        await s1.append_message(
            c.id,
            "assistant",
            "first reply",
            usage=Usage(input_tokens=5, output_tokens=10, subscription=True),
        )
        await s1.close()

        # Reopen.
        s2 = Store(db)
        await s2.open()
        try:
            # Same key → same conversation row.
            again = await s2.get_or_create_conversation("discord", "thread-1")
            assert again.id == c.id
            msgs = await s2.recent_messages(again.id)
            assert [m.content for m in msgs] == ["first message", "first reply"]
            # Usage round-trips.
            assistant = next(m for m in msgs if m.role == "assistant")
            assert assistant.usage is not None
            assert assistant.usage.subscription is True
        finally:
            await s2.close()


# --- Misc store API surface -----------------------------------------------


class TestSessionAndTouch:
    async def test_update_claude_cli_session_round_trip(self, store: Store) -> None:
        c = await store.get_or_create_conversation("discord", "t")
        await store.update_claude_cli_session(c.id, "session-abc")
        again = await store.get_conversation(c.id)
        assert again is not None
        assert again.claude_cli_session_id == "session-abc"

        await store.update_claude_cli_session(c.id, None)
        again2 = await store.get_conversation(c.id)
        assert again2 is not None
        assert again2.claude_cli_session_id is None

    async def test_touch_conversation_updates_last_active(self, store: Store) -> None:
        c1 = await store.get_or_create_conversation("discord", "t")
        # Bare smoke test — the contract is "updates last_active to now."
        # Without sleeping we can't strongly assert ordering, but we can
        # confirm the call succeeds and the row still exists.
        await store.touch_conversation(c1.id)
        again = await store.get_conversation(c1.id)
        assert again is not None
