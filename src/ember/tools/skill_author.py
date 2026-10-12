"""Skill-author tools (FC-3, FC-5, UoW-10/11, v7).

Three tools the agent uses to draft new skills:

1. `SkillSaveManifestTool` — writes a declarative TOML manifest to
   `<skills_dir>/<name>.toml` atomically. Validates `name`, validates
   tool references against the live tool registry, refuses path
   traversal.

2. `SkillDraftPythonTool` — drafts a Python imperative skill at
   `<skills_dir>/_drafts/<name>.py`. Loaders skip `_*` files so drafts
   never auto-load — the user reviews and moves the file into place.

3. `SkillListKnownToolsTool` — exposes the current tool registry to
   the agent so it can decide which tool names to reference.

All three are non-destructive (UoW-10) — `requires_confirmation=False`.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

from ember.tools.base import ToolResult
from ember.tools.registry import Registry

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _validate_name(name: Any) -> str | None:
    """Return an error message if `name` is not a valid skill name, else None."""
    if not isinstance(name, str):
        return f"name must be a string, got {type(name).__name__}"
    if not _NAME_RE.match(name):
        return (
            f"invalid skill name {name!r}: must match `^[a-z][a-z0-9_]*$` "
            "(lowercase letter first, then lowercase/digits/underscores)"
        )
    return None


def _atomic_write(target: Path, body: str) -> None:
    """Write `body` to `target` atomically via a sibling tempfile + rename.

    The tempfile lives in the same directory as `target` so `replace`
    is a single rename(2) on the same filesystem (atomic).
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent),
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    tmp_path = Path(tmp_name)
    try:
        os.close(fd)
        try:
            tmp_path.write_text(body)
        except BaseException:
            # Failed before rename: clean up the temp file.
            try:
                tmp_path.unlink()
            except OSError:
                pass
            raise
        try:
            tmp_path.replace(target)
        except BaseException:
            try:
                tmp_path.unlink()
            except OSError:
                pass
            raise
    except BaseException:
        # Belt-and-suspenders: ensure no `.tmp` lingers.
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


def _toml_escape_basic(s: str) -> str:
    """Escape a string for use inside a basic TOML `"..."` string."""
    out = []
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return "".join(out)


def _render_string(s: str) -> str:
    """Render a TOML string. Multi-line strings use triple-quoted
    blocks (single-line just uses regular basic strings)."""
    if "\n" in s:
        # Triple-quoted: only need to escape backslashes and `"""` runs.
        escaped = s.replace("\\", "\\\\").replace('"""', '\\"""')
        return f'"""\n{escaped}"""'
    return f'"{_toml_escape_basic(s)}"'


def _render_string_list(items: list[str]) -> str:
    parts = [f'"{_toml_escape_basic(x)}"' for x in items]
    return "[" + ", ".join(parts) + "]"


def _render_manifest(
    *,
    name: str,
    description: str,
    tools: list[str] | None,
    system_prompt: str | None,
    preferred_model: str | None,
    suggest_when: list[str] | None,
) -> str:
    lines = []
    lines.append(f"name = {_render_string(name)}")
    lines.append(f"description = {_render_string(description)}")
    if tools is not None:
        lines.append(f"tools = {_render_string_list(tools)}")
    if system_prompt is not None:
        lines.append(f"system_prompt = {_render_string(system_prompt)}")
    if preferred_model is not None:
        lines.append(f"preferred_model = {_render_string(preferred_model)}")
    if suggest_when is not None:
        lines.append(f"suggest_when = {_render_string_list(suggest_when)}")
    return "\n".join(lines) + "\n"


class SkillSaveManifestTool:
    """Save a declarative skill manifest to `<skills_dir>/<name>.toml`."""

    name: str = "skill_save_manifest"
    description: str = (
        "Save a declarative skill manifest to /data/skills/<name>.toml. "
        "Validates name and that every entry in `tools` is a registered tool."
    )
    requires_confirmation: bool = False

    def __init__(self, *, registry: Registry, skills_dir: Path) -> None:
        self._registry = registry
        self._skills_dir = Path(skills_dir)
        self.input_schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Skill name; must match `^[a-z][a-z0-9_]*$`.",
                },
                "description": {
                    "type": "string",
                    "description": "One-line description of what the skill does.",
                },
                "tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Names of tools (in the live registry) the skill exposes.",
                },
                "system_prompt": {
                    "type": "string",
                    "description": "System prompt to append when this skill is active.",
                },
                "preferred_model": {
                    "type": "string",
                    "description": "Provider:model preference, e.g. `claude_cli:sonnet-4-6`.",
                },
                "suggest_when": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Trigger phrases the agent may use to softly suggest this skill.",
                },
            },
            "required": ["name", "description"],
            "additionalProperties": False,
        }

    async def call(self, args: dict[str, Any]) -> ToolResult:
        name = args.get("name")
        err = _validate_name(name)
        if err is not None:
            return ToolResult(output={"error": err}, is_error=True)

        # Path-traversal guardrail: confirm the resolved target stays
        # inside `skills_dir`. With a name validated by `_NAME_RE` this
        # is always true, but we double-check defensively.
        target = self._skills_dir / f"{name}.toml"
        try:
            resolved = target.resolve()
            base = self._skills_dir.resolve()
        except OSError as e:
            return ToolResult(
                output={"error": f"path resolution failed: {e}"},
                is_error=True,
            )
        if base != resolved.parent:
            return ToolResult(
                output={"error": f"refusing to write outside skills_dir: {target}"},
                is_error=True,
            )

        description = args.get("description")
        if not isinstance(description, str) or not description:
            return ToolResult(
                output={"error": "`description` is required and must be a non-empty string"},
                is_error=True,
            )

        raw_tools = args.get("tools")
        tools: list[str] | None = None
        if raw_tools is not None:
            if not isinstance(raw_tools, list) or not all(isinstance(t, str) for t in raw_tools):
                return ToolResult(
                    output={"error": "`tools` must be a list of strings"},
                    is_error=True,
                )
            known = set(self._registry.names())
            unknown = [t for t in raw_tools if t not in known]
            if unknown:
                return ToolResult(
                    output={
                        "error": (
                            f"unknown tool(s): {', '.join(sorted(unknown))} — "
                            "must be registered in the tool registry"
                        )
                    },
                    is_error=True,
                )
            tools = list(raw_tools)

        system_prompt = args.get("system_prompt")
        if system_prompt is not None and not isinstance(system_prompt, str):
            return ToolResult(
                output={"error": "`system_prompt` must be a string"},
                is_error=True,
            )

        preferred_model = args.get("preferred_model")
        if preferred_model is not None and not isinstance(preferred_model, str):
            return ToolResult(
                output={"error": "`preferred_model` must be a string"},
                is_error=True,
            )

        raw_suggest = args.get("suggest_when")
        suggest_when: list[str] | None = None
        if raw_suggest is not None:
            if not isinstance(raw_suggest, list) or not all(
                isinstance(s, str) for s in raw_suggest
            ):
                return ToolResult(
                    output={"error": "`suggest_when` must be a list of strings"},
                    is_error=True,
                )
            suggest_when = list(raw_suggest)

        body = _render_manifest(
            name=name,
            description=description,
            tools=tools,
            system_prompt=system_prompt,
            preferred_model=preferred_model,
            suggest_when=suggest_when,
        )

        try:
            self._skills_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write(target, body)
        except OSError as e:
            return ToolResult(
                output={"error": f"failed to write manifest: {e}"},
                is_error=True,
            )

        return ToolResult(output={"path": str(target), "name": name})


class SkillDraftPythonTool:
    """Draft an imperative Python skill into `<skills_dir>/_drafts/<name>.py`."""

    name: str = "skill_draft_python"
    description: str = (
        "Draft an imperative Python skill at /data/skills/_drafts/<name>.py. "
        "Drafts are NOT auto-loaded — the user moves them into place after review. "
        "This is the ONLY way to schedule recurring work — include a "
        '`scheduled_runs = [{"cron": "<expr>", "timezone": "<tz>", '
        '"function": "<async_fn_name>", "name": "<label>"}]` block '
        "alongside the matching `async def` to register a cron job. Do not "
        "propose host cron / systemd — you have no shell on the host."
    )
    requires_confirmation: bool = False

    def __init__(self, *, registry: Registry, skills_dir: Path) -> None:
        self._registry = registry
        self._skills_dir = Path(skills_dir)
        self.input_schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Skill name; must match `^[a-z][a-z0-9_]*$`.",
                },
                "body": {
                    "type": "string",
                    "description": "Full Python source for the skill module.",
                },
            },
            "required": ["name", "body"],
            "additionalProperties": False,
        }

    async def call(self, args: dict[str, Any]) -> ToolResult:
        name = args.get("name")
        err = _validate_name(name)
        if err is not None:
            return ToolResult(output={"error": err}, is_error=True)

        body = args.get("body")
        if not isinstance(body, str):
            return ToolResult(
                output={"error": "`body` must be a string"},
                is_error=True,
            )

        drafts_dir = self._skills_dir / "_drafts"
        target = drafts_dir / f"{name}.py"
        try:
            resolved = target.resolve()
            base = (
                drafts_dir.resolve()
                if drafts_dir.exists()
                else (self._skills_dir.resolve() / "_drafts")
            )
        except OSError as e:
            return ToolResult(
                output={"error": f"path resolution failed: {e}"},
                is_error=True,
            )
        if base != resolved.parent:
            return ToolResult(
                output={"error": f"refusing to write outside _drafts: {target}"},
                is_error=True,
            )

        try:
            drafts_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write(target, body)
        except OSError as e:
            return ToolResult(
                output={"error": f"failed to write draft: {e}"},
                is_error=True,
            )

        return ToolResult(output={"path": str(target), "name": name})


class SkillListKnownToolsTool:
    """List every tool currently registered in the live tool registry."""

    name: str = "skill_list_known_tools"
    description: str = (
        "List the names + descriptions of every tool the agent can wire into a skill."
    )
    requires_confirmation: bool = False

    def __init__(self, *, registry: Registry, skills_dir: Path | None = None) -> None:
        # `skills_dir` is unused but accepted to keep the constructor
        # signature uniform across the three skill-author tools.
        self._registry = registry
        self.input_schema: dict[str, Any] = {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }

    async def call(self, args: dict[str, Any]) -> ToolResult:
        tools = [
            {"name": t.name, "description": t.description} for t in self._registry.list_tools()
        ]
        return ToolResult(output={"tools": tools})
