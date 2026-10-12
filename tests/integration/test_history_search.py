"""History search & fetch (UoW-27 / FC-27) — Store-level semantics.

`search_history` — FTS5 keyword search over user/assistant message
content, ranked by bm25, spanning archived and live conversations:

- Results carry the `HistoryHit` shape from FC-27.
- `limit` respected (default 8, explicit value, max results available).
- Optional `conversation_id` filter.
- Archived conversations included and flagged `archived=True`.
- `snippet` is a non-empty string containing match context.
- Malformed FTS5 syntax falls back to a literal quoted-term search
  instead of raising (UoW-27 step 3).
- No match → empty list.
- Read-only: zero writes across search/fetch (TLC-19 spirit).

`history_window` — ordered message window around an anchor id:

- Returns `HistoryWindow` (FC-27 dataclass) with ordered messages.
- `before`/`after` bounds respected, clipped at conversation edges.
- Per-message content truncated at 4000 chars with a marker appended.
- Tool-role rows DO appear in windows (indexed or not, they are part
  of the record).
- Unknown `message_id` → None.

Documented choices (spec ambiguities, noted in the tester report):
- The anchor message itself is part of the window (`before=0, after=0`
  yields exactly the anchor) — "a window around the anchor" is read as
  inclusive of it.
- The truncation contract is read literally: first 4000 chars kept,
  marker appended after them (assertions accept a marker that keeps
  total length >= 4000 and do not pin the marker text).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ember.store.repo import Store
from ember.store.types import HistoryHit, HistoryWindow


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


# --- search_history: hit shape and ranking --------------------------------


class TestSearchHistoryBasics:
    async def test_hit_shape_matches_fc27(self, store: Store) -> None:
        c = await store.get_or_create_conversation("discord", "t1")
        mid = await store.append_message(c.id, "user", "we talked about the tuna sandwich")

        hits = await store.search_history("tuna")
        assert len(hits) == 1
        hit = hits[0]
        assert isinstance(hit, HistoryHit)
        assert hit.message_id == mid
        assert hit.conversation_id == c.id
        assert hit.archived is False
        assert hit.role == "user"
        assert hit.created_at is not None
        # conversation_name mirrors conversations.name (None when unset).
        assert hit.conversation_name is None
        assert isinstance(hit.snippet, str)

    async def test_ranked_by_relevance(self, store: Store) -> None:
        c = await store.get_or_create_conversation("discord", "t1")
        heavy = await store.append_message(c.id, "user", "tuna tuna tuna tuna")
        filler = (
            "a long rambling note about soup weather bicycles and the museum "
            "trip we planned for the autumn holidays with many extra words "
        ) * 3
        await store.append_message(c.id, "user", filler + "plus one mention of tuna")

        hits = await store.search_history("tuna")
        assert len(hits) == 2
        # bm25: the short, term-dense row ranks first.
        assert hits[0].message_id == heavy

    async def test_snippet_contains_match_context(self, store: Store) -> None:
        c = await store.get_or_create_conversation("discord", "t1")
        await store.append_message(
            c.id, "user", "yesterday we decided the aardwolf logo should be blue"
        )

        hits = await store.search_history("aardwolf")
        assert len(hits) == 1
        assert isinstance(hits[0].snippet, str)
        assert hits[0].snippet.strip() != ""
        assert "aardwolf" in hits[0].snippet.lower()

    async def test_no_match_returns_empty_list(self, store: Store) -> None:
        c = await store.get_or_create_conversation("discord", "t1")
        await store.append_message(c.id, "user", "completely unrelated words")
        assert await store.search_history("xylotheque") == []

    async def test_empty_db_returns_empty_list(self, store: Store) -> None:
        assert await store.search_history("anything") == []

    async def test_tool_rows_never_hit(self, store: Store) -> None:
        c = await store.get_or_create_conversation("discord", "t1")
        await store.append_message(c.id, "tool", "raw payload mentioning cormorant")
        assert await store.search_history("cormorant") == []


# --- search_history: limit ------------------------------------------------


class TestSearchHistoryLimit:
    async def _seed_many(self, store: Store, n: int) -> None:
        c = await store.get_or_create_conversation("discord", "t1")
        for i in range(n):
            await store.append_message(c.id, "user", f"note {i} about the pelican census")

    async def test_default_limit_is_8(self, store: Store) -> None:
        await self._seed_many(store, 12)
        hits = await store.search_history("pelican")
        assert len(hits) == 8

    async def test_explicit_limit(self, store: Store) -> None:
        await self._seed_many(store, 12)
        hits = await store.search_history("pelican", limit=3)
        assert len(hits) == 3

    async def test_limit_above_result_count_returns_all(self, store: Store) -> None:
        await self._seed_many(store, 2)
        hits = await store.search_history("pelican", limit=25)
        assert len(hits) == 2


# --- search_history: conversation filter + archived flag ------------------


class TestSearchHistoryScope:
    async def test_conversation_id_filter(self, store: Store) -> None:
        ca = await store.get_or_create_conversation("discord", "t-a")
        cb = await store.get_or_create_conversation("discord", "t-b")
        await store.append_message(ca.id, "user", "the ocelot report from thread a")
        await store.append_message(cb.id, "user", "the ocelot report from thread b")

        all_hits = await store.search_history("ocelot")
        assert {h.conversation_id for h in all_hits} == {ca.id, cb.id}

        filtered = await store.search_history("ocelot", conversation_id=ca.id)
        assert len(filtered) == 1
        assert filtered[0].conversation_id == ca.id

    async def test_archived_conversations_included_and_flagged(self, store: Store) -> None:
        live = await store.get_or_create_conversation("discord", "t-live")
        gone = await store.get_or_create_conversation("discord", "t-gone")
        await store.append_message(live.id, "user", "ibex sighting in the live thread")
        await store.append_message(gone.id, "user", "ibex sighting in the deleted thread")
        assert await store.archive_conversation("discord", "t-gone", now=_now()) is True

        hits = await store.search_history("ibex")
        by_conv = {h.conversation_id: h for h in hits}
        assert set(by_conv) == {live.id, gone.id}
        assert by_conv[gone.id].archived is True
        assert by_conv[live.id].archived is False


# --- search_history: malformed-query fallback -----------------------------


class TestSearchHistoryMalformedQuery:
    async def test_fts_syntax_error_falls_back_to_literal_terms(self, store: Store) -> None:
        c = await store.get_or_create_conversation("discord", "t1")
        mid = await store.append_message(c.id, "user", "this unbalanced quote thing came up")

        # `unbalanced (quote` is invalid FTS5 syntax (unclosed group).
        # UoW-27 step 3: retry once with every whitespace-separated term
        # double-quoted — a literal AND search that matches the row.
        hits = await store.search_history("unbalanced (quote")
        assert [h.message_id for h in hits] == [mid]

    async def test_trailing_lone_double_quote_does_not_raise(self, store: Store) -> None:
        c = await store.get_or_create_conversation("discord", "t1")
        await store.append_message(c.id, "user", "this unbalanced quote thing came up")

        # The nastier variant from the milestone brief. The fallback must
        # not surface an error; whether the dangling-quote "term" can
        # still match anything is not pinned — only that a list comes back.
        hits = await store.search_history('unbalanced (quote "')
        assert isinstance(hits, list)


# --- read-only guarantee ---------------------------------------------------


class TestReadOnly:
    async def test_search_and_window_issue_zero_writes(self, store: Store) -> None:
        c = await store.get_or_create_conversation("discord", "t1")
        mid = await store.append_message(c.id, "user", "read only wallaby check")
        await store.append_message(c.id, "assistant", "noted the wallaby")
        await store.archive_conversation("discord", "t1", now=_now())

        count_before = await store.message_count(c.id)
        changes_before = store._c.total_changes

        hits = await store.search_history("wallaby")
        assert hits  # the operations under test actually ran
        window = await store.history_window(mid)
        assert window is not None
        assert (await store.history_window(999_999)) is None
        assert await store.search_history("nothing-matches-this") == []

        # Zero writes: sqlite change counter untouched.
        assert store._c.total_changes == changes_before
        # And the observable state is unchanged.
        assert await store.message_count(c.id) == count_before
        conv = await store.get_conversation(c.id)
        assert conv is not None
        assert conv.archived_at is not None  # stamp survived searching


# --- history_window --------------------------------------------------------


async def _seed_window_conv(store: Store) -> tuple[int, list[int]]:
    """One conversation with 12 messages (user/assistant alternating,
    with a tool row at index 7). Returns (conversation_id, message_ids)."""
    c = await store.get_or_create_conversation("discord", "t-window")
    ids: list[int] = []
    for i in range(12):
        role = "tool" if i == 7 else ("user" if i % 2 == 0 else "assistant")
        ids.append(await store.append_message(c.id, role, f"m{i}"))
    return c.id, ids


class TestHistoryWindow:
    async def test_window_shape_and_meta(self, store: Store) -> None:
        conv_id, ids = await _seed_window_conv(store)
        w = await store.history_window(ids[6])
        assert w is not None
        assert isinstance(w, HistoryWindow)
        assert w.conversation_id == conv_id
        assert w.archived is False
        assert w.created_at is not None
        assert w.last_active is not None
        assert isinstance(w.messages, tuple)
        m = w.messages[0]
        # HistoryWindowMessage shape: id, role, created_at, content.
        assert m.id in ids
        assert m.role in ("user", "assistant", "tool")
        assert m.created_at is not None
        assert isinstance(m.content, str)

    async def test_window_ordered_around_anchor(self, store: Store) -> None:
        _, ids = await _seed_window_conv(store)
        w = await store.history_window(ids[6], before=3, after=3)
        assert w is not None
        assert [m.content for m in w.messages] == ["m3", "m4", "m5", "m6", "m7", "m8", "m9"]
        # Ordered oldest-first by id.
        got_ids = [m.id for m in w.messages]
        assert got_ids == sorted(got_ids)

    async def test_default_bounds_are_5_and_5(self, store: Store) -> None:
        _, ids = await _seed_window_conv(store)
        w = await store.history_window(ids[6])
        assert w is not None
        assert [m.content for m in w.messages] == [f"m{i}" for i in range(1, 12)]

    async def test_bounds_clip_at_conversation_start(self, store: Store) -> None:
        _, ids = await _seed_window_conv(store)
        w = await store.history_window(ids[1], before=5, after=1)
        assert w is not None
        assert [m.content for m in w.messages] == ["m0", "m1", "m2"]

    async def test_zero_bounds_yield_only_anchor(self, store: Store) -> None:
        # Documented choice: the anchor itself is part of the window.
        _, ids = await _seed_window_conv(store)
        w = await store.history_window(ids[6], before=0, after=0)
        assert w is not None
        assert [m.content for m in w.messages] == ["m6"]

    async def test_tool_rows_appear_in_window(self, store: Store) -> None:
        _, ids = await _seed_window_conv(store)
        w = await store.history_window(ids[7], before=1, after=1)
        assert w is not None
        roles = [m.role for m in w.messages]
        assert "tool" in roles
        anchor = next(m for m in w.messages if m.id == ids[7])
        assert anchor.role == "tool"
        assert anchor.content == "m7"

    async def test_window_flags_archived_conversation(self, store: Store) -> None:
        _, ids = await _seed_window_conv(store)
        await store.archive_conversation("discord", "t-window", now=_now())
        w = await store.history_window(ids[6])
        assert w is not None
        assert w.archived is True

    async def test_unknown_message_id_returns_none(self, store: Store) -> None:
        await _seed_window_conv(store)
        assert (await store.history_window(999_999)) is None

    async def test_oversized_content_truncated_at_4000_with_marker(self, store: Store) -> None:
        c = await store.get_or_create_conversation("discord", "t-big")
        original = "start-marker " + "x" * 5987 + " end-marker"  # 6011 chars
        mid = await store.append_message(c.id, "user", original)

        w = await store.history_window(mid, before=0, after=0)
        assert w is not None
        content = w.messages[0].content
        assert content != original
        # First 4000 chars are kept, then a marker is appended.
        assert content.startswith(original[:4000])
        assert len(content) > 4000
        assert len(content) < len(original)
        # The far tail never survives, and the marker is visible (the
        # content no longer ends inside the x-run).
        assert "end-marker" not in content
        assert not content.endswith("x")

    async def test_short_content_not_truncated(self, store: Store) -> None:
        c = await store.get_or_create_conversation("discord", "t-small")
        mid = await store.append_message(c.id, "user", "short and sweet")
        w = await store.history_window(mid, before=0, after=0)
        assert w is not None
        assert w.messages[0].content == "short and sweet"
