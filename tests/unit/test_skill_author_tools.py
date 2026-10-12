"""skill_author tools (FC-3, FC-5, UoW-10/11, v7).

Three tools ship under `ember.tools.skill_author`:

1. `SkillSaveManifestTool` (`name="skill_save_manifest"`,
   `requires_confirmation=False`).
   - Validates `name` matches `^[a-z][a-z0-9_]*$`.
   - Rejects path-traversal attempts (`../`, absolute paths, names that
     would escape `<skills_dir>`).
   - Validates every entry in `tools` resolves in the live tool
     registry.
   - Writes `<skills_dir>/<name>.toml` atomically (write to a temp
     file, then rename).
   - Returns `{"path": str, "name": str}`.

2. `SkillDraftPythonTool` (`name="skill_draft_python"`,
   `requires_confirmation=False`).
   - Validates `name` matches `^[a-z][a-z0-9_]*$`.
   - Writes `<skills_dir>/_drafts/<name>.py` atomically (creates
     `_drafts/` if missing).
   - Returns `{"path": str, "name": str}`.

3. `SkillListKnownToolsTool` (`name="skill_list_known_tools"`,
   `requires_confirmation=False`).
   - Input schema is `{}` (no args).
   - Returns `{"tools": list[{"name": str, "description": str}]}`,
     sorted by name.

All three tools take `Registry` and `skills_dir: Path` in their
constructor. None ever write outside `skills_dir`.

Spec ambiguity flagged in report:
1. Whether `SkillSaveManifestTool` overwrites an existing manifest or
   refuses. Brief leans **overwrite** to keep the agent flow ergonomic.
   The "overwrite" test asserts overwrite; if the implementer chose to
   refuse, that test will fail with a load-bearing message.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

from ember.errors import ToolError
from ember.tools.base import Tool, ToolResult
from ember.tools.registry import Registry


def _import_save_tool():
    from ember.tools.skill_author import SkillSaveManifestTool

    return SkillSaveManifestTool


def _import_draft_tool():
    from ember.tools.skill_author import SkillDraftPythonTool

    return SkillDraftPythonTool


def _import_list_tool():
    from ember.tools.skill_author import SkillListKnownToolsTool

    return SkillListKnownToolsTool


# --- Fakes ---------------------------------------------------------------


class _StubTool(Tool):
    """A trivial Tool to register in a real `Registry` so `SkillSaveManifestTool`
    has something to validate `tools` references against."""

    def __init__(self, name: str, description: str = "stub tool") -> None:
        self.name = name
        self.description = description
        self.input_schema: dict[str, Any] = {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }
        self.requires_confirmation: bool = False

    async def call(self, args: dict[str, Any]) -> ToolResult:  # pragma: no cover
        return ToolResult(output={})


def _make_registry(*tool_names: str) -> Registry:
    reg = Registry()
    for name in tool_names:
        reg.register(_StubTool(name))
    return reg


# --- Fixtures -------------------------------------------------------------


@pytest.fixture
def skills_dir(tmp_path: Path) -> Path:
    d = tmp_path / "skills"
    d.mkdir()
    return d


# ===== SkillSaveManifestTool =============================================


class TestSaveManifestSurface:
    def test_name(self, skills_dir: Path) -> None:
        SkillSaveManifestTool = _import_save_tool()
        tool = SkillSaveManifestTool(registry=Registry(), skills_dir=skills_dir)
        assert tool.name == "skill_save_manifest"

    def test_does_not_require_confirmation(self, skills_dir: Path) -> None:
        SkillSaveManifestTool = _import_save_tool()
        tool = SkillSaveManifestTool(registry=Registry(), skills_dir=skills_dir)
        # TLC-3: writing skill files is reversible (UoW-10) — not destructive.
        assert tool.requires_confirmation is False

    def test_input_schema_shape(self, skills_dir: Path) -> None:
        SkillSaveManifestTool = _import_save_tool()
        tool = SkillSaveManifestTool(registry=Registry(), skills_dir=skills_dir)
        schema = tool.input_schema
        assert schema["type"] == "object"
        props = schema["properties"]
        # Required keys.
        assert "name" in props
        assert "description" in props
        # Optional keys present in shape.
        assert "tools" in props
        assert "system_prompt" in props
        assert "preferred_model" in props
        assert "suggest_when" in props
        # Required list pins name + description.
        required = set(schema.get("required", []))
        assert "name" in required
        assert "description" in required

    def test_description_nonempty(self, skills_dir: Path) -> None:
        SkillSaveManifestTool = _import_save_tool()
        tool = SkillSaveManifestTool(registry=Registry(), skills_dir=skills_dir)
        assert isinstance(tool.description, str)
        assert tool.description.strip() != ""


class TestSaveManifestHappyPath:
    async def test_writes_well_formed_toml(self, skills_dir: Path) -> None:
        SkillSaveManifestTool = _import_save_tool()
        registry = _make_registry("mealie_search", "mealie_random")
        tool = SkillSaveManifestTool(registry=registry, skills_dir=skills_dir)

        result = await tool.call(
            {
                "name": "demo",
                "description": "A demo declarative skill.",
                "tools": ["mealie_search", "mealie_random"],
                "system_prompt": "You help with meals.",
                "preferred_model": "claude_cli:sonnet-4-6",
                "suggest_when": ["meal planning"],
            }
        )
        assert isinstance(result, ToolResult)
        assert result.is_error is False
        out = result.output
        assert isinstance(out, dict)
        assert out["name"] == "demo"
        path = Path(out["path"])
        assert path == skills_dir / "demo.toml"
        assert path.exists()

        # Round-trip the TOML.
        data = tomllib.loads(path.read_text())
        assert data["name"] == "demo"
        assert data["description"] == "A demo declarative skill."
        assert data["tools"] == ["mealie_search", "mealie_random"]
        assert data["system_prompt"] == "You help with meals."
        assert data["preferred_model"] == "claude_cli:sonnet-4-6"
        assert data["suggest_when"] == ["meal planning"]

    async def test_minimal_manifest_writes(self, skills_dir: Path) -> None:
        SkillSaveManifestTool = _import_save_tool()
        registry = _make_registry()
        tool = SkillSaveManifestTool(registry=registry, skills_dir=skills_dir)

        result = await tool.call({"name": "demo", "description": "Just basics."})
        assert result.is_error is False
        path = Path(result.output["path"])
        assert path.exists()
        data = tomllib.loads(path.read_text())
        assert data["name"] == "demo"
        assert data["description"] == "Just basics."

    async def test_overwrite_existing_manifest(self, skills_dir: Path) -> None:
        # Spec ambiguity flagged: brief leans overwrite for ergonomics.
        SkillSaveManifestTool = _import_save_tool()
        registry = _make_registry()
        tool = SkillSaveManifestTool(registry=registry, skills_dir=skills_dir)

        path = skills_dir / "demo.toml"
        path.write_text('name = "demo"\ndescription = "v1"\n')

        result = await tool.call({"name": "demo", "description": "v2"})
        assert result.is_error is False
        data = tomllib.loads(path.read_text())
        assert data["description"] == "v2"


class TestSaveManifestRejection:
    async def test_invalid_name_uppercase(self, skills_dir: Path) -> None:
        SkillSaveManifestTool = _import_save_tool()
        tool = SkillSaveManifestTool(registry=Registry(), skills_dir=skills_dir)
        # Two acceptable behaviors: raise ToolError, or return a
        # ToolResult with `is_error=True`. Accept either.
        try:
            result = await tool.call({"name": "DemoSkill", "description": "x"})
        except ToolError:
            return
        assert result.is_error is True
        # Nothing written.
        assert not (skills_dir / "DemoSkill.toml").exists()

    async def test_invalid_name_starts_with_digit(self, skills_dir: Path) -> None:
        SkillSaveManifestTool = _import_save_tool()
        tool = SkillSaveManifestTool(registry=Registry(), skills_dir=skills_dir)
        try:
            result = await tool.call({"name": "1bad", "description": "x"})
        except ToolError:
            return
        assert result.is_error is True
        assert not (skills_dir / "1bad.toml").exists()

    async def test_invalid_name_contains_dash(self, skills_dir: Path) -> None:
        SkillSaveManifestTool = _import_save_tool()
        tool = SkillSaveManifestTool(registry=Registry(), skills_dir=skills_dir)
        try:
            result = await tool.call({"name": "bad-name", "description": "x"})
        except ToolError:
            return
        assert result.is_error is True

    async def test_path_traversal_rejected_dot_dot(self, skills_dir: Path) -> None:
        SkillSaveManifestTool = _import_save_tool()
        tool = SkillSaveManifestTool(registry=Registry(), skills_dir=skills_dir)
        try:
            result = await tool.call({"name": "../escape", "description": "x"})
        except ToolError:
            return
        assert result.is_error is True
        # Confirm nothing written outside skills_dir.
        outside = skills_dir.parent / "escape.toml"
        assert not outside.exists()

    async def test_path_traversal_rejected_absolute_path(self, skills_dir: Path) -> None:
        SkillSaveManifestTool = _import_save_tool()
        tool = SkillSaveManifestTool(registry=Registry(), skills_dir=skills_dir)
        try:
            result = await tool.call({"name": "/etc/passwd", "description": "x"})
        except ToolError:
            return
        assert result.is_error is True

    async def test_unknown_tool_reference_rejected(self, skills_dir: Path) -> None:
        SkillSaveManifestTool = _import_save_tool()
        registry = _make_registry("only_this_one")
        tool = SkillSaveManifestTool(registry=registry, skills_dir=skills_dir)

        try:
            result = await tool.call(
                {
                    "name": "demo",
                    "description": "x",
                    "tools": ["only_this_one", "ghost_tool"],
                }
            )
        except ToolError:
            return
        assert result.is_error is True
        # No file written.
        assert not (skills_dir / "demo.toml").exists()


class TestSaveManifestAtomicity:
    async def test_failure_during_rename_leaves_no_partial_file(
        self, skills_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Force `Path.replace` (atomic rename) to raise. No demo.toml
        # should appear in skills_dir afterward, and no `.tmp` either.
        SkillSaveManifestTool = _import_save_tool()
        registry = _make_registry()
        tool = SkillSaveManifestTool(registry=registry, skills_dir=skills_dir)

        original_replace = Path.replace

        def _failing_replace(self: Path, target: Any) -> Any:
            raise OSError("simulated rename failure")

        monkeypatch.setattr(Path, "replace", _failing_replace)

        try:
            result = await tool.call({"name": "demo", "description": "x"})
            # Either the impl raised (caught here) or returned an error.
            assert result.is_error is True
        except (OSError, ToolError):
            pass
        finally:
            monkeypatch.setattr(Path, "replace", original_replace)

        # Final file does NOT exist.
        assert not (skills_dir / "demo.toml").exists()

    async def test_failure_during_write_leaves_no_final_file(
        self, skills_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Force write_text to fail mid-stream. The atomic write should
        # leave neither a final nor a stale temp file.
        SkillSaveManifestTool = _import_save_tool()
        registry = _make_registry()
        tool = SkillSaveManifestTool(registry=registry, skills_dir=skills_dir)

        original_write = Path.write_text

        def _failing_write(self: Path, *args: Any, **kwargs: Any) -> int:
            raise OSError("disk full")

        monkeypatch.setattr(Path, "write_text", _failing_write)

        try:
            result = await tool.call({"name": "demo", "description": "x"})
            assert result.is_error is True
        except (OSError, ToolError):
            pass
        finally:
            monkeypatch.setattr(Path, "write_text", original_write)

        assert not (skills_dir / "demo.toml").exists()


# ===== SkillDraftPythonTool =============================================


class TestDraftPythonSurface:
    def test_name(self, skills_dir: Path) -> None:
        SkillDraftPythonTool = _import_draft_tool()
        tool = SkillDraftPythonTool(registry=Registry(), skills_dir=skills_dir)
        assert tool.name == "skill_draft_python"

    def test_does_not_require_confirmation(self, skills_dir: Path) -> None:
        SkillDraftPythonTool = _import_draft_tool()
        tool = SkillDraftPythonTool(registry=Registry(), skills_dir=skills_dir)
        assert tool.requires_confirmation is False

    def test_input_schema_shape(self, skills_dir: Path) -> None:
        SkillDraftPythonTool = _import_draft_tool()
        tool = SkillDraftPythonTool(registry=Registry(), skills_dir=skills_dir)
        schema = tool.input_schema
        assert schema["type"] == "object"
        props = schema["properties"]
        assert "name" in props
        assert "body" in props
        required = set(schema.get("required", []))
        assert "name" in required
        assert "body" in required


class TestDraftPythonHappyPath:
    async def test_writes_to_drafts_subdir(self, skills_dir: Path) -> None:
        SkillDraftPythonTool = _import_draft_tool()
        tool = SkillDraftPythonTool(registry=Registry(), skills_dir=skills_dir)

        body = 'name = "demo"\ndescription = "drafted"\n\nasync def helper(ctx):\n    return None\n'
        result = await tool.call({"name": "demo", "body": body})
        assert isinstance(result, ToolResult)
        assert result.is_error is False
        path = Path(result.output["path"])
        # Lives under _drafts/.
        assert path.parent == skills_dir / "_drafts"
        assert path == skills_dir / "_drafts" / "demo.py"
        assert path.exists()
        assert path.read_text() == body

    async def test_creates_drafts_subdir_if_missing(self, skills_dir: Path) -> None:
        SkillDraftPythonTool = _import_draft_tool()
        tool = SkillDraftPythonTool(registry=Registry(), skills_dir=skills_dir)
        assert not (skills_dir / "_drafts").exists()

        result = await tool.call({"name": "demo", "body": 'name = "demo"\n'})
        assert result.is_error is False
        assert (skills_dir / "_drafts").is_dir()
        assert (skills_dir / "_drafts" / "demo.py").exists()


class TestDraftPythonRejection:
    async def test_invalid_name_uppercase(self, skills_dir: Path) -> None:
        SkillDraftPythonTool = _import_draft_tool()
        tool = SkillDraftPythonTool(registry=Registry(), skills_dir=skills_dir)
        try:
            result = await tool.call({"name": "BadName", "body": ""})
        except ToolError:
            return
        assert result.is_error is True
        assert not (skills_dir / "_drafts" / "BadName.py").exists()

    async def test_invalid_name_dash(self, skills_dir: Path) -> None:
        SkillDraftPythonTool = _import_draft_tool()
        tool = SkillDraftPythonTool(registry=Registry(), skills_dir=skills_dir)
        try:
            result = await tool.call({"name": "bad-name", "body": ""})
        except ToolError:
            return
        assert result.is_error is True

    async def test_path_traversal_dot_dot_rejected(self, skills_dir: Path) -> None:
        SkillDraftPythonTool = _import_draft_tool()
        tool = SkillDraftPythonTool(registry=Registry(), skills_dir=skills_dir)
        try:
            result = await tool.call({"name": "../escape", "body": ""})
        except ToolError:
            return
        assert result.is_error is True
        outside = skills_dir.parent / "escape.py"
        assert not outside.exists()

    async def test_path_traversal_absolute_rejected(self, skills_dir: Path) -> None:
        SkillDraftPythonTool = _import_draft_tool()
        tool = SkillDraftPythonTool(registry=Registry(), skills_dir=skills_dir)
        try:
            result = await tool.call({"name": "/etc/passwd", "body": ""})
        except ToolError:
            return
        assert result.is_error is True


class TestDraftPythonAtomicity:
    async def test_failure_during_rename_leaves_no_partial_file(
        self, skills_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        SkillDraftPythonTool = _import_draft_tool()
        tool = SkillDraftPythonTool(registry=Registry(), skills_dir=skills_dir)

        original_replace = Path.replace

        def _failing_replace(self: Path, target: Any) -> Any:
            raise OSError("simulated rename failure")

        monkeypatch.setattr(Path, "replace", _failing_replace)
        try:
            result = await tool.call({"name": "demo", "body": "x"})
            assert result.is_error is True
        except (OSError, ToolError):
            pass
        finally:
            monkeypatch.setattr(Path, "replace", original_replace)

        assert not (skills_dir / "_drafts" / "demo.py").exists()


# ===== SkillListKnownToolsTool ===========================================


class TestListKnownToolsSurface:
    def test_name(self, skills_dir: Path) -> None:
        SkillListKnownToolsTool = _import_list_tool()
        tool = SkillListKnownToolsTool(registry=Registry(), skills_dir=skills_dir)
        assert tool.name == "skill_list_known_tools"

    def test_does_not_require_confirmation(self, skills_dir: Path) -> None:
        SkillListKnownToolsTool = _import_list_tool()
        tool = SkillListKnownToolsTool(registry=Registry(), skills_dir=skills_dir)
        assert tool.requires_confirmation is False

    def test_input_schema_is_empty(self, skills_dir: Path) -> None:
        SkillListKnownToolsTool = _import_list_tool()
        tool = SkillListKnownToolsTool(registry=Registry(), skills_dir=skills_dir)
        schema = tool.input_schema
        # Spec: {} — accept either {"type": "object", "properties": {}}
        # or a minimal schema as long as it accepts an empty object.
        assert schema.get("type", "object") == "object"
        assert schema.get("properties", {}) == {}


class TestListKnownToolsBehavior:
    async def test_returns_each_registered_tool(self, skills_dir: Path) -> None:
        SkillListKnownToolsTool = _import_list_tool()
        registry = _make_registry("alpha_tool", "beta_tool")
        # Add custom-described tools to assert descriptions surface.
        # Use a fresh registry to control descriptions.
        registry = Registry()
        registry.register(_StubTool("alpha_tool", description="alpha desc"))
        registry.register(_StubTool("beta_tool", description="beta desc"))
        tool = SkillListKnownToolsTool(registry=registry, skills_dir=skills_dir)

        result = await tool.call({})
        assert isinstance(result, ToolResult)
        assert result.is_error is False
        out = result.output
        assert isinstance(out, dict)
        tools_list = out["tools"]
        assert isinstance(tools_list, list)
        names_in_output = [entry["name"] for entry in tools_list]
        assert "alpha_tool" in names_in_output
        assert "beta_tool" in names_in_output
        # Each entry has a description.
        descriptions = {entry["name"]: entry["description"] for entry in tools_list}
        assert descriptions["alpha_tool"] == "alpha desc"
        assert descriptions["beta_tool"] == "beta desc"

    async def test_returns_sorted_by_name(self, skills_dir: Path) -> None:
        SkillListKnownToolsTool = _import_list_tool()
        registry = Registry()
        registry.register(_StubTool("zulu"))
        registry.register(_StubTool("alpha"))
        registry.register(_StubTool("mike"))

        tool = SkillListKnownToolsTool(registry=registry, skills_dir=skills_dir)
        result = await tool.call({})
        names = [entry["name"] for entry in result.output["tools"]]
        assert names == ["alpha", "mike", "zulu"]

    async def test_empty_registry_returns_empty_list(self, skills_dir: Path) -> None:
        SkillListKnownToolsTool = _import_list_tool()
        tool = SkillListKnownToolsTool(registry=Registry(), skills_dir=skills_dir)
        result = await tool.call({})
        assert result.output == {"tools": []}
