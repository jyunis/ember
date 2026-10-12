"""DirectorySkillLoader file watcher (FC-5 hot-reload).

Per FC-5: "modifying a skill file replaces the in-memory entry on the
next file-watcher tick. In-flight uses of the previous version finish
on the previous version."

This test exercises that promise with a real on-disk module:
- write a skill file with description "v1"
- `load_all`, `start_watching`
- modify the file to set description "v2"
- assert the registry's entry reflects "v2" within the debounce window
- delete the file
- assert the registry no longer has the skill (either `get` raises
  KeyError, or `remove` was called).

Spec ambiguity flagged in report:
- File deletion handling. The doc says "modifying a skill file
  replaces" but doesn't explicitly say what deletion does. We accept
  either: `get(name)` raises `KeyError`, OR the skill remains until
  the next manual reload. Test admits both.
"""

from __future__ import annotations

import asyncio
from pathlib import Path


def _import_loader():
    from ember.skills.loader import DirectorySkillLoader

    return DirectorySkillLoader


def _import_registry():
    from ember.skills.registry import InMemorySkillRegistry

    return InMemorySkillRegistry


_SKILL_TEMPLATE = """\
name = "demo"
description = {description!r}
tools = []
scheduled_runs = []

async def helper(ctx):
    return None
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


class TestWatcherReload:
    async def test_modification_propagates_to_registry(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        skill_file = skills_dir / "demo.py"
        skill_file.write_text(_SKILL_TEMPLATE.format(description="v1"))

        InMemorySkillRegistry = _import_registry()
        DirectorySkillLoader = _import_loader()
        registry = InMemorySkillRegistry()
        loader = DirectorySkillLoader(skills_dir, registry)

        await loader.load_all()
        assert registry.get("demo").description == "v1"

        await loader.start_watching()
        try:
            skill_file.write_text(_SKILL_TEMPLATE.format(description="v2"))
            updated = await _wait_until(
                lambda: registry.get("demo").description == "v2",
                timeout_s=4.0,
            )
            assert updated, "watcher did not reload modification"
        finally:
            await loader.stop()

    async def test_deletion_removes_or_leaves_consistent(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        skill_file = skills_dir / "demo.py"
        skill_file.write_text(_SKILL_TEMPLATE.format(description="v1"))

        InMemorySkillRegistry = _import_registry()
        DirectorySkillLoader = _import_loader()
        registry = InMemorySkillRegistry()
        loader = DirectorySkillLoader(skills_dir, registry)
        await loader.load_all()
        await loader.start_watching()

        try:
            skill_file.unlink()
            removed = await _wait_until(
                lambda: "demo" not in registry.names(),
                timeout_s=4.0,
            )
            # Either the watcher removed the entry, or it left it in
            # place. Both are defensible; we assert removal here as the
            # natural FC-5 reading. Flip if the spec says otherwise.
            assert removed, "watcher did not remove deleted skill from registry"
        finally:
            await loader.stop()
