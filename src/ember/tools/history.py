"""History tools (v=14, UoW-27 / FC-27) — search + fetch over the archive.

Two always-registered, read-only tools modeled on the memory-MCP pair
`semantic_search` + `get_memory`:

- `history_search` — bm25-ranked FTS5 keyword search across EVERY
  conversation ever stored, archived included (Decision #7: hits are
  labeled, never filtered).
- `history_fetch`  — the ordered message window around a hit, so a
  search result can be pulled into context.

Construction takes the process `Store` (precedent: `mcp_connector`).
Per FC-27 this *clarifies* FC-3's "no conversation-state access" clause:
explicitly-injected, read-only, cross-conversation access is the entire
point — the tools never learn which conversation invoked them.
"""

from __future__ import annotations

from typing import Any

from ember.errors import StoreError, ToolError
from ember.store.repo import Store
from ember.tools._fts_terms import _FTS_KEYWORDS, _FTS_SYNTAX_CHARS, _plain_terms  # noqa: F401
from ember.tools.base import ToolResult

# FC-27 (Decision #8): combined before+after ceiling per history_fetch call,
# sized so the worst-case serialized result stays under the MCP bridge's
# 60KB per-result cap (FC-4 / UoW-4 step 4).
_WINDOW_BUDGET = 12

# `_plain_terms` (and the syntax-char / keyword sets it consults) lives in
# `tools/_fts_terms.py` since doc #59 so `docs_search` applies the identical
# OR-broadening rule; re-exported here so the names keep resolving.
__all__ = ["HistorySearchTool", "HistoryFetchTool", "_plain_terms"]


class _HistoryToolBase:
    requires_confirmation: bool = False

    def __init__(self, *, store: Store) -> None:
        self._store = store


class HistorySearchTool(_HistoryToolBase):
    name = "history_search"
    description = (
        "Keyword-search the full archive of past conversations — including "
        "conversations whose Discord threads were deleted (archived; their "
        "transcripts are kept forever). Use this when something was "
        "discussed before but is NOT in memory and NOT in the current "
        'conversation\'s context — e.g. "what did we decide about X?", a '
        "name/number/URL mentioned weeks ago, or anything from a dead "
        "thread. Returns bm25-ranked snippet hits with message ids; pass a "
        "hit's message_id to history_fetch to read the surrounding "
        "exchange. Results label archived conversations but never exclude "
        "them. Multiple words must ALL appear in one message, so prefer "
        "1-2 distinctive terms; if a plain multi-word query matches "
        "nothing it is automatically retried with the terms OR-ed and the "
        "result is labeled match='broadened' (treat broadened hits as "
        "leads to verify via history_fetch, not exact matches)."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Search terms (FTS5 keyword match over user/assistant "
                    "message content). Plain words work best — every word "
                    "must match, so fewer distinctive terms beat many. A "
                    "query with unbalanced quotes or operator characters "
                    "is retried as a literal search automatically."
                ),
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 25,
                "default": 8,
                "description": "Maximum hits to return (default 8).",
            },
            "conversation_id": {
                "type": "integer",
                "description": (
                    "Optional: restrict the search to one conversation id "
                    "(as returned in earlier hits)."
                ),
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    async def call(self, args: dict[str, Any]) -> ToolResult:
        query = args["query"]
        if not query.strip():
            raise ToolError("history_search: query must be non-empty")
        limit = int(args.get("limit", 8))
        conversation_id = args.get("conversation_id")
        conv_id = int(conversation_id) if conversation_id is not None else None
        try:
            hits = await self._store.search_history(query, limit=limit, conversation_id=conv_id)
        except StoreError as e:
            raise ToolError(f"history_search failed: {e}") from e

        # Zero-hit broadening: a plain multi-word query is an implicit AND
        # in FTS5, and one absent word turns "found it" into "never
        # happened". Retry once with the terms OR-ed (quoted, so operator
        # characters stay literal) and say so in the result — the model
        # must treat broadened hits as leads, not exact matches.
        match_kind = "exact"
        if not hits:
            terms = _plain_terms(query)
            if terms is not None:
                or_expr = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
                try:
                    hits = await self._store.search_history(
                        or_expr, limit=limit, conversation_id=conv_id
                    )
                except StoreError as e:
                    raise ToolError(f"history_search failed: {e}") from e
                match_kind = "broadened"

        return ToolResult(
            output={
                "query": query,
                "match": match_kind,
                "count": len(hits),
                "hits": [
                    {
                        "message_id": h.message_id,
                        "conversation_id": h.conversation_id,
                        "conversation_name": h.conversation_name,
                        "archived": h.archived,
                        "role": h.role,
                        "created_at": h.created_at.isoformat(),
                        "snippet": h.snippet,
                    }
                    for h in hits
                ],
            }
        )


class HistoryFetchTool(_HistoryToolBase):
    name = "history_fetch"
    description = (
        "Fetch the exchange surrounding one message from a past "
        "conversation (found via history_search): conversation metadata "
        "plus an ordered window of messages before and after the anchor "
        "message id. All roles appear in the window — including tool and "
        "system rows that keyword search does not index — so you can see "
        "the full surrounding record. Long messages are truncated. The "
        "combined before+after window is capped at 12 messages per call; "
        "to read more of a discussion, page by re-anchoring on the first "
        "or last message id of the previous window."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "message_id": {
                "type": "integer",
                "description": "Anchor message id, from a history_search hit.",
            },
            "before": {
                "type": "integer",
                "minimum": 0,
                "maximum": 12,
                "default": 5,
                "description": "Messages to include before the anchor (default 5).",
            },
            "after": {
                "type": "integer",
                "minimum": 0,
                "maximum": 12,
                "default": 5,
                "description": "Messages to include after the anchor (default 5).",
            },
        },
        "required": ["message_id"],
        "additionalProperties": False,
    }

    async def call(self, args: dict[str, Any]) -> ToolResult:
        message_id = int(args["message_id"])
        before = int(args.get("before", 5))
        after = int(args.get("after", 5))
        # FC-27 window budget: worst-case serialized output must stay under
        # the bridge's 60KB per-result cap (FC-4). 12 + anchor messages at
        # the 4000-char truncation ceiling ≈ 53KB.
        if before + after > _WINDOW_BUDGET:
            raise ToolError(
                f"history_fetch: before+after may total at most {_WINDOW_BUDGET} "
                f"messages per call (got {before}+{after}); page with repeated "
                "calls instead"
            )
        try:
            window = await self._store.history_window(message_id, before=before, after=after)
        except StoreError as e:
            raise ToolError(f"history_fetch failed: {e}") from e
        if window is None:
            raise ToolError(f"history_fetch: no message with id {message_id}")
        return ToolResult(
            output={
                "conversation_id": window.conversation_id,
                "conversation_name": window.conversation_name,
                "archived": window.archived,
                "created_at": window.created_at.isoformat(),
                "last_active": window.last_active.isoformat(),
                "anchor_message_id": message_id,
                "messages": [
                    {
                        "id": m.id,
                        "role": m.role,
                        "created_at": m.created_at.isoformat(),
                        "content": m.content,
                    }
                    for m in window.messages
                ],
            }
        )
