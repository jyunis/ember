"""DirectorySkillLoader (FC-5, TLC-13).

The implementer ships `ember.skills.loader.DirectorySkillLoader` which:
- Walks a directory of `.py` modules, importlib-loads each, validates
  the FC-5 contract, and registers the resulting `ImperativeSkill` in
  a `SkillRegistry`.
- Refuses sync functions in `scheduled_runs` slots (FC-5).
- Refuses references to nonexistent functions (FC-5).
- Refuses "declarative-style" modules carrying `scheduled_runs` but
  no coroutines (TLC-13: only imperative skills can be scheduled —
  detected here as "scheduled_runs non-empty AND no async fn"). The
  spec doesn't pin the *exact* trigger mechanism for TLC-13 detection,
  but the unresolved-function path and this no-coroutines path both
  surface as `SkillLoadError`.
- Re-raises import errors as `SkillLoadError` (with the original
  chained).
- Refuses modules without `name`.
- Hot-reloads on file change via `start_watching` and updates the
  registry.

Tests use `tmp_path` to write skill modules. The loader takes
`(directory, registry)`.

Spec ambiguity flagged in report:
- Whether the loader's constructor is `(directory, registry)` or
  `(registry, directory)` — assumed `(directory, registry)` per the
  natural reading of the boot-time API. If the implementer chose the
  opposite order, this becomes a one-line fix.
- File-watcher debounce window — pyproject sets
  `skills_watch_debounce_ms=500`. We poll up to ~3s for a reload to
  appear; if the implementer uses a longer debounce we'll surface that.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ember.errors import SkillLoadError
from ember.skills.base import ImperativeSkill


def _import_loader():
    from ember.skills.loader import DirectorySkillLoader

    return DirectorySkillLoader


def _import_registry():
    from ember.skills.registry import InMemorySkillRegistry

    return InMemorySkillRegistry


# --- Skill module text helpers -------------------------------------------


_VALID_SKILL = """\
name = "demo"
description = "Demo skill."
tools = []

scheduled_runs = [
    {"cron": "0 5 * * *", "function": "send_brief", "name": "morning_brief", "timeout_s": 60},
]

async def send_brief(ctx):
    return {"ok": True}
"""


_NO_NAME_SKILL = """\
description = "Missing name."

async def f(ctx):
    return None
"""


_SYNC_FN_SKILL = """\
name = "syncbad"
description = "Has a sync function in scheduled_runs slot."

scheduled_runs = [
    {"cron": "0 5 * * *", "function": "tick", "name": "tick"},
]

def tick(ctx):  # SYNC — invalid
    return None
"""


_UNKNOWN_FN_SKILL = """\
name = "unknownfn"
description = "References a function that does not exist."

scheduled_runs = [
    {"cron": "0 5 * * *", "function": "missing", "name": "tick"},
]
"""


_DECLARATIVE_LOOKING_SKILL = """\
name = "declarative_with_schedule"
description = "Looks declarative — has scheduled_runs but no coroutines."
tools = ["foo"]

scheduled_runs = [
    {"cron": "0 5 * * *", "function": "tick", "name": "tick"},
]

# No async def at all → TLC-13 violation. (And `function` won't resolve.)
"""


_IMPORT_ERROR_SKILL = """\
name = "importbad"
description = "Imports a missing module."

import this_module_does_not_exist_xyz  # noqa: E402

async def f(ctx):
    return None
"""


_VALID_NO_SCHEDULES_SKILL = """\
name = "tools_only"
description = "A skill with no scheduled_runs."
tools = ["foo", "bar"]

async def helper(ctx):
    return "ok"
"""


# --- Fixtures -------------------------------------------------------------


@pytest.fixture
def skills_dir(tmp_path: Path) -> Path:
    d = tmp_path / "skills"
    d.mkdir()
    return d


# --- load_all: valid skill -----------------------------------------------


class TestLoadAllValidSkill:
    async def test_loads_valid_skill_into_registry(self, skills_dir: Path) -> None:
        (skills_dir / "demo.py").write_text(_VALID_SKILL)

        InMemorySkillRegistry = _import_registry()
        DirectorySkillLoader = _import_loader()
        registry = InMemorySkillRegistry()
        loader = DirectorySkillLoader(skills_dir, registry)
        await loader.load_all()

        skill = registry.get("demo")
        assert isinstance(skill, ImperativeSkill)
        assert skill.name == "demo"
        assert skill.description == "Demo skill."
        # `tools` is normalized to a tuple per `ImperativeSkill`.
        assert skill.tools == ()
        # `scheduled_runs` becomes tuple[ScheduleSpec, ...].
        assert len(skill.scheduled_runs) == 1
        spec = skill.scheduled_runs[0]
        assert spec.cron == "0 5 * * *"
        assert spec.function == "send_brief"
        assert spec.name == "morning_brief"
        assert spec.timeout_s == 60
        # `functions` mapping has the bound coroutine.
        assert "send_brief" in skill.functions
        fn = skill.functions["send_brief"]
        assert asyncio.iscoroutinefunction(fn)

    async def test_loads_skill_with_no_schedules(self, skills_dir: Path) -> None:
        (skills_dir / "tools_only.py").write_text(_VALID_NO_SCHEDULES_SKILL)

        InMemorySkillRegistry = _import_registry()
        DirectorySkillLoader = _import_loader()
        registry = InMemorySkillRegistry()
        loader = DirectorySkillLoader(skills_dir, registry)
        await loader.load_all()

        skill = registry.get("tools_only")
        assert skill.name == "tools_only"
        assert skill.tools == ("foo", "bar")
        assert skill.scheduled_runs == ()


# --- load_all: rejection cases (FC-5) ------------------------------------


class TestLoadAllRejection:
    async def test_sync_function_in_scheduled_slot_raises(self, skills_dir: Path) -> None:
        (skills_dir / "syncbad.py").write_text(_SYNC_FN_SKILL)

        InMemorySkillRegistry = _import_registry()
        DirectorySkillLoader = _import_loader()
        registry = InMemorySkillRegistry()
        loader = DirectorySkillLoader(skills_dir, registry)

        with pytest.raises(SkillLoadError):
            await loader.load_all()

    async def test_unknown_function_reference_raises(self, skills_dir: Path) -> None:
        (skills_dir / "unknownfn.py").write_text(_UNKNOWN_FN_SKILL)

        InMemorySkillRegistry = _import_registry()
        DirectorySkillLoader = _import_loader()
        registry = InMemorySkillRegistry()
        loader = DirectorySkillLoader(skills_dir, registry)

        with pytest.raises(SkillLoadError):
            await loader.load_all()

    async def test_declarative_style_module_with_schedules_rejected(self, skills_dir: Path) -> None:
        # TLC-13: only imperative skills can be scheduled.
        # The detection path here is: scheduled_runs is non-empty AND
        # no `async def` is present → function names cannot resolve.
        # SkillLoadError is the right outcome.
        (skills_dir / "declarative.py").write_text(_DECLARATIVE_LOOKING_SKILL)

        InMemorySkillRegistry = _import_registry()
        DirectorySkillLoader = _import_loader()
        registry = InMemorySkillRegistry()
        loader = DirectorySkillLoader(skills_dir, registry)

        with pytest.raises(SkillLoadError):
            await loader.load_all()

    async def test_import_error_re_raised_as_skill_load_error(self, skills_dir: Path) -> None:
        (skills_dir / "importbad.py").write_text(_IMPORT_ERROR_SKILL)

        InMemorySkillRegistry = _import_registry()
        DirectorySkillLoader = _import_loader()
        registry = InMemorySkillRegistry()
        loader = DirectorySkillLoader(skills_dir, registry)

        with pytest.raises(SkillLoadError) as ei:
            await loader.load_all()
        # The original exception should be chained for diagnosis.
        assert ei.value.__cause__ is not None or ei.value.__context__ is not None

    async def test_module_without_name_raises(self, skills_dir: Path) -> None:
        (skills_dir / "no_name.py").write_text(_NO_NAME_SKILL)

        InMemorySkillRegistry = _import_registry()
        DirectorySkillLoader = _import_loader()
        registry = InMemorySkillRegistry()
        loader = DirectorySkillLoader(skills_dir, registry)

        with pytest.raises(SkillLoadError):
            await loader.load_all()


# --- start_watching: hot-reload (FC-5) -----------------------------------


async def _wait_until(predicate, *, timeout_s: float = 3.0, poll_s: float = 0.05) -> bool:
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


class TestStartWatching:
    async def test_modifying_file_replaces_registry_entry(self, skills_dir: Path) -> None:
        # Initial version has description "Demo skill." — modify and
        # confirm the registry shows the new value within the watcher's
        # debounce window.
        skill_file = skills_dir / "demo.py"
        skill_file.write_text(_VALID_SKILL)

        InMemorySkillRegistry = _import_registry()
        DirectorySkillLoader = _import_loader()
        registry = InMemorySkillRegistry()
        loader = DirectorySkillLoader(skills_dir, registry)
        await loader.load_all()
        assert registry.get("demo").description == "Demo skill."

        await loader.start_watching()
        try:
            new_text = _VALID_SKILL.replace("Demo skill.", "Demo skill v2.")
            skill_file.write_text(new_text)

            updated = await _wait_until(
                lambda: registry.get("demo").description == "Demo skill v2.",
                timeout_s=4.0,
            )
            assert updated, "watcher did not pick up the description change"
        finally:
            await loader.stop()
