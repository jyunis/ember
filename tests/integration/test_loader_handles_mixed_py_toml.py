"""Loader handles a directory mixing `.py` and `.toml` skills (FC-5, v7).

A directory with one imperative `.py` file (carrying a `scheduled_runs`
entry) and one declarative `.toml` file. After `load_all()`:

- Both skills are present in the registry.
- `list_schedules()` returns one entry (the imperative one). Declarative
  skills have no `scheduled_runs`, so they're filtered out.

This is the registry-generalization test: the registry now stores
`Skill` (the union) and `list_schedules` filters appropriately via
`getattr(skill, "scheduled_runs", ())`.
"""

from __future__ import annotations

from pathlib import Path

from ember.skills.base import DeclarativeSkill, ImperativeSkill
from ember.skills.loader import DirectorySkillLoader
from ember.skills.registry import InMemorySkillRegistry


_IMPERATIVE_WITH_SCHEDULE = """\
name = "imp_skill"
description = "Imperative with a schedule."
tools = []

scheduled_runs = [
    {"cron": "0 5 * * *", "function": "tick", "name": "morning"},
]

async def tick(ctx):
    return None
"""


_DECLARATIVE = """\
name = "dec_skill"
description = "Declarative."
tools = ["foo"]
"""


class TestMixedPyAndToml:
    async def test_both_skills_present_in_registry(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "imp_skill.py").write_text(_IMPERATIVE_WITH_SCHEDULE)
        (skills_dir / "dec_skill.toml").write_text(_DECLARATIVE)

        registry = InMemorySkillRegistry()
        loader = DirectorySkillLoader(skills_dir, registry)
        await loader.load_all()

        names = registry.names()
        assert set(names) == {"imp_skill", "dec_skill"}

        imp = registry.get("imp_skill")
        dec = registry.get("dec_skill")
        assert isinstance(imp, ImperativeSkill)
        assert isinstance(dec, DeclarativeSkill)

    async def test_list_schedules_only_returns_imperative_schedules(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "imp_skill.py").write_text(_IMPERATIVE_WITH_SCHEDULE)
        (skills_dir / "dec_skill.toml").write_text(_DECLARATIVE)

        registry = InMemorySkillRegistry()
        loader = DirectorySkillLoader(skills_dir, registry)
        await loader.load_all()

        scheds = registry.list_schedules()
        # Exactly one schedule, from the imperative skill.
        assert len(scheds) == 1
        assert scheds[0].skill == "imp_skill"
        assert scheds[0].spec.name == "morning"
