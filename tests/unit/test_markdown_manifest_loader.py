"""SKILL.md declarative skill loader (FC-14, TLC-13, v8).

The implementer ships `ember.skills.markdown.load_markdown(path)`,
which parses a SKILL.md file at `<skills_dir>/<name>/SKILL.md` into
a `DeclarativeSkill` instance. Mirrors `load_declarative` (the TOML
loader) in shape and validation strictness.

Validation rules pinned by the FC-14 spec:
- File must begin with YAML frontmatter (between `---` lines).
- `name` (required, non-empty) must equal `path.parent.name`.
- `description` (required, non-empty).
- `tools` / `allowed-tools` are interchangeable aliases for
  `tuple[str, ...]`. Both present is rejected.
- `preferred_model` (optional `str`).
- `suggest_when` (optional `list[str]`).
- `scheduled_runs` is **rejected** (TLC-13).
- Unknown frontmatter top-level keys are silently ignored (post-v8
  amendment). Real-world SKILL.md files in the broader Agent Skills
  ecosystem commonly carry vendor metadata (`emoji`, `homepage`,
  `metadata`, `tags`, etc.); rejecting them defeated the drop-in
  goal. `system_prompt:` in frontmatter falls under this — it's
  silently ignored, with the body still becoming the actual
  system prompt.
- Markdown body becomes `system_prompt` verbatim, with
  leading/trailing whitespace trimmed; whitespace-only body becomes
  `None`.

Every rejection raises `SkillLoadError`. Every happy path produces a
frozen `DeclarativeSkill` whose `tools`/`suggest_when` are tuples.
`source_path` is `str(path)` — i.e. the SKILL.md path, not the
parent directory.

Spec ambiguities flagged in report:
- The spec says "internal whitespace is preserved" but doesn't pin
  whether trailing newlines on each internal line are preserved
  byte-for-byte. Tests assume "trim outer surrounding whitespace
  only; everything between the first and last non-whitespace
  characters is preserved verbatim, including blank lines and
  internal indentation."
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ember.errors import SkillLoadError
from ember.skills.base import DeclarativeSkill


def _import_load_markdown():
    from ember.skills.markdown import load_markdown

    return load_markdown


# --- SKILL.md text helpers -----------------------------------------------


_VALID_FULL = """\
---
name: demo
description: A demo declarative skill.
tools:
  - mealie_search
  - mealie_random
preferred_model: claude_cli:sonnet-4-6
suggest_when:
  - meal planning
  - what should we eat
---
You are the demo skill.

Use the available tools to help the user.
"""


_VALID_MINIMAL = """\
---
name: demo
description: Just the basics.
---
Body content.
"""


_VALID_ALLOWED_TOOLS_ALIAS = """\
---
name: demo
description: Uses the `allowed-tools` alias for tools.
allowed-tools:
  - mealie_search
  - mealie_random
---
You are the demo skill.
"""


_NO_FRONTMATTER = """\
This file does not begin with three dashes.

name: demo
description: not actually frontmatter
"""


_MALFORMED_YAML = """\
---
name: demo
description: "unclosed quote
tools: [a, b
---
body
"""


_NO_NAME = """\
---
description: Missing name.
---
body
"""


_NO_DESCRIPTION = """\
---
name: demo
---
body
"""


_EMPTY_NAME = """\
---
name: ""
description: Empty name.
---
body
"""


_EMPTY_DESCRIPTION = """\
---
name: demo
description: ""
---
body
"""


_NAME_MISMATCH = """\
---
name: different
description: Parent dir is `demo` but name is `different`.
---
body
"""


_HAS_SCHEDULED_RUNS = """\
---
name: demo
description: Declarative manifest carrying scheduled_runs (TLC-13 violation).
scheduled_runs:
  - cron: "0 5 * * *"
    function: tick
    name: morning
---
body
"""


_UNKNOWN_KEY = """\
---
name: demo
description: Has an unknown top-level key.
totally_unknown_field: nope
---
body
"""


_BOTH_TOOLS_AND_ALLOWED_TOOLS = """\
---
name: demo
description: Both `tools` and `allowed-tools` present — should be rejected.
tools:
  - foo
allowed-tools:
  - bar
---
body
"""


_TOOLS_NOT_LIST = """\
---
name: demo
description: tools should be a list.
tools: mealie_search
---
body
"""


_TOOLS_LIST_OF_NON_STRINGS = """\
---
name: demo
description: tools must be list[str].
tools:
  - mealie_search
  - 42
---
body
"""


_SYSTEM_PROMPT_IN_FRONTMATTER = """\
---
name: demo
description: The body is the system prompt; frontmatter `system_prompt` is unknown.
system_prompt: this should not be here
---
body
"""


_PREFERRED_MODEL_NOT_STRING = """\
---
name: demo
description: preferred_model must be str.
preferred_model: 42
---
body
"""


_SUGGEST_WHEN_NOT_LIST = """\
---
name: demo
description: suggest_when must be a list.
suggest_when: meal planning
---
body
"""


_SUGGEST_WHEN_LIST_OF_NON_STRINGS = """\
---
name: demo
description: suggest_when must be list[str].
suggest_when:
  - meal planning
  - 42
---
body
"""


_EMPTY_BODY = """\
---
name: demo
description: Frontmatter only; body is whitespace.
---


"""


_BODY_WITH_SURROUNDING_BLANK_LINES = """\
---
name: demo
description: Body has leading/trailing blank lines.
---


First line.

  Indented line.

Last line.


"""


# --- Fixtures -------------------------------------------------------------


@pytest.fixture
def skills_dir(tmp_path: Path) -> Path:
    d = tmp_path / "skills"
    d.mkdir()
    return d


def _write_skill_md(skills_dir: Path, name: str, content: str) -> Path:
    """Create `<skills_dir>/<name>/SKILL.md` and write `content`. Return the file path."""
    skill_dir = skills_dir / name
    skill_dir.mkdir()
    path = skill_dir / "SKILL.md"
    path.write_text(content)
    return path


# --- Happy paths ---------------------------------------------------------


class TestHappyPath:
    def test_full_frontmatter_round_trip(self, skills_dir: Path) -> None:
        path = _write_skill_md(skills_dir, "demo", _VALID_FULL)
        load_markdown = _import_load_markdown()

        skill = load_markdown(path)
        assert isinstance(skill, DeclarativeSkill)
        assert skill.name == "demo"
        assert skill.description == "A demo declarative skill."
        # tuple per the dataclass definition.
        assert skill.tools == ("mealie_search", "mealie_random")
        assert skill.preferred_model == "claude_cli:sonnet-4-6"
        assert skill.suggest_when == ("meal planning", "what should we eat")
        assert skill.source_path == str(path)
        # Body becomes system_prompt; outer whitespace trimmed, internal preserved.
        assert skill.system_prompt is not None
        assert skill.system_prompt.startswith("You are the demo skill.")
        assert skill.system_prompt.endswith("Use the available tools to help the user.")
        # Internal blank line preserved between paragraphs.
        assert "\n\n" in skill.system_prompt

    def test_minimal_frontmatter(self, skills_dir: Path) -> None:
        path = _write_skill_md(skills_dir, "demo", _VALID_MINIMAL)
        load_markdown = _import_load_markdown()

        skill = load_markdown(path)
        assert skill.name == "demo"
        assert skill.description == "Just the basics."
        # Optional fields default to empty/None per the dataclass.
        assert skill.tools == ()
        assert skill.preferred_model is None
        assert skill.suggest_when == ()
        assert skill.system_prompt == "Body content."
        assert skill.source_path == str(path)

    def test_fc14_allowed_tools_alias_works(self, skills_dir: Path) -> None:
        path = _write_skill_md(skills_dir, "demo", _VALID_ALLOWED_TOOLS_ALIAS)
        load_markdown = _import_load_markdown()

        skill = load_markdown(path)
        assert skill.tools == ("mealie_search", "mealie_random")

    def test_empty_body_yields_none_system_prompt(self, skills_dir: Path) -> None:
        path = _write_skill_md(skills_dir, "demo", _EMPTY_BODY)
        load_markdown = _import_load_markdown()

        skill = load_markdown(path)
        assert skill.system_prompt is None

    def test_body_with_surrounding_blank_lines_is_trimmed(self, skills_dir: Path) -> None:
        path = _write_skill_md(skills_dir, "demo", _BODY_WITH_SURROUNDING_BLANK_LINES)
        load_markdown = _import_load_markdown()

        skill = load_markdown(path)
        assert skill.system_prompt is not None
        # Outer whitespace trimmed.
        assert skill.system_prompt.startswith("First line.")
        assert skill.system_prompt.endswith("Last line.")
        # Internal whitespace preserved (blank lines between content,
        # leading-space indent on the middle line).
        assert "\n\n" in skill.system_prompt
        assert "  Indented line." in skill.system_prompt


# --- Rejections ----------------------------------------------------------


class TestRejections:
    def test_no_frontmatter_raises(self, skills_dir: Path) -> None:
        path = _write_skill_md(skills_dir, "demo", _NO_FRONTMATTER)
        load_markdown = _import_load_markdown()

        with pytest.raises(SkillLoadError):
            load_markdown(path)

    def test_malformed_yaml_raises(self, skills_dir: Path) -> None:
        path = _write_skill_md(skills_dir, "demo", _MALFORMED_YAML)
        load_markdown = _import_load_markdown()

        with pytest.raises(SkillLoadError):
            load_markdown(path)

    def test_missing_name_raises(self, skills_dir: Path) -> None:
        path = _write_skill_md(skills_dir, "demo", _NO_NAME)
        load_markdown = _import_load_markdown()

        with pytest.raises(SkillLoadError):
            load_markdown(path)

    def test_empty_name_raises(self, skills_dir: Path) -> None:
        path = _write_skill_md(skills_dir, "demo", _EMPTY_NAME)
        load_markdown = _import_load_markdown()

        with pytest.raises(SkillLoadError):
            load_markdown(path)

    def test_missing_description_raises(self, skills_dir: Path) -> None:
        path = _write_skill_md(skills_dir, "demo", _NO_DESCRIPTION)
        load_markdown = _import_load_markdown()

        with pytest.raises(SkillLoadError):
            load_markdown(path)

    def test_empty_description_raises(self, skills_dir: Path) -> None:
        path = _write_skill_md(skills_dir, "demo", _EMPTY_DESCRIPTION)
        load_markdown = _import_load_markdown()

        with pytest.raises(SkillLoadError):
            load_markdown(path)

    def test_fc14_name_must_match_parent_dir(self, skills_dir: Path) -> None:
        # File at `<skills_dir>/demo/SKILL.md` carries `name: different` —
        # mismatch with parent dir name should be rejected.
        path = _write_skill_md(skills_dir, "demo", _NAME_MISMATCH)
        load_markdown = _import_load_markdown()

        with pytest.raises(SkillLoadError):
            load_markdown(path)

    def test_scheduled_runs_present_raises_tlc13(self, skills_dir: Path) -> None:
        # TLC-13: declarative manifests cannot be scheduled.
        path = _write_skill_md(skills_dir, "demo", _HAS_SCHEDULED_RUNS)
        load_markdown = _import_load_markdown()

        with pytest.raises(SkillLoadError):
            load_markdown(path)

    def test_unknown_top_level_key_is_ignored(self, skills_dir: Path) -> None:
        # FC-14 (post-v8 amendment): unknown frontmatter keys are
        # silently ignored, not rejected. Real-world SKILL.md files
        # commonly carry vendor metadata (emoji, homepage, metadata,
        # tags, etc.) and we accept them as-is so drop-in works.
        path = _write_skill_md(skills_dir, "demo", _UNKNOWN_KEY)
        load_markdown = _import_load_markdown()

        skill = load_markdown(path)
        assert skill.name == "demo"
        assert skill.description == "Has an unknown top-level key."
        # The unknown field is not stashed anywhere on the skill —
        # it's just dropped.
        assert not hasattr(skill, "totally_unknown_field")

    def test_both_tools_and_allowed_tools_raises(self, skills_dir: Path) -> None:
        path = _write_skill_md(skills_dir, "demo", _BOTH_TOOLS_AND_ALLOWED_TOOLS)
        load_markdown = _import_load_markdown()

        with pytest.raises(SkillLoadError):
            load_markdown(path)

    def test_tools_not_a_list_raises(self, skills_dir: Path) -> None:
        path = _write_skill_md(skills_dir, "demo", _TOOLS_NOT_LIST)
        load_markdown = _import_load_markdown()

        with pytest.raises(SkillLoadError):
            load_markdown(path)

    def test_tools_list_of_non_strings_raises(self, skills_dir: Path) -> None:
        path = _write_skill_md(skills_dir, "demo", _TOOLS_LIST_OF_NON_STRINGS)
        load_markdown = _import_load_markdown()

        with pytest.raises(SkillLoadError):
            load_markdown(path)

    def test_system_prompt_in_frontmatter_is_ignored_body_is_used(self, skills_dir: Path) -> None:
        # The body IS the system prompt. A `system_prompt:` field in
        # frontmatter is an unknown key (post-v8 amendment): silently
        # ignored, with the body still becoming the actual system
        # prompt.
        path = _write_skill_md(skills_dir, "demo", _SYSTEM_PROMPT_IN_FRONTMATTER)
        load_markdown = _import_load_markdown()

        skill = load_markdown(path)
        # The frontmatter `system_prompt:` value ("this should not be
        # here") is dropped; the body ("body") is what ends up as
        # system_prompt.
        assert skill.system_prompt == "body"

    def test_preferred_model_not_string_raises(self, skills_dir: Path) -> None:
        path = _write_skill_md(skills_dir, "demo", _PREFERRED_MODEL_NOT_STRING)
        load_markdown = _import_load_markdown()

        with pytest.raises(SkillLoadError):
            load_markdown(path)

    def test_suggest_when_not_list_raises(self, skills_dir: Path) -> None:
        path = _write_skill_md(skills_dir, "demo", _SUGGEST_WHEN_NOT_LIST)
        load_markdown = _import_load_markdown()

        with pytest.raises(SkillLoadError):
            load_markdown(path)

    def test_suggest_when_list_of_non_strings_raises(self, skills_dir: Path) -> None:
        path = _write_skill_md(skills_dir, "demo", _SUGGEST_WHEN_LIST_OF_NON_STRINGS)
        load_markdown = _import_load_markdown()

        with pytest.raises(SkillLoadError):
            load_markdown(path)
