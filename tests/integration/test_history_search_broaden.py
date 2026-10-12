"""history_search zero-hit OR-broadening (doc #54 decision #9).

A plain multi-word query is an implicit AND in FTS5 — one absent word
hides a real hit. On zero hits the tool retries once with the terms
OR-ed and labels the result `match: "broadened"`; exact matches are
labeled `match: "exact"`. Queries using FTS5 syntax deliberately, and
single-term queries, are never rewritten.

Motivating live failure: `history_search("itchy rash bug bites")`
returned nothing while the archive held the answer under "bites" +
"itch mites" — no message contained "rash" or "bug".
"""

from __future__ import annotations

import pytest

from ember.store.repo import Store
from ember.tools.history import HistorySearchTool


@pytest.fixture
async def store(tmp_path):
    s = Store(tmp_path / "conversations.db")
    await s.open()
    try:
        yield s
    finally:
        await s.close()


@pytest.fixture
async def seeded(store: Store):
    """One conversation about summer bites — deliberately containing
    'bites' and 'itch' but neither 'rash' nor 'bug'."""
    c = await store.get_or_create_conversation("discord", "thread-1")
    await store.append_message(c.id, "user", "I get these bites every summer and they itch badly")
    await store.append_message(c.id, "assistant", "sounds like oak leaf itch mites to me")
    return store


async def test_exact_match_is_labeled_exact(seeded: Store) -> None:
    tool = HistorySearchTool(store=seeded)
    result = await tool.call({"query": "bites"})
    assert result.output["match"] == "exact"
    assert result.output["count"] >= 1


async def test_zero_hit_multiword_broadens_to_or(seeded: Store) -> None:
    # AND-semantics miss: no single message has all four words.
    tool = HistorySearchTool(store=seeded)
    result = await tool.call({"query": "itchy rash bug bites"})
    assert result.output["match"] == "broadened"
    assert result.output["count"] >= 1
    snippets = " ".join(h["snippet"] for h in result.output["hits"])
    assert "bites" in snippets or "itch" in snippets


async def test_broadened_but_still_empty_stays_labeled(seeded: Store) -> None:
    tool = HistorySearchTool(store=seeded)
    result = await tool.call({"query": "zeppelin quasar marmalade"})
    assert result.output["match"] == "broadened"
    assert result.output["count"] == 0


async def test_single_term_zero_hit_is_not_broadened(seeded: Store) -> None:
    tool = HistorySearchTool(store=seeded)
    result = await tool.call({"query": "zeppelin"})
    assert result.output["match"] == "exact"
    assert result.output["count"] == 0


async def test_explicit_fts_syntax_is_never_rewritten(seeded: Store) -> None:
    tool = HistorySearchTool(store=seeded)
    # Deliberate OR query that already found nothing: leave it alone.
    result = await tool.call({"query": "zeppelin OR quasar"})
    assert result.output["match"] == "exact"
    assert result.output["count"] == 0
    # Quoted-phrase query: also left alone.
    result = await tool.call({"query": '"bug bites rash"'})
    assert result.output["match"] == "exact"
    assert result.output["count"] == 0


async def test_broadening_respects_conversation_filter(seeded: Store) -> None:
    other = await seeded.get_or_create_conversation("discord", "thread-2")
    await seeded.append_message(other.id, "user", "unrelated chatter about gardens")
    conv = await seeded.get_or_create_conversation("discord", "thread-1")
    tool = HistorySearchTool(store=seeded)
    result = await tool.call({"query": "itchy rash bug bites", "conversation_id": conv.id})
    assert result.output["match"] == "broadened"
    assert all(h["conversation_id"] == conv.id for h in result.output["hits"])
