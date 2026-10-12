"""skill_read — body + companion-file access for the agent."""

from __future__ import annotations

from pathlib import Path

import pytest

from ember.skills.base import DeclarativeSkill
from ember.skills.registry import InMemorySkillRegistry
from ember.tools.registry import Registry
from ember.tools.skill_read import SkillReadTool


@pytest.fixture
def skill_dir(tmp_path: Path) -> Path:
    d = tmp_path / "wire-wallet"
    (d / "references").mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: example-skill\ndescription: x\n---\n# Example skill body\n")
    (d / "references" / "setup.md").write_text("MCP URL: https://example.test/mcp\n")
    (d / "references" / "api.md").write_text("# API\n")
    return d


@pytest.fixture
def skill_registry(skill_dir: Path) -> InMemorySkillRegistry:
    reg = InMemorySkillRegistry()
    reg.replace(
        DeclarativeSkill(
            name="wire-wallet",
            description="manages a wallet",
            tools=(),
            system_prompt="# Example skill body\nSee references/setup.md for setup.",
            source_path=str(skill_dir / "SKILL.md"),
        )
    )
    return reg


@pytest.fixture
def registry(skill_registry: InMemorySkillRegistry) -> Registry:
    r = Registry()
    r.register(SkillReadTool(skill_registry=skill_registry))
    return r


class TestReadBody:
    async def test_returns_body_and_metadata(self, registry: Registry) -> None:
        result = await registry.call_tool("skill_read", {"name": "wire-wallet"})
        assert result.is_error is False
        out = result.output
        assert out["name"] == "wire-wallet"
        assert "Example skill body" in out["body"]
        assert out["truncated"] is False
        assert "references/setup.md" in out["available_files"]
        assert "references/api.md" in out["available_files"]
        # SKILL.md is excluded from the listing.
        assert "SKILL.md" not in out["available_files"]


class TestReadFile:
    async def test_returns_companion_file(self, registry: Registry) -> None:
        result = await registry.call_tool(
            "skill_read", {"name": "wire-wallet", "file": "references/setup.md"}
        )
        assert result.is_error is False
        assert "MCP URL: https://example.test/mcp" in result.output["body"]

    async def test_rejects_path_traversal(self, registry: Registry) -> None:
        result = await registry.call_tool(
            "skill_read", {"name": "wire-wallet", "file": "../etc/passwd"}
        )
        assert result.is_error
        assert result.output["error"] == "invalid_path"

    async def test_rejects_backslashes(self, registry: Registry) -> None:
        result = await registry.call_tool(
            "skill_read", {"name": "wire-wallet", "file": "references\\setup.md"}
        )
        assert result.is_error
        assert result.output["error"] == "invalid_path"

    async def test_missing_file_is_invalid_path(self, registry: Registry) -> None:
        result = await registry.call_tool(
            "skill_read", {"name": "wire-wallet", "file": "references/missing.md"}
        )
        assert result.is_error
        assert result.output["error"] == "invalid_path"


class TestUnknownSkill:
    async def test_lists_available(self, registry: Registry) -> None:
        result = await registry.call_tool("skill_read", {"name": "does-not-exist"})
        assert result.is_error
        assert result.output["error"] == "unknown_skill"
        assert "wire-wallet" in result.output["available"]


class TestNoSkillDir:
    async def test_single_file_skill_rejects_file_arg(self) -> None:
        reg = InMemorySkillRegistry()
        reg.replace(
            DeclarativeSkill(
                name="single",
                description="x",
                system_prompt="body",
                source_path="/tmp/single.toml",
            )
        )
        tools = Registry()
        tools.register(SkillReadTool(skill_registry=reg))
        result = await tools.call_tool("skill_read", {"name": "single", "file": "x.md"})
        assert result.is_error
        assert result.output["error"] == "no_skill_dir"
