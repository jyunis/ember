"""Router-mode `## Your tools` catalog block (tool-awareness fix).

The router prompt used to advertise only skills, so the model would deny
having integrations it actually has ("no Mealie tool in my registry")
when the tool was registered and callable. `build_tools_catalog_block`
enumerates the registry (minus internal plumbing) so the model knows its
full action surface. Pure function — no Agent/Store/Provider.
"""

from __future__ import annotations

from ember.skills.router_prompt import (
    DEFAULT_CATALOG_EXCLUDE,
    build_tools_catalog_block,
)


class _FakeTool:
    def __init__(self, name: str, description: str = "") -> None:
        self.name = name
        self.description = description


class _FakeRegistry:
    def __init__(self, tools: list[_FakeTool]) -> None:
        self._tools = tools

    def list_tools(self) -> list[_FakeTool]:
        return list(self._tools)


def _registry() -> _FakeRegistry:
    return _FakeRegistry(
        [
            _FakeTool("mealie_add_recipe", "Import a recipe into Mealie by URL.\nsecond line"),
            _FakeTool("gmail_send", "Send an email."),
            _FakeTool("web_search", "Search the web via Brave."),
            _FakeTool("echo", "Test echo tool."),  # excluded
            _FakeTool("mcp_connect", "Mount an external MCP."),  # excluded
            _FakeTool("profile_update", "Edit the user profile."),  # excluded
        ]
    )


def test_lists_domain_tools_with_first_line_desc() -> None:
    block = build_tools_catalog_block(_registry())
    assert block.startswith("## Your tools")
    assert "- `mealie_add_recipe` — Import a recipe into Mealie by URL." in block
    assert "- `gmail_send` — Send an email." in block
    assert "- `web_search` — Search the web via Brave." in block
    # Only the first line of a multi-line description.
    assert "second line" not in block
    # Anti-hedge guidance present.
    assert "you DO have" in block or "lack an integration" in block


def test_excludes_internal_plumbing() -> None:
    block = build_tools_catalog_block(_registry())
    assert "- `echo`" not in block
    assert "- `mcp_connect`" not in block
    assert "- `profile_update`" not in block


def test_name_sorted_for_stability() -> None:
    block = build_tools_catalog_block(_registry())
    # gmail_send sorts before mealie_add_recipe before web_search.
    assert block.index("gmail_send") < block.index("mealie_add_recipe") < block.index("web_search")
    # Byte-stable across calls.
    reg = _registry()
    assert build_tools_catalog_block(reg) == build_tools_catalog_block(reg)


def test_empty_when_all_excluded_or_none() -> None:
    assert build_tools_catalog_block(_FakeRegistry([])) == ""
    only_excluded = _FakeRegistry([_FakeTool(n) for n in DEFAULT_CATALOG_EXCLUDE])
    assert build_tools_catalog_block(only_excluded) == ""


def test_custom_exclude_overrides_default() -> None:
    block = build_tools_catalog_block(_registry(), exclude=frozenset({"gmail_send"}))
    assert "- `gmail_send`" not in block
    # echo no longer excluded under the custom set.
    assert "- `echo`" in block
