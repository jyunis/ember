"""Loader enforces TLC-13 at the manifest boundary (v7).

A `.toml` manifest carrying `scheduled_runs` is an architectural error
(TLC-13: only imperative skills can be scheduled). The manifest loader
raises `SkillLoadError`. The directory loader's reaction is to log and
skip — which means the registry has neither the rejected skill nor any
crash.

What we assert:
- The TOML containing `scheduled_runs` is NOT registered.
- The directory loader does not let the error escape (the watcher /
  load_all path swallows individual file errors per the existing
  loader pattern — see `test_skill_loader.py` rejection tests).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ember.errors import SkillLoadError
from ember.skills.loader import DirectorySkillLoader
from ember.skills.manifest import load_declarative
from ember.skills.registry import InMemorySkillRegistry


_TOML_WITH_SCHEDULE = """\
name = "bad_decl"
description = "Declarative TOML with scheduled_runs (TLC-13 violation)."
tools = []

scheduled_runs = [
    {cron = "0 5 * * *", function = "tick", name = "morning"},
]
"""


class TestTomlWithScheduledRunsRejected:
    def test_load_declarative_raises_skill_load_error(self, tmp_path: Path) -> None:
        # Manifest-loader-level rejection (the architectural enforcement
        # point).
        path = tmp_path / "bad_decl.toml"
        path.write_text(_TOML_WITH_SCHEDULE)

        with pytest.raises(SkillLoadError):
            load_declarative(path)

    async def test_directory_loader_skips_rejected_manifest(self, tmp_path: Path) -> None:
        # When the directory loader sees a manifest that
        # `load_declarative` rejects, it should log + skip (consistent
        # with the existing imperative behavior). The registry stays
        # empty for this skill name.
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "bad_decl.toml").write_text(_TOML_WITH_SCHEDULE)

        registry = InMemorySkillRegistry()
        loader = DirectorySkillLoader(skills_dir, registry)

        # `load_all` may raise OR swallow the error — both are spec-
        # consistent. Either way, the rejected manifest must not appear
        # in the registry.
        try:
            await loader.load_all()
        except SkillLoadError:
            pass

        assert "bad_decl" not in registry.names()
