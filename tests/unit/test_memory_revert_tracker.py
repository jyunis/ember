"""MemoryRevertTracker — supports `/memory revert` (UoW-7).

Per UoW-7 + architecture decision #2:
- Agent memory writes are revertable; the tracker is the in-process
  pointer list of agent-written memory IDs.
- It's a per-process LIFO; lost on container restart (acceptable for an
  "oops" undo).
- Per-conversation filter: revert only affects the conversation it was
  invoked in.
- Bounded — the LIFO can't grow without bound.

The tracker's interface is tiny:

    record(memory_id: int, conversation_key: str)
    take_last(n: int, conversation_key: str | None = None) -> list[int]
    __len__()

Tester-flagged invariants we assert:
- `take_last` is destructive (it pops, not peeks). Per the docstring:
  "Pop and return up to `n` most-recent memory IDs."
- "Most recent" means LIFO — newest first.
- `take_last(0)` → `[]` and does not mutate.
- `take_last(N)` with fewer-than-N entries returns what's available.
- Bounded `max_size`: the oldest is evicted when the limit is hit.
- The conversation filter pops only matching entries; non-matching
  entries remain in the tracker.
"""

from __future__ import annotations


from ember.commands.memory import MemoryRevertTracker


# --- Basic record + take ------------------------------------------------


class TestRecordAndTake:
    def test_record_then_take_last_one(self) -> None:
        t = MemoryRevertTracker()
        t.record(101, "thread-1")
        assert t.take_last(1) == [101]

    def test_take_last_empties_on_take(self) -> None:
        t = MemoryRevertTracker()
        t.record(101, "thread-1")
        t.take_last(1)
        # Subsequent take returns empty — first call popped.
        assert t.take_last(1) == []

    def test_take_last_returns_lifo_order(self) -> None:
        t = MemoryRevertTracker()
        t.record(1, "thread-1")
        t.record(2, "thread-1")
        t.record(3, "thread-1")
        # Most-recent first: 3, then 2, then 1.
        assert t.take_last(3) == [3, 2, 1]

    def test_take_zero_returns_empty(self) -> None:
        t = MemoryRevertTracker()
        t.record(1, "thread-1")
        assert t.take_last(0) == []
        # ...and does not mutate.
        assert len(t) == 1

    def test_take_more_than_available_returns_what_is_there(self) -> None:
        t = MemoryRevertTracker()
        t.record(1, "thread-1")
        t.record(2, "thread-1")
        # take_last(5) on 2 entries: returns both, no error.
        assert t.take_last(5) == [2, 1]
        assert len(t) == 0

    def test_take_when_empty_returns_empty_list(self) -> None:
        t = MemoryRevertTracker()
        assert t.take_last(3) == []


# --- Bounded size -------------------------------------------------------


class TestBounded:
    def test_max_size_evicts_oldest(self) -> None:
        t = MemoryRevertTracker(max_size=3)
        t.record(1, "thread-1")
        t.record(2, "thread-1")
        t.record(3, "thread-1")
        t.record(4, "thread-1")  # evicts 1
        # Newest 3 remain — 4, 3, 2 (LIFO).
        out = t.take_last(10)
        assert 1 not in out
        assert set(out) == {2, 3, 4}
        # Order: most recent first.
        assert out[0] == 4

    def test_default_max_size_is_bounded(self) -> None:
        # Default is 256 per the constructor signature; assert that
        # after recording 257 entries, the first one is gone.
        t = MemoryRevertTracker()
        for i in range(300):
            t.record(i, "thread-1")
        # `len` may be capped at 256 (the default max).
        assert len(t) <= 256

    def test_len_tracks_entries(self) -> None:
        t = MemoryRevertTracker()
        assert len(t) == 0
        t.record(1, "thread-1")
        assert len(t) == 1
        t.record(2, "thread-2")
        assert len(t) == 2


# --- Per-conversation filter --------------------------------------------


class TestConversationFilter:
    def test_filter_pops_only_matching_conversation(self) -> None:
        t = MemoryRevertTracker()
        t.record(1, "thread-A")
        t.record(2, "thread-B")
        t.record(3, "thread-A")
        # Pop 1 most-recent A-only.
        out = t.take_last(1, conversation_key="thread-A")
        assert out == [3]
        # B's entry untouched.
        remaining = t.take_last(10)
        assert 2 in remaining

    def test_filter_pops_lifo_within_conversation(self) -> None:
        t = MemoryRevertTracker()
        t.record(1, "thread-A")
        t.record(2, "thread-B")
        t.record(3, "thread-A")
        t.record(4, "thread-B")
        t.record(5, "thread-A")
        out = t.take_last(2, conversation_key="thread-A")
        assert out == [5, 3]
        # The remaining tracker should still have B's entries plus
        # any unmatched A entries.
        remaining = t.take_last(10)
        assert 2 in remaining
        assert 4 in remaining
        assert 1 in remaining
        assert 3 not in remaining
        assert 5 not in remaining

    def test_filter_returns_empty_when_no_matches(self) -> None:
        t = MemoryRevertTracker()
        t.record(1, "thread-A")
        t.record(2, "thread-A")
        out = t.take_last(2, conversation_key="thread-Z")
        assert out == []
        # And nothing in the tracker was popped.
        assert len(t) == 2

    def test_unfiltered_take_pops_across_conversations(self) -> None:
        # Sanity: without a key, take_last operates globally.
        t = MemoryRevertTracker()
        t.record(1, "thread-A")
        t.record(2, "thread-B")
        out = t.take_last(2)
        assert set(out) == {1, 2}
