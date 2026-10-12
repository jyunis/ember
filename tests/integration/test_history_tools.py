"""History tools (`tools/history.py`, UoW-27 / FC-27) — integration.

The two always-registered, read-only tools, constructed with the
process Store (FC-27: explicitly-injected cross-conversation read
access, Decision #4) and exercised through the `Tool` protocol and the
registry (TLC-6 schema validation):

- `requires_confirmation` is False for both.
- Input schemas match FC-27: `history_search` {query required, limit
  1–25 default 8, conversation_id optional}; `history_fetch`
  {message_id required, before/after 0–12 default 5, combined
  before+after ≤ 12 per call — Decision #8, bridge-cap budget}.
- Registry rejects schema-invalid args with `ToolValidationError`
  before the tool runs.
- Empty/whitespace `query` → `ToolError`; unknown `message_id` →
  `ToolError` naming the id.
- Happy-path result shapes carry the FC-27 fields.

Naming assumptions the implementer must honor (or flag): class names
`HistorySearchTool` / `HistoryFetchTool` in `ember.tools.history`,
constructed with `store=` keyword (house convention: ImportSkillTool,
SendFileTool, McpConnectTool). FC-27 pins the module path and tool
names but not the class names.

Documented choice: FC-27 does not pin the exact ToolResult.output
container (bare list vs {"hits": [...]}, etc.), so shape assertions
locate the hit/message list leniently and then require the FC-27
field names on each entry.
"""

from __future__ import annotations

from typing import Any

import pytest

from ember.errors import ToolError, ToolValidationError
from ember.store.repo import Store
from ember.tools.base import Tool, ToolResult
from ember.tools.history import HistoryFetchTool, HistorySearchTool
from ember.tools.registry import Registry


# --- Fixtures -------------------------------------------------------------


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
    """Store with one conversation and a known anchor message. Returns
    (store, conversation, anchor_message_id)."""
    c = await store.get_or_create_conversation("discord", "thread-1")
    await store.append_message(c.id, "user", "let us discuss the narwhal budget")
    anchor = await store.append_message(c.id, "assistant", "the narwhal budget is approved")
    await store.append_message(c.id, "tool", "tool output rows are fetchable")
    await store.append_message(c.id, "user", "thanks for the narwhal update")
    return store, c, anchor


def _field(item: Any, name: str) -> Any:
    """Read a field off a hit/message entry whether it is a dict or an
    object (the output container shape is not pinned by FC-27)."""
    if isinstance(item, dict):
        assert name in item, f"missing field {name!r} in {item!r}"
        return item[name]
    assert hasattr(item, name), f"missing field {name!r} on {item!r}"
    return getattr(item, name)


def _find_list(output: Any) -> list[Any]:
    """Locate the list of entries in a tool result output."""
    if isinstance(output, list):
        return output
    assert isinstance(output, dict), f"unexpected output shape: {output!r}"
    for key in ("hits", "results", "matches", "messages"):
        if isinstance(output.get(key), list):
            return output[key]
    lists = [v for v in output.values() if isinstance(v, list)]
    assert lists, f"no list of entries found in output: {output!r}"
    return lists[0]


def _window_messages(output: Any) -> list[Any]:
    """Locate the message-window list in a history_fetch output."""
    if isinstance(output, dict) and isinstance(output.get("messages"), list):
        return output["messages"]
    if not isinstance(output, dict) and hasattr(output, "messages"):
        return list(output.messages)
    return _find_list(output)


# --- Tool protocol surface -------------------------------------------------


class TestToolSurface:
    async def test_search_tool_protocol_and_confirmation(self, store: Store) -> None:
        tool = HistorySearchTool(store=store)
        assert isinstance(tool, Tool)
        assert tool.name == "history_search"
        assert tool.requires_confirmation is False
        assert isinstance(tool.description, str) and tool.description.strip()

    async def test_fetch_tool_protocol_and_confirmation(self, store: Store) -> None:
        tool = HistoryFetchTool(store=store)
        assert isinstance(tool, Tool)
        assert tool.name == "history_fetch"
        assert tool.requires_confirmation is False
        assert isinstance(tool.description, str) and tool.description.strip()

    async def test_search_input_schema_matches_fc27(self, store: Store) -> None:
        schema = HistorySearchTool(store=store).input_schema
        props = schema["properties"]
        assert set(schema["required"]) == {"query"}
        assert props["query"]["type"] == "string"
        assert props["limit"]["type"] == "integer"
        assert props["limit"]["minimum"] == 1
        assert props["limit"]["maximum"] == 25
        assert props["conversation_id"]["type"] == "integer"

    async def test_fetch_input_schema_matches_fc27(self, store: Store) -> None:
        schema = HistoryFetchTool(store=store).input_schema
        props = schema["properties"]
        assert set(schema["required"]) == {"message_id"}
        assert props["message_id"]["type"] == "integer"
        for bound in ("before", "after"):
            assert props[bound]["type"] == "integer"
            assert props[bound]["minimum"] == 0
            assert props[bound]["maximum"] == 12


# --- TLC-6: registry schema validation -------------------------------------


class TestRegistryValidation:
    @pytest.fixture
    def registry(self, store: Store) -> Registry:
        reg = Registry()
        reg.register(HistorySearchTool(store=store))
        reg.register(HistoryFetchTool(store=store))
        return reg

    async def test_search_missing_query_rejected(self, registry: Registry) -> None:
        with pytest.raises(ToolValidationError):
            await registry.call_tool("history_search", {})

    async def test_search_query_wrong_type_rejected(self, registry: Registry) -> None:
        with pytest.raises(ToolValidationError):
            await registry.call_tool("history_search", {"query": 123})

    @pytest.mark.parametrize("limit", [0, 26, -1])
    async def test_search_limit_out_of_bounds_rejected(self, registry: Registry, limit) -> None:
        with pytest.raises(ToolValidationError):
            await registry.call_tool("history_search", {"query": "x", "limit": limit})

    async def test_fetch_missing_message_id_rejected(self, registry: Registry) -> None:
        with pytest.raises(ToolValidationError):
            await registry.call_tool("history_fetch", {})

    @pytest.mark.parametrize(
        "args",
        [
            {"message_id": 1, "before": -1},
            {"message_id": 1, "before": 13},
            {"message_id": 1, "after": -1},
            {"message_id": 1, "after": 13},
        ],
    )
    async def test_fetch_bounds_out_of_range_rejected(self, registry: Registry, args) -> None:
        with pytest.raises(ToolValidationError):
            await registry.call_tool("history_fetch", args)


# --- ToolError paths --------------------------------------------------------


class TestToolErrors:
    async def test_empty_query_raises_tool_error(self, store: Store) -> None:
        tool = HistorySearchTool(store=store)
        with pytest.raises(ToolError):
            await tool.call({"query": ""})

    async def test_whitespace_query_raises_tool_error(self, store: Store) -> None:
        tool = HistorySearchTool(store=store)
        with pytest.raises(ToolError):
            await tool.call({"query": "   \t "})

    async def test_unknown_message_id_raises_tool_error_naming_id(self, store: Store) -> None:
        tool = HistoryFetchTool(store=store)
        with pytest.raises(ToolError) as excinfo:
            await tool.call({"message_id": 987654})
        assert "987654" in str(excinfo.value)

    async def test_fetch_combined_window_over_budget_raises_tool_error(self, store: Store) -> None:
        # Decision #8: each bound passes the 0–12 schema, but the combined
        # before+after budget (12) is enforced at the tool with ToolError.
        tool = HistoryFetchTool(store=store)
        with pytest.raises(ToolError):
            await tool.call({"message_id": 1, "before": 7, "after": 6})

    async def test_fetch_combined_window_at_budget_allowed(self, store: Store) -> None:
        # Exactly at budget the call proceeds past the guard (unknown id
        # then fails for the ordinary reason, naming the id).
        tool = HistoryFetchTool(store=store)
        with pytest.raises(ToolError) as excinfo:
            await tool.call({"message_id": 987654, "before": 6, "after": 6})
        assert "987654" in str(excinfo.value)


# --- Happy paths ------------------------------------------------------------


class TestSearchHappyPath:
    async def test_search_result_shape(self, seeded) -> None:
        store, c, _anchor = seeded
        tool = HistorySearchTool(store=store)

        result = await tool.call({"query": "narwhal"})
        assert isinstance(result, ToolResult)
        assert result.is_error is False

        hits = _find_list(result.output)
        assert len(hits) == 3  # two user rows + one assistant row
        for hit in hits:
            assert _field(hit, "conversation_id") == c.id
            assert _field(hit, "archived") is False
            assert _field(hit, "role") in ("user", "assistant")
            assert _field(hit, "message_id")
            assert _field(hit, "created_at")
            snippet = _field(hit, "snippet")
            assert isinstance(snippet, str) and snippet.strip()
            # conversation_name present (None: the conversation is unnamed).
            _field(hit, "conversation_name")

    async def test_search_limit_respected_through_registry(self, seeded) -> None:
        store, _c, _anchor = seeded
        registry = Registry()
        registry.register(HistorySearchTool(store=store))

        result = await registry.call_tool("history_search", {"query": "narwhal", "limit": 1})
        assert result.is_error is False
        assert len(_find_list(result.output)) == 1

    async def test_search_no_hits_is_not_an_error(self, seeded) -> None:
        store, _c, _anchor = seeded
        tool = HistorySearchTool(store=store)
        result = await tool.call({"query": "no-such-term-anywhere"})
        assert result.is_error is False
        assert _find_list(result.output) == []


class TestFetchHappyPath:
    async def test_fetch_result_shape(self, seeded) -> None:
        store, c, anchor = seeded
        tool = HistoryFetchTool(store=store)

        result = await tool.call({"message_id": anchor})
        assert isinstance(result, ToolResult)
        assert result.is_error is False

        out = result.output
        assert _field(out, "conversation_id") == c.id
        assert _field(out, "archived") is False
        assert _field(out, "created_at")
        assert _field(out, "last_active")

        messages = _window_messages(out)
        assert messages, "window should contain the anchor and its neighbors"
        contents = [_field(m, "content") for m in messages]
        assert "the narwhal budget is approved" in contents
        # Tool rows are part of the window even though they are not indexed.
        roles = [_field(m, "role") for m in messages]
        assert "tool" in roles
        for m in messages:
            assert _field(m, "id")
            assert _field(m, "role") in ("user", "assistant", "system", "tool")
            assert _field(m, "created_at")
            assert isinstance(_field(m, "content"), str)

    async def test_fetch_bounds_respected(self, seeded) -> None:
        store, _c, anchor = seeded
        tool = HistoryFetchTool(store=store)
        result = await tool.call({"message_id": anchor, "before": 0, "after": 0})
        assert result.is_error is False
        messages = _window_messages(result.output)
        # Documented choice (also pinned at Store level): the anchor is
        # included in its own window.
        assert len(messages) == 1
        assert _field(messages[0], "content") == "the narwhal budget is approved"
