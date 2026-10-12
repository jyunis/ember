"""Shim skill `scripts/skills/docs_sync.py` (UoW-33 / FC-36).

Properties under test:

- The file loads through `DirectorySkillLoader` when copied into a
  skills dir: name `docs_sync`, `tools == ("docs_sync",)`, no
  `system_prompt`, exactly one `scheduled_runs` entry with cron
  `*/15 * * * *`, timezone `America/Chicago`, function/name `sync`,
  timeout 240, misfire grace 600, coalesce True.
- `sync(ctx)` calls `ctx.tools.call_tool("docs_sync", {})` and returns
  the tool's `output` dict; an `is_error` result raises.
- TLC-29: the skill body never touches `ctx.router` (the stub ctx has no
  router attribute at all — any access would raise AttributeError).

Documented choice: the loader normalizes `tools` to a tuple, so the
interface's `tools == ["docs_sync"]` is asserted as `("docs_sync",)`;
the raw file text is also checked for the literal list.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ember.skills.base import ImperativeSkill
from ember.skills.loader import DirectorySkillLoader
from ember.skills.registry import InMemorySkillRegistry
from ember.tools.base import ToolResult

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_SRC = REPO_ROOT / "scripts" / "skills" / "docs_sync.py"


@pytest.fixture
async def skill(tmp_path: Path) -> ImperativeSkill:
    assert SKILL_SRC.exists(), f"missing shim skill at {SKILL_SRC}"
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    shutil.copy(SKILL_SRC, skills_dir / "docs_sync.py")
    registry = InMemorySkillRegistry()
    loader = DirectorySkillLoader(skills_dir, registry)
    await loader.load_all()
    return registry.get("docs_sync")


class _Tools:
    def __init__(self, result: ToolResult) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, args: dict[str, Any], **kwargs: Any) -> ToolResult:
        self.calls.append((name, dict(args)))
        return self.result


class TestLoads:
    async def test_manifest(self, skill: ImperativeSkill) -> None:
        assert isinstance(skill, ImperativeSkill)
        assert skill.name == "docs_sync"
        assert isinstance(skill.description, str) and skill.description.strip()
        assert tuple(skill.tools) == ("docs_sync",)
        assert skill.system_prompt is None
        assert 'tools = ["docs_sync"]' in SKILL_SRC.read_text()

    async def test_single_schedule(self, skill: ImperativeSkill) -> None:
        assert len(skill.scheduled_runs) == 1
        spec = skill.scheduled_runs[0]
        assert spec.cron == "*/15 * * * *"
        assert spec.timezone == "America/Chicago"
        assert spec.function == "sync"
        assert spec.name == "sync"
        assert spec.timeout_s == 240
        assert spec.misfire_grace_s == 600
        assert spec.coalesce is True
        assert "sync" in skill.functions


class TestSyncBody:
    async def test_returns_tool_output_dict(self, skill: ImperativeSkill) -> None:
        payload = {
            "scanned": 3,
            "upserted": 2,
            "excluded": 1,
            "removed": 0,
            "reconciliation_skipped": False,
            "cursor": "c",
        }
        tools = _Tools(ToolResult(output=payload, is_error=False))
        ctx = SimpleNamespace(tools=tools)
        out = await skill.functions["sync"](ctx)
        assert out == payload
        assert tools.calls == [("docs_sync", {})]

    async def test_error_result_raises(self, skill: ImperativeSkill) -> None:
        tools = _Tools(ToolResult(output={"error": "paperless unreachable"}, is_error=True))
        ctx = SimpleNamespace(tools=tools)
        with pytest.raises(Exception) as ei:
            await skill.functions["sync"](ctx)
        assert "paperless unreachable" in str(ei.value)
