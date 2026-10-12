"""SKILL.md declarative skill loader (FC-14, TLC-13, v8).

A SKILL.md file lives at `<skills_dir>/<name>/SKILL.md` and consists
of YAML frontmatter (between `---` lines) followed by a markdown body.
The body becomes the skill's `system_prompt`; the frontmatter carries
the same metadata as the parallel TOML manifest loader.

Validation rules (FC-14):
- File must begin with YAML frontmatter (between `---` lines).
- `name` (required, non-empty) must equal `path.parent.name` (the
  enclosing skill directory's name).
- `description` (required, non-empty).
- `tools` / `allowed-tools` are interchangeable aliases for
  `list[str]`. Both present is rejected.
- `preferred_model` (optional `str`).
- `suggest_when` (optional `list[str]`).
- `scheduled_runs` is **rejected** (TLC-13: only imperative skills can
  be scheduled).
- Unknown frontmatter top-level keys are silently ignored (logged at
  debug level). Real-world SKILL.md files in the broader Agent Skills
  ecosystem commonly carry vendor metadata (`emoji`, `homepage`,
  `metadata`, `tags`, etc.); rejecting them would defeat the drop-in
  goal. Note that `system_prompt:` in frontmatter is one of those
  ignored keys — the body IS the system prompt, and a frontmatter
  `system_prompt:` field has no effect.

Body becomes `system_prompt`, with leading/trailing whitespace
stripped (`body.strip()`); internal whitespace is preserved verbatim.
A whitespace-only body yields `system_prompt = None`.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from ember.errors import SkillLoadError
from ember.skills.base import DeclarativeSkill

_log = logging.getLogger("ember.skills.markdown")

# Frontmatter keys recognized by the loader. `allowed-tools` is the
# alias for `tools` (both present is rejected); `system_prompt` is
# deliberately absent because the body is the system prompt.
_ALLOWED_KEYS = frozenset(
    {
        "name",
        "description",
        "tools",
        "allowed-tools",
        "preferred_model",
        "suggest_when",
    }
)


def _split_frontmatter(text: str, path: Path) -> tuple[str, str]:
    """Split SKILL.md text into (frontmatter, body).

    Standard YAML-frontmatter convention: the file's first non-empty
    line must be `---` (with optional surrounding whitespace), and the
    frontmatter ends at the next line that is exactly `---` (again,
    surrounding whitespace tolerated). Everything after that line is
    the body.

    Raises SkillLoadError if the file does not begin with `---` or no
    closing delimiter is found.
    """
    lines = text.splitlines(keepends=True)

    # Find the opening delimiter: first non-empty stripped line must be `---`.
    open_idx: int | None = None
    for i, line in enumerate(lines):
        if line.strip() == "":
            continue
        if line.strip() == "---":
            open_idx = i
            break
        # First non-empty line is not `---` → no frontmatter.
        raise SkillLoadError(f"{path}: SKILL.md must begin with YAML frontmatter")

    if open_idx is None:
        raise SkillLoadError(f"{path}: SKILL.md must begin with YAML frontmatter")

    # Find the closing delimiter starting after open_idx.
    close_idx: int | None = None
    for j in range(open_idx + 1, len(lines)):
        if lines[j].strip() == "---":
            close_idx = j
            break

    if close_idx is None:
        raise SkillLoadError(f"{path}: SKILL.md frontmatter is missing closing `---` delimiter")

    frontmatter = "".join(lines[open_idx + 1 : close_idx])
    body = "".join(lines[close_idx + 1 :])
    return frontmatter, body


def load_markdown(path: Path) -> DeclarativeSkill:
    """Parse a SKILL.md file into a `DeclarativeSkill`.

    Raises:
        SkillLoadError: missing frontmatter, malformed YAML, missing
            required fields, name mismatch with parent directory,
            unknown keys, both `tools` and `allowed-tools` present,
            type mismatches, or `scheduled_runs` present (TLC-13).
    """
    try:
        raw = path.read_text()
    except OSError as e:
        raise SkillLoadError(f"{path}: cannot read SKILL.md: {e}") from e

    frontmatter_text, body = _split_frontmatter(raw, path)

    try:
        data = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as e:
        raise SkillLoadError(f"{path}: malformed YAML frontmatter: {e}") from e

    if data is None:
        # Empty frontmatter → treat as missing required fields below.
        data = {}

    if not isinstance(data, dict):
        raise SkillLoadError(
            f"{path}: YAML frontmatter must be a mapping, got {type(data).__name__}"
        )

    if "scheduled_runs" in data:
        raise SkillLoadError(
            f"{path}: declarative manifests cannot define `scheduled_runs` (TLC-13)"
        )

    # Unknown frontmatter keys are silently ignored (logged at debug
    # level). SKILL.md files in the wild commonly carry vendor metadata
    # such as `emoji`, `homepage`, `metadata`, `tags` — rejecting them
    # would defeat the drop-in goal. We accept and ignore.
    unknown = set(data.keys()) - _ALLOWED_KEYS
    if unknown:
        _log.debug(
            "%s: ignoring unknown frontmatter key(s): %s",
            path,
            ", ".join(sorted(unknown)),
        )

    # tools / allowed-tools: both present is rejected; otherwise normalize.
    if "tools" in data and "allowed-tools" in data:
        raise SkillLoadError(f"{path}: `tools` and `allowed-tools` are aliases; specify only one")

    name = data.get("name")
    if not isinstance(name, str) or not name:
        raise SkillLoadError(f"{path}: `name` is required and must be a non-empty string")

    expected_name = path.parent.name
    if name != expected_name:
        raise SkillLoadError(
            f"{path}: manifest name {name!r} does not match parent directory name {expected_name!r}"
        )

    description = data.get("description")
    if not isinstance(description, str) or not description:
        raise SkillLoadError(f"{path}: `description` is required and must be a non-empty string")

    raw_tools = data.get("tools") if "tools" in data else data.get("allowed-tools")
    if raw_tools is None:
        tools: tuple[str, ...] = ()
    else:
        if not isinstance(raw_tools, list):
            raise SkillLoadError(
                f"{path}: `tools` must be a list[str], got {type(raw_tools).__name__}"
            )
        for entry in raw_tools:
            if not isinstance(entry, str):
                raise SkillLoadError(
                    f"{path}: every entry in `tools` must be a str (got {type(entry).__name__})"
                )
        tools = tuple(raw_tools)

    preferred_model = data.get("preferred_model")
    if preferred_model is not None and not isinstance(preferred_model, str):
        raise SkillLoadError(
            f"{path}: `preferred_model` must be a string, got {type(preferred_model).__name__}"
        )

    raw_suggest = data.get("suggest_when")
    if raw_suggest is None:
        suggest_when: tuple[str, ...] = ()
    else:
        if not isinstance(raw_suggest, list):
            raise SkillLoadError(
                f"{path}: `suggest_when` must be a list[str], got {type(raw_suggest).__name__}"
            )
        for entry in raw_suggest:
            if not isinstance(entry, str):
                raise SkillLoadError(
                    f"{path}: every entry in `suggest_when` must be a str "
                    f"(got {type(entry).__name__})"
                )
        suggest_when = tuple(raw_suggest)

    stripped_body = body.strip()
    system_prompt: str | None = stripped_body if stripped_body else None

    return DeclarativeSkill(
        name=name,
        description=description,
        tools=tools,
        system_prompt=system_prompt,
        preferred_model=preferred_model,
        suggest_when=suggest_when,
        source_path=str(path),
    )
