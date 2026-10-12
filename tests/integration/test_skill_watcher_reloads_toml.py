"""Skill watcher reloads `.toml` declarative manifests (FC-5 hot-reload).

Per FC-5: "modifying a skill file replaces the in-memory entry on the
next file-watcher tick." v7 extends this to `.toml` manifests:
- Modifying a `.toml` triggers `replace`.
- Deleting a `.toml` triggers `remove`.

This mirrors the existing `test_skill_watcher_reload.py` (for `.py`)
but for declarative skills.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from ember.skills.loader import DirectorySkillLoader
from ember.skills.registry import InMemorySkillRegistry


_DECLARATIVE_TEMPLATE = """\
name = "demo_decl"
description = {description!r}
tools = []
"""


async def _wait_until(predicate, *, timeout_s: float = 4.0, poll_s: float = 0.05) -> bool:
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


class TestTomlWatcher:
    async def test_modification_propagates_to_registry(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        manifest = skills_dir / "demo_decl.toml"
        manifest.write_text(_DECLARATIVE_TEMPLATE.format(description="v1"))

        registry = InMemorySkillRegistry()
        loader = DirectorySkillLoader(skills_dir, registry)
        await loader.load_all()
        assert registry.get("demo_decl").description == "v1"

        await loader.start_watching()
        try:
            manifest.write_text(_DECLARATIVE_TEMPLATE.format(description="v2"))
            updated = await _wait_until(
                lambda: registry.get("demo_decl").description == "v2",
                timeout_s=4.0,
            )
            assert updated, "watcher did not reload TOML modification"
        finally:
            await loader.stop()

    async def test_deletion_removes_skill_from_registry(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        manifest = skills_dir / "demo_decl.toml"
        manifest.write_text(_DECLARATIVE_TEMPLATE.format(description="v1"))

        registry = InMemorySkillRegistry()
        loader = DirectorySkillLoader(skills_dir, registry)
        await loader.load_all()
        await loader.start_watching()

        try:
            manifest.unlink()
            removed = await _wait_until(
                lambda: "demo_decl" not in registry.names(),
                timeout_s=4.0,
            )
            assert removed, "watcher did not remove deleted TOML manifest"
        finally:
            await loader.stop()
