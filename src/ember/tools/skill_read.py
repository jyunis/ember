"""skill_read — expose SKILL.md body + references/* to the agent.

The router-mode system prompt only carries skill names and first-line
descriptions (FC-18). When the model needs more — e.g. to find an MCP
server URL declared in a SKILL.md or one of its `references/` files
before calling `mcp_connect` — it uses `skill_read`.

Two modes:
- `skill_read(name)` — returns the skill's body (system_prompt) plus
  metadata (description, tools, source_path, available files).
- `skill_read(name, file="references/setup.md")` — returns the
  contents of a companion file under the skill's directory. Path is
  rejected if it escapes the skill dir or hits a non-regular file.

Bytes returned are capped to keep tool_result events manageable; the
truncation is reported in the response (`truncated: true`).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ember.skills.base import SkillRegistry
from ember.tools.base import ToolResult

_log = logging.getLogger("ember.tools.skill_read")

_MAX_BYTES = 32_000  # ~8k tokens at 4 chars/token; well below Discord/MCP limits.
_MAX_FILES_LISTED = 50


class SkillReadTool:
    name = "skill_read"
    description = (
        "Read a loaded skill's SKILL.md body or a companion file under "
        "its directory (e.g. `references/setup.md`). Use this when you "
        "need details a skill carries — like an MCP server URL or a "
        "setup procedure — that aren't in the system prompt's first-line "
        "summary. Pass `file` to read a specific companion; omit it to "
        "get the skill body."
    )
    requires_confirmation: bool = False
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Skill name as listed in the system prompt.",
            },
            "file": {
                "type": ["string", "null"],
                "default": None,
                "description": (
                    "Optional path under the skill's directory "
                    "(e.g. `references/setup.md`). Forward slashes only. "
                    "When omitted, returns the SKILL.md body."
                ),
            },
        },
        "required": ["name"],
        "additionalProperties": False,
    }

    def __init__(self, *, skill_registry: SkillRegistry) -> None:
        self._skill_registry = skill_registry

    async def call(self, args: dict[str, Any]) -> ToolResult:
        name = args["name"]
        file_arg = args.get("file") or None

        try:
            skill = self._skill_registry.get(name)
        except KeyError:
            return ToolResult(
                output={
                    "error": "unknown_skill",
                    "name": name,
                    "available": self._skill_registry.names(),
                },
                is_error=True,
            )

        skill_dir = self._skill_dir(skill)

        if file_arg is None:
            body = getattr(skill, "system_prompt", None) or ""
            body, truncated = _truncate(body)
            return ToolResult(
                output={
                    "name": skill.name,
                    "description": getattr(skill, "description", ""),
                    "tools": list(getattr(skill, "tools", ()) or ()),
                    "source_path": getattr(skill, "source_path", None),
                    "skill_dir": str(skill_dir) if skill_dir else None,
                    "available_files": self._enumerate_files(skill_dir),
                    "body": body,
                    "truncated": truncated,
                }
            )

        if skill_dir is None:
            return ToolResult(
                output={
                    "error": "no_skill_dir",
                    "name": name,
                    "message": (
                        "this skill is a single .py/.toml file with no "
                        "companion directory; pass `file=null` to read its body"
                    ),
                },
                is_error=True,
            )

        resolved = self._resolve_safely(skill_dir, file_arg)
        if resolved is None:
            return ToolResult(
                output={
                    "error": "invalid_path",
                    "name": name,
                    "file": file_arg,
                    "message": "path escapes the skill directory or is not a regular file",
                },
                is_error=True,
            )

        try:
            data = resolved.read_text()
        except OSError as e:
            return ToolResult(
                output={
                    "error": "read_failed",
                    "name": name,
                    "file": file_arg,
                    "message": f"{type(e).__name__}: {e}",
                },
                is_error=True,
            )

        body, truncated = _truncate(data)
        return ToolResult(
            output={
                "name": skill.name,
                "file": file_arg,
                "path": str(resolved),
                "body": body,
                "truncated": truncated,
            }
        )

    @staticmethod
    def _skill_dir(skill: Any) -> Path | None:
        """The directory companion files live in. For SKILL.md skills,
        that's the parent of `source_path`. For single-file .py/.toml
        skills, there is no companion dir — return None."""
        sp = getattr(skill, "source_path", None)
        if not sp:
            return None
        path = Path(sp)
        if path.name == "SKILL.md":
            return path.parent
        return None

    @staticmethod
    def _enumerate_files(skill_dir: Path | None) -> list[str]:
        """List companion files under `skill_dir`, paths relative to
        `skill_dir`, forward slashes. Capped at `_MAX_FILES_LISTED` so
        a runaway directory doesn't bloat the tool result."""
        if skill_dir is None:
            return []
        out: list[str] = []
        try:
            for path in sorted(skill_dir.rglob("*")):
                if not path.is_file():
                    continue
                rel = path.relative_to(skill_dir).as_posix()
                if rel == "SKILL.md":
                    continue
                out.append(rel)
                if len(out) >= _MAX_FILES_LISTED:
                    break
        except OSError:
            pass
        return out

    @staticmethod
    def _resolve_safely(skill_dir: Path, file_arg: str) -> Path | None:
        """Resolve `file_arg` against `skill_dir`, rejecting any path
        that escapes the directory or doesn't point at a regular file."""
        # Reject backslashes / absolute paths up front — frontmatter
        # convention is forward slashes.
        if "\\" in file_arg:
            return None
        candidate = (skill_dir / file_arg).resolve()
        try:
            candidate.relative_to(skill_dir.resolve())
        except ValueError:
            return None
        if not candidate.is_file():
            return None
        return candidate


def _truncate(body: str) -> tuple[str, bool]:
    encoded = body.encode("utf-8")
    if len(encoded) <= _MAX_BYTES:
        return body, False
    truncated = encoded[:_MAX_BYTES].decode("utf-8", errors="ignore")
    return truncated, True
