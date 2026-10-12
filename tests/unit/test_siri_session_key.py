"""Siri session-window key resolution (design doc #55, SIRI-UoW-1).

Two surfaces under test:

- `ember.channels.siri.resolve_conversation_key` — the pure
  continue-vs-mint decision given now, the newest session info, pending
  state, and the explicit `new` flag. Rule order (first match wins):

    1. `new` is True                                   -> mint
    2. no newest session                               -> mint
    3. newest has a pending_* interaction              -> continue
    4. newest active within the session window         -> continue
    5. otherwise                                       -> mint

  Mint format is exactly ``f"siri:{int(now.timestamp())}"``.

- `Store.newest_channel_session(channel)` + the `ChannelSession` row
  dataclass — the unarchived conversation on `channel` with the
  greatest `last_active` (ties broken by greatest id); `has_pending`
  is True iff any of pending_profile_proposal /
  pending_tool_confirmation / pending_schwab_reauth is non-NULL.

Imports of the new surfaces are deliberately deferred into helpers so
that, against a pre-implementation tree, the Store-method tests fail
with AttributeError (missing method) rather than being masked by a
collection-time ImportError for `ember.channels.siri`.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ember.store.repo import Store

NOW = datetime(2026, 8, 28, 12, 0, 0, tzinfo=UTC)
WINDOW_S = 600


def _resolve(**kwargs) -> str:
    """Deferred import: pre-implementation this raises ImportError /
    ModuleNotFoundError for `ember.channels.siri` (the right baseline
    failure), without poisoning the whole module at collection time."""
    from ember.channels.siri import resolve_conversation_key

    return resolve_conversation_key(**kwargs)


def _session(key: str, last_active: datetime, *, has_pending: bool = False):
    from ember.store.repo import ChannelSession

    return ChannelSession(key=key, last_active=last_active, has_pending=has_pending)


# --- resolve_conversation_key (pure, SIRI-UoW-1) ---------------------------


class TestResolveConversationKey:
    def test_no_newest_session_mints(self) -> None:
        key = _resolve(now=NOW, newest=None, new=False, window_s=WINDOW_S)
        assert key == f"siri:{int(NOW.timestamp())}"

    def test_mint_format_is_siri_colon_epoch_seconds(self) -> None:
        # A distinct fixed instant, so the format assertion isn't an
        # accident of NOW.
        now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
        key = _resolve(now=now, newest=None, new=False, window_s=WINDOW_S)
        assert key == f"siri:{int(now.timestamp())}"

    def test_within_window_continues(self) -> None:
        newest = _session("siri:1000", NOW - timedelta(seconds=WINDOW_S - 1))
        key = _resolve(now=NOW, newest=newest, new=False, window_s=WINDOW_S)
        assert key == "siri:1000"

    def test_exactly_at_window_boundary_continues(self) -> None:
        # Rule 4 is `<= window_s`, inclusive.
        newest = _session("siri:1000", NOW - timedelta(seconds=WINDOW_S))
        key = _resolve(now=NOW, newest=newest, new=False, window_s=WINDOW_S)
        assert key == "siri:1000"

    def test_beyond_window_mints(self) -> None:
        newest = _session("siri:1000", NOW - timedelta(seconds=WINDOW_S + 1))
        key = _resolve(now=NOW, newest=newest, new=False, window_s=WINDOW_S)
        assert key == f"siri:{int(NOW.timestamp())}"

    def test_new_flag_mints_even_within_window(self) -> None:
        newest = _session("siri:1000", NOW - timedelta(seconds=5))
        key = _resolve(now=NOW, newest=newest, new=True, window_s=WINDOW_S)
        assert key == f"siri:{int(NOW.timestamp())}"

    def test_new_flag_mints_even_with_pending(self) -> None:
        # Rule 1 outranks rule 3: an explicit `new` always mints.
        newest = _session("siri:1000", NOW - timedelta(seconds=5), has_pending=True)
        key = _resolve(now=NOW, newest=newest, new=True, window_s=WINDOW_S)
        assert key == f"siri:{int(NOW.timestamp())}"

    def test_pending_continues_past_lapsed_window(self) -> None:
        # Rule 3: a pending confirmation pins the conversation even when
        # the window has long lapsed.
        newest = _session("siri:1000", NOW - timedelta(seconds=WINDOW_S * 10), has_pending=True)
        key = _resolve(now=NOW, newest=newest, new=False, window_s=WINDOW_S)
        assert key == "siri:1000"

    def test_pending_and_within_window_continues(self) -> None:
        newest = _session("siri:1000", NOW - timedelta(seconds=5), has_pending=True)
        key = _resolve(now=NOW, newest=newest, new=False, window_s=WINDOW_S)
        assert key == "siri:1000"

    def test_new_flag_mints_when_no_newest_session(self) -> None:
        key = _resolve(now=NOW, newest=None, new=True, window_s=WINDOW_S)
        assert key == f"siri:{int(NOW.timestamp())}"


# --- ChannelSession dataclass ----------------------------------------------


class TestChannelSession:
    def test_is_frozen(self) -> None:
        cs = _session("siri:1", NOW)
        with pytest.raises(dataclasses.FrozenInstanceError):
            cs.key = "siri:2"  # type: ignore[misc]

    def test_fields(self) -> None:
        cs = _session("siri:1", NOW, has_pending=True)
        assert cs.key == "siri:1"
        assert cs.last_active == NOW
        assert cs.has_pending is True


# --- Store.newest_channel_session ------------------------------------------


@pytest.fixture
async def store(tmp_path: Path):
    s = Store(tmp_path / "conversations.db")
    await s.open()
    try:
        yield s
    finally:
        await s.close()


def _iso(dt: datetime) -> str:
    """Render a datetime in the store's TEXT format
    (strftime('%Y-%m-%dT%H:%M:%fZ'))."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + ".000Z"


async def _seed(
    store: Store,
    key: str,
    last_active: datetime,
    *,
    channel: str = "siri",
) -> int:
    conv = await store.get_or_create_conversation(channel, key)
    await store._c.execute(
        "UPDATE conversations SET last_active = ? WHERE id = ?",
        (_iso(last_active), conv.id),
    )
    await store._c.commit()
    return conv.id


async def _set_column(store: Store, conv_id: int, column: str, value: str) -> None:
    # Column names come from the test parametrization, never from data.
    await store._c.execute(
        f"UPDATE conversations SET {column} = ? WHERE id = ?",  # noqa: S608
        (value, conv_id),
    )
    await store._c.commit()


class TestNewestChannelSession:
    async def test_no_conversations_returns_none(self, store: Store) -> None:
        assert await store.newest_channel_session("siri") is None

    async def test_returns_greatest_last_active(self, store: Store) -> None:
        await _seed(store, "siri:100", NOW - timedelta(seconds=500))
        await _seed(store, "siri:200", NOW - timedelta(seconds=100))
        newest = await store.newest_channel_session("siri")
        assert newest is not None
        assert newest.key == "siri:200"

    async def test_last_active_is_tz_aware_and_correct(self, store: Store) -> None:
        when = NOW - timedelta(seconds=100)
        await _seed(store, "siri:100", when)
        newest = await store.newest_channel_session("siri")
        assert newest is not None
        assert newest.last_active.tzinfo is not None
        assert newest.last_active == when

    async def test_tie_on_last_active_greatest_id_wins(self, store: Store) -> None:
        when = NOW - timedelta(seconds=100)
        await _seed(store, "siri:100", when)
        await _seed(store, "siri:200", when)
        newest = await store.newest_channel_session("siri")
        assert newest is not None
        assert newest.key == "siri:200"

    async def test_other_channels_are_excluded(self, store: Store) -> None:
        await _seed(store, "siri:100", NOW - timedelta(seconds=500))
        await _seed(store, "thread-9", NOW - timedelta(seconds=1), channel="discord")
        newest = await store.newest_channel_session("siri")
        assert newest is not None
        assert newest.key == "siri:100"

    async def test_channel_with_no_rows_returns_none(self, store: Store) -> None:
        await _seed(store, "siri:100", NOW - timedelta(seconds=500))
        assert await store.newest_channel_session("telegram") is None

    async def test_archived_conversation_is_skipped(self, store: Store) -> None:
        await _seed(store, "siri:100", NOW - timedelta(seconds=500))
        await _seed(store, "siri:200", NOW - timedelta(seconds=100))
        assert await store.archive_conversation("siri", "siri:200", now=NOW)
        newest = await store.newest_channel_session("siri")
        assert newest is not None
        assert newest.key == "siri:100"

    async def test_all_archived_returns_none(self, store: Store) -> None:
        await _seed(store, "siri:100", NOW - timedelta(seconds=500))
        assert await store.archive_conversation("siri", "siri:100", now=NOW)
        assert await store.newest_channel_session("siri") is None

    async def test_has_pending_false_when_all_null(self, store: Store) -> None:
        await _seed(store, "siri:100", NOW - timedelta(seconds=100))
        newest = await store.newest_channel_session("siri")
        assert newest is not None
        assert newest.has_pending is False

    @pytest.mark.parametrize(
        "column",
        [
            "pending_profile_proposal",
            "pending_tool_confirmation",
            "pending_schwab_reauth",
        ],
    )
    async def test_has_pending_true_for_each_pending_column(
        self, store: Store, column: str
    ) -> None:
        cid = await _seed(store, "siri:100", NOW - timedelta(seconds=100))
        await _set_column(store, cid, column, '{"pending": true}')
        newest = await store.newest_channel_session("siri")
        assert newest is not None
        assert newest.has_pending is True

    async def test_pending_on_older_row_does_not_leak_onto_newest(self, store: Store) -> None:
        old_id = await _seed(store, "siri:100", NOW - timedelta(seconds=500))
        await _seed(store, "siri:200", NOW - timedelta(seconds=100))
        await _set_column(store, old_id, "pending_tool_confirmation", "{}")
        newest = await store.newest_channel_session("siri")
        assert newest is not None
        assert newest.key == "siri:200"
        assert newest.has_pending is False
