"""Discord thread-delete archival (UoW-26 / FC-27) — integration.

Drives the real `DiscordChannel._handle_thread_delete` (the handler
`_BotClient.on_raw_thread_delete` delegates to, per FC-27's channel
adapter obligations) against a real Store, with fake raw thread-delete
payloads. Asserts:

- A delete whose `parent_id` is the configured conversations forum
  stamps the conversation (`archived_at` set) and leaves every message
  row intact (TLC-23).
- A delete under a different parent (scheduler channel, unrelated
  forum) is ignored.
- A delete for a thread key with no conversation row is a silent no-op
  (no exception, no new rows).
- A duplicate delete event is a no-op (stamping is idempotent).
- No outbound message is sent (the thread is gone; nothing to send —
  the handler never touches the send path, so no exception can come
  from it either).

Reconciliation sweep (UoW-26 sweep path). FC-27 obliges "reconciliation
sweep runs from the ready path, once per process" but names no method.
NAMING ASSUMPTIONS the implementer must honor or flag:

- `DiscordChannel._reconcile_archived_threads()` — the sweep entry point
  (async, no args), callable independently of the ready path.
- `DiscordChannel._enumerate_forum_thread_keys()` — async seam returning
  the collection of *string* conversation keys (thread ids) for every
  thread under the conversations forum, BOTH active and Discord-archived
  (Discord auto-archive ≠ deleted); raises on failure/partial listing.
  Tests monkeypatch this seam on the instance.

Sweep assertions:
- A conversation whose thread is absent from the enumeration is stamped.
- A conversation whose thread is present (including Discord-archived
  threads — the enumeration contract includes them) is NOT stamped.
- Enumeration failure stamps nothing (Decision #6 fail-safe). Whether
  the sweep method itself swallows the error or the ready path does is
  not pinned; only the fail-safe is.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from ember.channels.discord import DiscordChannel
from ember.config import Settings
from ember.store.repo import Store

CONVERSATIONS_FORUM_ID = 2
SCHEDULER_CHANNEL_ID = 3


def _settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DISCORD_ALLOWED_USER_IDS", "42")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")
    monkeypatch.setenv("DISCORD_CONVERSATIONS_CHANNEL_ID", str(CONVERSATIONS_FORUM_ID))
    monkeypatch.setenv("DISCORD_SCHEDULER_CHANNEL_ID", str(SCHEDULER_CHANNEL_ID))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    return Settings()  # type: ignore[call-arg]


def _delete_payload(*, thread_id: int, parent_id: int) -> SimpleNamespace:
    # Mimics discord.RawThreadDeleteEvent's surface: ints for ids, and
    # `thread` only populated when the thread was in the internal cache.
    return SimpleNamespace(
        thread_id=thread_id,
        parent_id=parent_id,
        guild_id=1,
        thread=None,
    )


@pytest.fixture
async def channel_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    settings = _settings(monkeypatch, tmp_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    store = Store(tmp_path / "conversations.db")
    await store.open()
    ch = DiscordChannel(settings, store=store)
    try:
        yield ch, store
    finally:
        await store.close()


async def _seeded_thread(store: Store, thread_id: int):
    """A conversation keyed like a forum thread, with a short history."""
    conv = await store.get_or_create_conversation("discord", str(thread_id))
    await store.append_message(conv.id, "user", f"hello in {thread_id}")
    await store.append_message(conv.id, "assistant", f"reply in {thread_id}")
    return conv


async def _conversation_count(store: Store) -> int:
    cur = await store._c.execute("SELECT COUNT(*) FROM conversations")
    (n,) = await cur.fetchone()
    await cur.close()
    return n


# --- Event path ------------------------------------------------------------


class TestThreadDeleteStampsConversation:
    async def test_delete_under_forum_stamps(self, channel_store) -> None:
        ch, store = channel_store
        conv = await _seeded_thread(store, 777)

        await ch._handle_thread_delete(
            _delete_payload(thread_id=777, parent_id=CONVERSATIONS_FORUM_ID)
        )

        again = await store.get_conversation(conv.id)
        assert again is not None
        assert again.archived_at is not None

    async def test_messages_intact_after_delete_event(self, channel_store) -> None:
        # TLC-23 end-to-end: the delete gesture archives; it never
        # deletes rows.
        ch, store = channel_store
        conv = await _seeded_thread(store, 777)
        before = [(m.id, m.role, m.content) for m in await store.recent_messages(conv.id, limit=50)]

        await ch._handle_thread_delete(
            _delete_payload(thread_id=777, parent_id=CONVERSATIONS_FORUM_ID)
        )

        after = [(m.id, m.role, m.content) for m in await store.recent_messages(conv.id, limit=50)]
        assert after == before
        assert await store.message_count(conv.id) == 2

    async def test_duplicate_delete_event_is_noop(self, channel_store) -> None:
        ch, store = channel_store
        conv = await _seeded_thread(store, 777)
        payload = _delete_payload(thread_id=777, parent_id=CONVERSATIONS_FORUM_ID)

        await ch._handle_thread_delete(payload)
        first = await store.get_conversation(conv.id)
        assert first is not None and first.archived_at is not None

        # Duplicate delivery: no error, stamp unchanged.
        await ch._handle_thread_delete(payload)
        second = await store.get_conversation(conv.id)
        assert second is not None
        assert second.archived_at == first.archived_at


class TestThreadDeleteIgnores:
    async def test_delete_under_other_parent_is_ignored(self, channel_store) -> None:
        ch, store = channel_store
        conv = await _seeded_thread(store, 777)

        # Same thread id, but the deleted thread lives under the
        # scheduler channel / an unrelated forum — not ours.
        await ch._handle_thread_delete(
            _delete_payload(thread_id=777, parent_id=SCHEDULER_CHANNEL_ID)
        )
        await ch._handle_thread_delete(_delete_payload(thread_id=777, parent_id=9999))

        again = await store.get_conversation(conv.id)
        assert again is not None
        assert again.archived_at is None

    async def test_delete_with_no_conversation_row_is_silent_noop(self, channel_store) -> None:
        ch, store = channel_store
        conv = await _seeded_thread(store, 777)
        rows_before = await _conversation_count(store)

        # A thread that predates Ember or was never spoken in: no row.
        await ch._handle_thread_delete(
            _delete_payload(thread_id=31337, parent_id=CONVERSATIONS_FORUM_ID)
        )

        # No new rows, nothing stamped, no exception.
        assert await _conversation_count(store) == rows_before
        again = await store.get_conversation(conv.id)
        assert again is not None
        assert again.archived_at is None


# --- Reconciliation sweep ---------------------------------------------------


def _fake_enumeration(monkeypatch: pytest.MonkeyPatch, ch: DiscordChannel, keys: set[str]):
    async def fake() -> set[str]:
        return keys

    monkeypatch.setattr(ch, "_enumerate_forum_thread_keys", fake)


class TestReconciliationSweep:
    async def test_sweep_stamps_conversation_whose_thread_is_gone(
        self, channel_store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ch, store = channel_store
        alive = await _seeded_thread(store, 100)
        gone = await _seeded_thread(store, 200)

        # Thread 100 still exists in Discord; thread 200 was deleted
        # while Ember was down.
        _fake_enumeration(monkeypatch, ch, {"100"})
        await ch._reconcile_archived_threads()

        alive_row = await store.get_conversation(alive.id)
        gone_row = await store.get_conversation(gone.id)
        assert alive_row is not None and alive_row.archived_at is None
        assert gone_row is not None and gone_row.archived_at is not None

    async def test_sweep_does_not_stamp_present_but_discord_archived_thread(
        self, channel_store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ch, store = channel_store
        auto_archived = await _seeded_thread(store, 300)

        # Discord's own auto-archive ≠ deleted: the enumeration seam's
        # contract is to list BOTH active and Discord-archived threads,
        # so a Discord-archived-but-present thread appears here and must
        # not be stamped.
        _fake_enumeration(monkeypatch, ch, {"300"})
        await ch._reconcile_archived_threads()

        row = await store.get_conversation(auto_archived.id)
        assert row is not None
        assert row.archived_at is None

    async def test_sweep_leaves_already_stamped_rows_alone(
        self, channel_store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ch, store = channel_store
        conv = await _seeded_thread(store, 400)
        await store.archive_conversation("discord", "400", now=datetime.now(UTC))
        stamped = await store.get_conversation(conv.id)
        assert stamped is not None

        _fake_enumeration(monkeypatch, ch, set())
        await ch._reconcile_archived_threads()

        again = await store.get_conversation(conv.id)
        assert again is not None
        assert again.archived_at == stamped.archived_at

    async def test_sweep_stamps_nothing_when_enumeration_fails(
        self, channel_store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Decision #6 fail-safe: a failed/partial listing must never
        # mass-archive live conversations.
        ch, store = channel_store
        c1 = await _seeded_thread(store, 500)
        c2 = await _seeded_thread(store, 600)

        async def boom() -> set[str]:
            raise RuntimeError("HTTP error mid-pagination")

        monkeypatch.setattr(ch, "_enumerate_forum_thread_keys", boom)

        # Contract: "sweep failure → logged, nothing stamped." Whether
        # the sweep method swallows the error itself or its caller does
        # is not pinned — only that nothing gets stamped.
        try:
            await ch._reconcile_archived_threads()
        except Exception:
            pass

        for conv in (c1, c2):
            row = await store.get_conversation(conv.id)
            assert row is not None
            assert row.archived_at is None
