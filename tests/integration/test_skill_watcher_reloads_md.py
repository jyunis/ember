"""Skill loader handles `*/SKILL.md` files (FC-14, v8 hot-reload).

Per FC-14: the loader's `_enumerate_skill_files` is extended to yield
`<skills_dir>/<name>/SKILL.md` (one level deep) alongside the existing
`*.py` and `*.toml` enumeration. `_reload_one` dispatches on path:
SKILL.md → `load_markdown`. The watcher infrastructure
(`_signature` + `_poll_once`) needs no change — the existing mtime+size
+hash signature works for any path.

These tests exercise the loader's public surface only:
`DirectorySkillLoader(skills_dir, registry).load_all()`,
`start_watching()`, `stop()`. They mirror the patterns in
`test_skill_watcher_reloads_toml.py` and
`test_loader_handles_mixed_py_toml.py`.

Spec ambiguities flagged in report:
- Multi-file `load_all` failure mode: when one SKILL.md is malformed
  and others are valid, the spec doesn't pin whether `load_all` raises
  or skips-and-warns. The existing TOML/PY loaders raise on the first
  invalid file (per `test_skill_loader.py`'s rejection cases that call
  `await loader.load_all()` and expect `SkillLoadError`). This test
  matches that behavior — flagged in the report.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from ember.skills.base import DeclarativeSkill, ImperativeSkill
from ember.skills.loader import DirectorySkillLoader
from ember.skills.registry import InMemorySkillRegistry


# --- SKILL.md / .py / .toml content templates ----------------------------


_VALID_MD = """\
---
name: {name}
description: {description}
tools:
  - mealie_search
suggest_when:
  - meal planning
---
{body}
"""


_INVALID_MD = """\
---
name: badmd
description: Missing closing frontmatter delimiter
tools:
  - foo
"""


_VALID_TOML = """\
name = "dec_skill"
description = "A declarative TOML skill."
tools = ["foo"]
"""


_VALID_PY = """\
name = "imp_skill"
description = "An imperative Python skill."
tools = []

scheduled_runs = [
    {"cron": "0 5 * * *", "function": "tick", "name": "morning"},
]

async def tick(ctx):
    return None
"""


# --- Helpers --------------------------------------------------------------


def _write_md(
    skills_dir: Path, name: str, *, description: str = "v1", body: str = "v1 body"
) -> Path:
    """Write `<skills_dir>/<name>/SKILL.md` with the standard template."""
    skill_dir = skills_dir / name
    skill_dir.mkdir()
    path = skill_dir / "SKILL.md"
    path.write_text(_VALID_MD.format(name=name, description=description, body=body))
    return path


async def _wait_until(predicate, *, timeout_s: float = 4.0, poll_s: float = 0.05) -> bool:
    """Poll `predicate()` until truthy or timeout. Returns False on timeout."""
    elapsed = 0.0
    while elapsed < timeout_s:
        try:
            if predicate():
                return True
        except Exception:
            pass
        await asyncio.sleep(poll_s)
        elapsed += poll_s
    return False


# --- load_all ------------------------------------------------------------


class TestLoadAllPicksUpSkillMd:
    async def test_skill_md_registered_as_declarative(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        _write_md(skills_dir, "mddemo", description="MD demo.", body="The MD body.")

        registry = InMemorySkillRegistry()
        loader = DirectorySkillLoader(skills_dir, registry)
        await loader.load_all()

        assert "mddemo" in registry.names()
        skill = registry.get("mddemo")
        assert isinstance(skill, DeclarativeSkill)
        assert skill.name == "mddemo"
        assert skill.description == "MD demo."
        assert skill.tools == ("mealie_search",)
        assert skill.suggest_when == ("meal planning",)
        assert skill.system_prompt == "The MD body."
        # source_path is the SKILL.md path itself, not the parent dir.
        assert skill.source_path is not None
        assert skill.source_path.endswith("/mddemo/SKILL.md")

    async def test_mixed_py_toml_md_directory(self, tmp_path: Path) -> None:
        # All three skill formats coexisting in the same skills dir.
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        _write_md(skills_dir, "mddemo", description="MD demo.", body="The MD body.")
        (skills_dir / "dec_skill.toml").write_text(_VALID_TOML)
        (skills_dir / "imp_skill.py").write_text(_VALID_PY)

        registry = InMemorySkillRegistry()
        loader = DirectorySkillLoader(skills_dir, registry)
        await loader.load_all()

        names = set(registry.names())
        assert names == {"mddemo", "dec_skill", "imp_skill"}

        assert isinstance(registry.get("mddemo"), DeclarativeSkill)
        assert isinstance(registry.get("dec_skill"), DeclarativeSkill)
        assert isinstance(registry.get("imp_skill"), ImperativeSkill)

    async def test_single_file_md_at_top_level_is_ignored(self, tmp_path: Path) -> None:
        # Per FC-14: only `<skills_dir>/<name>/SKILL.md` is supported.
        # A bare `<skills_dir>/foo.md` should NOT be picked up.
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        # Top-level foo.md — should be ignored by the loader.
        (skills_dir / "foo.md").write_text("---\nname: foo\ndescription: bare md\n---\nbody\n")
        # Properly-located bar/SKILL.md — should be picked up.
        _write_md(skills_dir, "bar", description="Bar skill.", body="Bar body.")

        registry = InMemorySkillRegistry()
        loader = DirectorySkillLoader(skills_dir, registry)
        await loader.load_all()

        assert "bar" in registry.names()
        assert "foo" not in registry.names()


class TestLoadAllInvalidMd:
    async def test_invalid_md_raises_skill_load_error(self, tmp_path: Path) -> None:
        # Mirror the existing PY/TOML behavior: an invalid manifest
        # causes load_all() to raise SkillLoadError.
        from ember.errors import SkillLoadError

        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        skill_dir = skills_dir / "badmd"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(_INVALID_MD)

        registry = InMemorySkillRegistry()
        loader = DirectorySkillLoader(skills_dir, registry)

        import pytest

        with pytest.raises(SkillLoadError):
            await loader.load_all()


# --- start_watching: hot-reload (FC-5 + FC-14) ---------------------------


class TestMdWatcher:
    async def test_modification_propagates_to_registry(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        skill_md = _write_md(skills_dir, "mddemo", description="v1 description", body="v1 body")

        registry = InMemorySkillRegistry()
        loader = DirectorySkillLoader(skills_dir, registry)
        await loader.load_all()
        assert registry.get("mddemo").system_prompt == "v1 body"

        await loader.start_watching()
        try:
            skill_md.write_text(
                _VALID_MD.format(name="mddemo", description="v2 description", body="v2 body")
            )
            updated = await _wait_until(
                lambda: registry.get("mddemo").system_prompt == "v2 body",
                timeout_s=4.0,
            )
            assert updated, "watcher did not pick up SKILL.md body change"
            # Description (frontmatter field) also reflects the new value.
            assert registry.get("mddemo").description == "v2 description"
        finally:
            await loader.stop()

    async def test_deletion_of_skill_md_removes_skill_from_registry(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        skill_md = _write_md(skills_dir, "mddemo", description="v1", body="v1 body")

        registry = InMemorySkillRegistry()
        loader = DirectorySkillLoader(skills_dir, registry)
        await loader.load_all()
        assert "mddemo" in registry.names()

        await loader.start_watching()
        try:
            skill_md.unlink()
            removed = await _wait_until(
                lambda: "mddemo" not in registry.names(),
                timeout_s=4.0,
            )
            assert removed, "watcher did not remove deleted SKILL.md"
        finally:
            await loader.stop()

    async def test_deletion_of_parent_directory_removes_skill_from_registry(
        self, tmp_path: Path
    ) -> None:
        # Per FC-14: deleting the parent dir removes the skill as a
        # side effect of the SKILL.md disappearing.
        import shutil

        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        _write_md(skills_dir, "mddemo", description="v1", body="v1 body")

        registry = InMemorySkillRegistry()
        loader = DirectorySkillLoader(skills_dir, registry)
        await loader.load_all()
        assert "mddemo" in registry.names()

        await loader.start_watching()
        try:
            shutil.rmtree(skills_dir / "mddemo")
            removed = await _wait_until(
                lambda: "mddemo" not in registry.names(),
                timeout_s=4.0,
            )
            assert removed, "watcher did not remove skill after parent dir deletion"
        finally:
            await loader.stop()
