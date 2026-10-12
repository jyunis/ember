"""Conversation archival (TLC-23 / UoW-26 / FC-27) — Store-level semantics.

Exercises the archive stamp API on a real Store against a tmp DB:

- `archive_conversation(channel, key, *, now)` stamps `archived_at` and
  returns True; a second call returns False (idempotent) and does not
  re-stamp; an unknown key returns False.
- Archival never touches `messages` rows (TLC-23: delete = archive,
  never forget). Row-for-row content survives the stamp.
- `unarchive_conversation` clears the stamp; True only when a stamp was
  actually cleared.
- `get_or_create_conversation` on a stamped row clears the stamp inline
  (UoW-26 self-heal: an inbound message proves the thread exists).
- `unarchived_conversation_keys(channel)` lists only unstamped keys for
  that channel (the reconciliation sweep's input).
- Grep-level TLC-23 enforcement: no `DELETE FROM conversations` /
  `DELETE FROM messages` statement anywhere under `store/`.

Written test-first per the implementer/tester barrier; the FC-27
signatures in doc #54 are the contract.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ember.store.repo import Store


# --- Fixtures -------------------------------------------------------------


@pytest.fixture
async def store(tmp_path: Path):
    s = Store(tmp_path / "conversations.db")
    await s.open()
    try:
        yield s
    finally:
        await s.close()


def _now() -> datetime:
    return datetime.now(UTC)


async def _seeded(store: Store, key: str = "thread-1"):
    """A conversation with user + assistant + tool messages."""
    c = await store.get_or_create_conversation("discord", key)
    await store.append_message(c.id, "user", f"hello from {key}")
    await store.append_message(c.id, "assistant", f"reply in {key}")
    await store.append_message(c.id, "tool", f"tool output in {key}")
    return c


# --- archive_conversation --------------------------------------------------


class TestArchiveConversation:
    async def test_archive_stamps_and_returns_true(self, store: Store) -> None:
        c = await _seeded(store)
        assert await store.archive_conversation("discord", "thread-1", now=_now()) is True
        again = await store.get_conversation(c.id)
        assert again is not None
        assert again.archived_at is not None

    async def test_second_archive_returns_false_and_keeps_stamp(self, store: Store) -> None:
        c = await _seeded(store)
        first_now = _now()
        assert await store.archive_conversation("discord", "thread-1", now=first_now) is True
        stamped = await store.get_conversation(c.id)
        assert stamped is not None
        first_stamp = stamped.archived_at

        # Re-archiving is a no-op: returns False and the original stamp
        # survives (pass a clearly different `now` to prove no re-stamp).
        later = first_now + timedelta(hours=3)
        assert await store.archive_conversation("discord", "thread-1", now=later) is False
        again = await store.get_conversation(c.id)
        assert again is not None
        assert again.archived_at == first_stamp

    async def test_archive_unknown_key_returns_false(self, store: Store) -> None:
        # Thread predating Ember / never spoken in: no row → False, no-op.
        assert await store.archive_conversation("discord", "no-such-thread", now=_now()) is False

    async def test_archive_is_channel_scoped(self, store: Store) -> None:
        c = await _seeded(store)
        # Same key, different channel → no match, nothing stamped.
        assert await store.archive_conversation("telegram", "thread-1", now=_now()) is False
        again = await store.get_conversation(c.id)
        assert again is not None
        assert again.archived_at is None


# --- TLC-23: messages fully intact after archival --------------------------


class TestArchivalPreservesMessages:
    async def test_messages_intact_after_archive(self, store: Store) -> None:
        c = await _seeded(store)
        before_msgs = await store.recent_messages(c.id, limit=50)
        before_count = await store.message_count(c.id)

        assert await store.archive_conversation("discord", "thread-1", now=_now()) is True

        after_msgs = await store.recent_messages(c.id, limit=50)
        assert await store.message_count(c.id) == before_count
        assert [(m.id, m.role, m.content) for m in after_msgs] == [
            (m.id, m.role, m.content) for m in before_msgs
        ]

    async def test_conversation_row_survives_archive(self, store: Store) -> None:
        c = await _seeded(store)
        await store.archive_conversation("discord", "thread-1", now=_now())
        again = await store.get_conversation(c.id)
        assert again is not None
        assert again.id == c.id
        assert again.channel_conversation_key == "thread-1"

    def test_no_delete_statements_in_store_sources(self) -> None:
        # TLC-23 grep-level enforcement (doc #54 test plan): no code path
        # in store/ may issue DELETE against conversations or messages.
        # (`messages_fts` maintenance is exempt — \b excludes it.)
        import ember.store as store_pkg

        pkg_dir = Path(store_pkg.__file__).parent
        pattern = re.compile(r"delete\s+from\s+(conversations|messages)\b", re.IGNORECASE)
        offenders = [
            p.name
            for p in list(pkg_dir.rglob("*.py")) + list(pkg_dir.rglob("*.sql"))
            if pattern.search(p.read_text(encoding="utf-8"))
        ]
        assert offenders == []


# --- unarchive_conversation -----------------------------------------------


class TestUnarchiveConversation:
    async def test_unarchive_clears_stamp(self, store: Store) -> None:
        c = await _seeded(store)
        await store.archive_conversation("discord", "thread-1", now=_now())

        assert await store.unarchive_conversation("discord", "thread-1") is True
        again = await store.get_conversation(c.id)
        assert again is not None
        assert again.archived_at is None

    async def test_unarchive_unstamped_returns_false(self, store: Store) -> None:
        await _seeded(store)
        assert await store.unarchive_conversation("discord", "thread-1") is False

    async def test_unarchive_unknown_key_returns_false(self, store: Store) -> None:
        assert await store.unarchive_conversation("discord", "no-such-thread") is False


# --- UoW-26 self-heal: get_or_create clears the stamp ----------------------


class TestSelfHealOnResolution:
    async def test_get_or_create_clears_existing_stamp(self, store: Store) -> None:
        c = await _seeded(store)
        await store.archive_conversation("discord", "thread-1", now=_now())

        # A message arriving for the "deleted" thread proves it exists:
        # conversation resolution clears the stamp (UoW-26 self-heal).
        resolved = await store.get_or_create_conversation("discord", "thread-1")
        assert resolved.id == c.id
        assert resolved.archived_at is None

        # And the clear is persisted, not just reflected in the returned row.
        again = await store.get_conversation(c.id)
        assert again is not None
        assert again.archived_at is None

    async def test_get_or_create_on_fresh_key_starts_unstamped(self, store: Store) -> None:
        c = await store.get_or_create_conversation("discord", "brand-new")
        assert c.archived_at is None


# --- unarchived_conversation_keys ------------------------------------------


class TestUnarchivedConversationKeys:
    async def test_lists_only_unstamped_keys(self, store: Store) -> None:
        await _seeded(store, key="t-live-1")
        await _seeded(store, key="t-live-2")
        await _seeded(store, key="t-gone")
        await store.archive_conversation("discord", "t-gone", now=_now())

        keys = await store.unarchived_conversation_keys("discord")
        assert set(keys) == {"t-live-1", "t-live-2"}

    async def test_channel_filter(self, store: Store) -> None:
        await _seeded(store, key="d-key")
        await store.get_or_create_conversation("telegram", "tg-key")

        keys = await store.unarchived_conversation_keys("discord")
        assert set(keys) == {"d-key"}

    async def test_empty_store_returns_empty_list(self, store: Store) -> None:
        assert await store.unarchived_conversation_keys("discord") == []

    async def test_unarchive_restores_key_to_listing(self, store: Store) -> None:
        await _seeded(store, key="t-1")
        await store.archive_conversation("discord", "t-1", now=_now())
        assert await store.unarchived_conversation_keys("discord") == []

        await store.unarchive_conversation("discord", "t-1")
        assert await store.unarchived_conversation_keys("discord") == ["t-1"]
