"""Declarative skill manifest loader (FC-5, TLC-13, v7).

The implementer ships `ember.skills.manifest.load_declarative(path)`,
which parses a TOML file at `<skills_dir>/<name>.toml` into a
`DeclarativeSkill` instance.

Validation rules pinned by the manifest stub's docstring + the brief:
- `name` (required, non-empty, must match filename stem).
- `description` (required).
- `tools` (optional `list[str]`).
- `system_prompt` (optional `str`).
- `preferred_model` (optional `str` shaped `<provider>:<model>`).
- `suggest_when` (optional `list[str]`).
- `scheduled_runs` is **rejected** (TLC-13).
- Any unknown top-level key is rejected.
- Malformed TOML is rejected.

Every rejection raises `SkillLoadError`. Every happy path produces a
frozen `DeclarativeSkill` whose `tools`/`suggest_when` are tuples (the
dataclass declares them as `tuple[str, ...]`) and whose `source_path`
is `str(path)`.

Spec ambiguities flagged in report:
- None for this file. The stub's docstring is precise.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ember.errors import SkillLoadError
from ember.skills.base import DeclarativeSkill


def _import_load_declarative():
    from ember.skills.manifest import load_declarative

    return load_declarative


# --- Manifest text helpers -----------------------------------------------


_VALID_FULL = """\
name = "demo"
description = "A demo declarative skill."
tools = ["mealie_search", "mealie_random"]
system_prompt = "You are the demo skill."
preferred_model = "claude_cli:sonnet-4-6"
suggest_when = ["meal planning", "what should we eat"]
"""


_VALID_MINIMAL = """\
name = "demo"
description = "Just the basics."
"""


_NO_NAME = """\
description = "Missing name."
"""


_NO_DESCRIPTION = """\
name = "demo"
"""


_EMPTY_NAME = """\
name = ""
description = "Empty name."
"""


_NAME_MISMATCH = """\
name = "different"
description = "Filename stem will not match."
"""


_HAS_SCHEDULED_RUNS = """\
name = "demo"
description = "Declarative manifest carrying scheduled_runs (TLC-13 violation)."
scheduled_runs = [
    {cron = "0 5 * * *", function = "tick", name = "morning"},
]
"""


_UNKNOWN_KEY = """\
name = "demo"
description = "Has an unknown top-level key."
totally_unknown_field = "nope"
"""


_TOOLS_NOT_LIST = """\
name = "demo"
description = "tools should be a list."
tools = "mealie_search"
"""


_SYSTEM_PROMPT_NOT_STRING = """\
name = "demo"
description = "system_prompt should be a string."
system_prompt = 42
"""


_MALFORMED_TOML = """\
name = "demo
description = "missing closing quote on name"
"""


# --- Fixtures -------------------------------------------------------------


@pytest.fixture
def skills_dir(tmp_path: Path) -> Path:
    d = tmp_path / "skills"
    d.mkdir()
    return d


# --- Happy paths ---------------------------------------------------------


class TestHappyPath:
    def test_full_manifest_round_trip(self, skills_dir: Path) -> None:
        path = skills_dir / "demo.toml"
        path.write_text(_VALID_FULL)
        load_declarative = _import_load_declarative()

        skill = load_declarative(path)
        assert isinstance(skill, DeclarativeSkill)
        assert skill.name == "demo"
        assert skill.description == "A demo declarative skill."
        # tuple per the dataclass definition.
        assert skill.tools == ("mealie_search", "mealie_random")
        assert skill.system_prompt == "You are the demo skill."
        assert skill.preferred_model == "claude_cli:sonnet-4-6"
        assert skill.suggest_when == ("meal planning", "what should we eat")
        assert skill.source_path == str(path)

    def test_minimal_manifest(self, skills_dir: Path) -> None:
        path = skills_dir / "demo.toml"
        path.write_text(_VALID_MINIMAL)
        load_declarative = _import_load_declarative()

        skill = load_declarative(path)
        assert skill.name == "demo"
        assert skill.description == "Just the basics."
        # Optional fields default to empty/None.
        assert skill.tools == ()
        assert skill.system_prompt is None
        assert skill.preferred_model is None
        assert skill.suggest_when == ()
        assert skill.source_path == str(path)


# --- Rejections ----------------------------------------------------------


class TestRejections:
    def test_missing_name_raises(self, skills_dir: Path) -> None:
        path = skills_dir / "demo.toml"
        path.write_text(_NO_NAME)
        load_declarative = _import_load_declarative()

        with pytest.raises(SkillLoadError):
            load_declarative(path)

    def test_missing_description_raises(self, skills_dir: Path) -> None:
        path = skills_dir / "demo.toml"
        path.write_text(_NO_DESCRIPTION)
        load_declarative = _import_load_declarative()

        with pytest.raises(SkillLoadError):
            load_declarative(path)

    def test_empty_name_raises(self, skills_dir: Path) -> None:
        path = skills_dir / "demo.toml"
        path.write_text(_EMPTY_NAME)
        load_declarative = _import_load_declarative()

        with pytest.raises(SkillLoadError):
            load_declarative(path)

    def test_name_does_not_match_filename_stem_raises(self, skills_dir: Path) -> None:
        path = skills_dir / "demo.toml"
        path.write_text(_NAME_MISMATCH)
        load_declarative = _import_load_declarative()

        with pytest.raises(SkillLoadError):
            load_declarative(path)

    def test_scheduled_runs_present_raises_tlc13(self, skills_dir: Path) -> None:
        # TLC-13: declarative manifests cannot be scheduled.
        path = skills_dir / "demo.toml"
        path.write_text(_HAS_SCHEDULED_RUNS)
        load_declarative = _import_load_declarative()

        with pytest.raises(SkillLoadError):
            load_declarative(path)

    def test_unknown_top_level_key_raises(self, skills_dir: Path) -> None:
        path = skills_dir / "demo.toml"
        path.write_text(_UNKNOWN_KEY)
        load_declarative = _import_load_declarative()

        with pytest.raises(SkillLoadError):
            load_declarative(path)

    def test_tools_not_a_list_raises(self, skills_dir: Path) -> None:
        path = skills_dir / "demo.toml"
        path.write_text(_TOOLS_NOT_LIST)
        load_declarative = _import_load_declarative()

        with pytest.raises(SkillLoadError):
            load_declarative(path)

    def test_system_prompt_not_string_raises(self, skills_dir: Path) -> None:
        path = skills_dir / "demo.toml"
        path.write_text(_SYSTEM_PROMPT_NOT_STRING)
        load_declarative = _import_load_declarative()

        with pytest.raises(SkillLoadError):
            load_declarative(path)

    def test_malformed_toml_raises(self, skills_dir: Path) -> None:
        path = skills_dir / "demo.toml"
        path.write_text(_MALFORMED_TOML)
        load_declarative = _import_load_declarative()

        with pytest.raises(SkillLoadError):
            load_declarative(path)
