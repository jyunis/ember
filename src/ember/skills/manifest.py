"""Declarative skill manifest loader (FC-5, TLC-13).

A declarative skill is a TOML file at `/data/skills/<name>.toml`
carrying these top-level keys: `name` (required, must match filename
stem), `description` (required), `tools` (optional `list[str]`),
`system_prompt` (optional `str` — TOML triple-quoted blocks work
fine), `preferred_model` (optional `str` shaped `<provider>:<model>`),
and `suggest_when` (optional `list[str]`).

Validation rules:
- `name` and `description` are required and non-empty.
- `name` must match the filename stem.
- `tools` (if present) is `list[str]`.
- `system_prompt` (if present) is `str`.
- `preferred_model` (if present) is `str` shaped `<provider>:<model>`.
- `suggest_when` (if present) is `list[str]`.
- `scheduled_runs` is **rejected** (TLC-13: only imperative skills can
  be scheduled).
- Any unknown top-level key is rejected.

Tool-name resolution against the live registry is the loader's job
(same as imperative skills); this module just shapes the manifest.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from ember.errors import SkillLoadError
from ember.skills.base import DeclarativeSkill

_ALLOWED_KEYS = frozenset(
    {
        "name",
        "description",
        "tools",
        "system_prompt",
        "preferred_model",
        "suggest_when",
    }
)


def load_declarative(path: Path) -> DeclarativeSkill:
    """Parse a TOML manifest into a `DeclarativeSkill`.

    Raises:
        SkillLoadError: malformed TOML, missing required fields,
            unknown keys, name/stem mismatch, or `scheduled_runs`
            present (TLC-13).
    """
    try:
        raw = path.read_text()
    except OSError as e:
        raise SkillLoadError(f"{path}: cannot read manifest: {e}") from e

    try:
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as e:
        raise SkillLoadError(f"{path}: malformed TOML: {e}") from e

    if "scheduled_runs" in data:
        raise SkillLoadError(
            f"{path}: declarative manifests cannot define `scheduled_runs` (TLC-13)"
        )

    unknown = set(data.keys()) - _ALLOWED_KEYS
    if unknown:
        raise SkillLoadError(f"{path}: unknown top-level key(s): {', '.join(sorted(unknown))}")

    name = data.get("name")
    if not isinstance(name, str) or not name:
        raise SkillLoadError(f"{path}: `name` is required and must be a non-empty string")

    if name != path.stem:
        raise SkillLoadError(
            f"{path}: manifest name {name!r} does not match filename stem {path.stem!r}"
        )

    description = data.get("description")
    if not isinstance(description, str) or not description:
        raise SkillLoadError(f"{path}: `description` is required and must be a non-empty string")

    raw_tools = data.get("tools")
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

    system_prompt = data.get("system_prompt")
    if system_prompt is not None and not isinstance(system_prompt, str):
        raise SkillLoadError(
            f"{path}: `system_prompt` must be a string, got {type(system_prompt).__name__}"
        )

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

    return DeclarativeSkill(
        name=name,
        description=description,
        tools=tools,
        system_prompt=system_prompt,
        preferred_model=preferred_model,
        suggest_when=suggest_when,
        source_path=str(path),
    )
