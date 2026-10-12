"""Loader picks up `.toml` declarative skills (FC-5, v7).

The `DirectorySkillLoader` already loads `.py` imperative skills. v7
extends `_enumerate_skill_files()` to include `*.toml` files (skipping
`_*.{py,toml}`) and dispatches by extension:
  - `.py` → existing imperative path
  - `.toml` → `manifest.load_declarative` then `registry.replace`.

This test writes a TOML manifest, runs `load_all()`, and asserts the
registry contains a `DeclarativeSkill` with the right fields.
"""

from __future__ import annotations

from pathlib import Path

from ember.skills.base import DeclarativeSkill
from ember.skills.loader import DirectorySkillLoader
from ember.skills.registry import InMemorySkillRegistry


_VALID_DECLARATIVE = """\
name = "demo_decl"
description = "A declarative demo."
tools = ["mealie_search"]
system_prompt = "You help with meals."
preferred_model = "claude_cli:sonnet-4-6"
suggest_when = ["meal planning"]
"""


class TestDeclarativeSkillLoadedFromDisk:
    async def test_load_all_picks_up_toml_manifest(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "demo_decl.toml").write_text(_VALID_DECLARATIVE)

        registry = InMemorySkillRegistry()
        loader = DirectorySkillLoader(skills_dir, registry)
        await loader.load_all()

        skill = registry.get("demo_decl")
        assert isinstance(skill, DeclarativeSkill)
        assert skill.name == "demo_decl"
        assert skill.description == "A declarative demo."
        assert skill.tools == ("mealie_search",)
        assert skill.system_prompt == "You help with meals."
        assert skill.preferred_model == "claude_cli:sonnet-4-6"
        assert skill.suggest_when == ("meal planning",)
        assert skill.source_path == str(skills_dir / "demo_decl.toml")

    async def test_underscore_prefixed_toml_files_are_skipped(self, tmp_path: Path) -> None:
        # `_*.toml` files are private/hidden — same convention as `_*.py`.
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "_skip.toml").write_text(_VALID_DECLARATIVE)

        registry = InMemorySkillRegistry()
        loader = DirectorySkillLoader(skills_dir, registry)
        await loader.load_all()

        # Should not have loaded anything.
        assert registry.names() == []
