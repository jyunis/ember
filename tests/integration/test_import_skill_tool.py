"""ImportSkillTool — BC-7 (gated, atomic-ish skill installation).

Contract surface tested:
- `requires_confirmation` is True (BC-7: the install hits TLC-3).
- Valid `.py` skill in `data_dir/uploads/...` lands in `skills_dir`,
  appears in the registry, and the tool returns
  `{ok: True, name: <name>, message: <str>}`.
- Malformed skill → `ok: False` AND no file remains in `skills_dir`
  (rollback per BC-7).
- Source path outside `data_dir/uploads/` is rejected without copying
  anything.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ember.skills.loader import DirectorySkillLoader
from ember.skills.registry import InMemorySkillRegistry
from ember.tools.import_skill import ImportSkillTool


_VALID_SKILL_SRC = """\
name = "uploaded_demo"
description = "Uploaded demo skill."
tools = []
"""

_BROKEN_SKILL_SRC = """\
# Missing required `name` and `description` attributes — should fail
# FC-5 validation when the loader inspects it.
tools = []
"""


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Returns (data_dir, uploads_dir, skills_dir).

    Mirrors the runtime layout: `data_dir/uploads/<conv>/<msg>/<file>`
    is where Discord drops user uploads; `skills_dir` is the install
    target the loader watches.
    """
    data_dir = tmp_path / "data"
    uploads_dir = data_dir / "uploads" / "thread-1" / "m1"
    skills_dir = tmp_path / "skills"
    uploads_dir.mkdir(parents=True)
    skills_dir.mkdir(parents=True)
    return data_dir, uploads_dir, skills_dir


@pytest.fixture
def tool_and_registry(dirs):
    data_dir, _, skills_dir = dirs
    registry = InMemorySkillRegistry()
    loader = DirectorySkillLoader(skills_dir, registry)
    tool = ImportSkillTool(loader=loader, data_dir=data_dir, skills_dir=skills_dir)
    return tool, registry, skills_dir


# --- requires_confirmation -------------------------------------------------


class TestImportSkillRequiresConfirmation:
    def test_requires_confirmation_is_true(self, tool_and_registry) -> None:
        tool, _, _ = tool_and_registry
        # BC-7: TLC-3 prompt fires before install.
        assert tool.requires_confirmation is True


# --- Happy path ------------------------------------------------------------


class TestImportSkillHappyPath:
    async def test_valid_py_skill_installs_and_registers(self, dirs, tool_and_registry) -> None:
        _, uploads_dir, skills_dir = dirs
        tool, registry, _ = tool_and_registry

        src = uploads_dir / "uploaded_demo.py"
        src.write_text(_VALID_SKILL_SRC)

        result = await tool.call({"path": str(src)})
        out = result.output
        assert out["ok"] is True, out
        # Return shape: {ok, name, message} — `name` must be the registered
        # skill's name; `message` must be a string.
        assert out["name"] == "uploaded_demo"
        assert isinstance(out["message"], str) and out["message"], out

        # File landed in skills_dir.
        assert (skills_dir / "uploaded_demo.py").is_file()

        # Registry sees it.
        assert "uploaded_demo" in registry.names()

    async def test_return_shape_has_expected_keys(self, dirs, tool_and_registry) -> None:
        _, uploads_dir, _ = dirs
        tool, _, _ = tool_and_registry

        src = uploads_dir / "uploaded_demo.py"
        src.write_text(_VALID_SKILL_SRC)

        result = await tool.call({"path": str(src)})
        # Spec says return shape is `{ok: bool, name: str, message: str}`.
        assert set(result.output.keys()) >= {"ok", "name", "message"}


# --- Malformed skill rollback ---------------------------------------------


class TestImportSkillRollback:
    async def test_malformed_skill_returns_failure(self, dirs, tool_and_registry) -> None:
        _, uploads_dir, _ = dirs
        tool, _, _ = tool_and_registry

        src = uploads_dir / "broken.py"
        src.write_text(_BROKEN_SKILL_SRC)

        result = await tool.call({"path": str(src)})
        assert result.output["ok"] is False, result.output

    async def test_malformed_skill_leaves_no_file_in_skills_dir(
        self, dirs, tool_and_registry
    ) -> None:
        _, uploads_dir, skills_dir = dirs
        tool, registry, _ = tool_and_registry

        src = uploads_dir / "broken.py"
        src.write_text(_BROKEN_SKILL_SRC)

        await tool.call({"path": str(src)})

        # BC-7 atomicity: on validation failure, no file is left in
        # skills_dir.
        assert not (skills_dir / "broken.py").exists()
        # Registry unchanged.
        assert registry.names() == []


# --- Path-outside-uploads rejection ---------------------------------------


class TestImportSkillRejectsOutsideUploads:
    async def test_path_outside_uploads_dir_is_rejected(
        self, tmp_path: Path, dirs, tool_and_registry
    ) -> None:
        # Drop the source somewhere completely outside data_dir/uploads/.
        _, _, skills_dir = dirs
        tool, registry, _ = tool_and_registry

        outside = tmp_path / "elsewhere"
        outside.mkdir()
        src = outside / "demo.py"
        src.write_text(_VALID_SKILL_SRC)

        result = await tool.call({"path": str(src)})
        assert result.output["ok"] is False, result.output

        # Nothing should have been copied into skills_dir.
        assert list(skills_dir.iterdir()) == []
        # Registry unchanged.
        assert registry.names() == []

    async def test_path_in_data_dir_but_not_under_uploads_is_rejected(
        self, dirs, tool_and_registry
    ) -> None:
        # Even within data_dir — only `data_dir/uploads/...` is allowed.
        data_dir, _, skills_dir = dirs
        tool, registry, _ = tool_and_registry

        sneak = data_dir / "sneak.py"
        sneak.write_text(_VALID_SKILL_SRC)

        result = await tool.call({"path": str(sneak)})
        assert result.output["ok"] is False, result.output
        assert list(skills_dir.iterdir()) == []
        assert registry.names() == []
