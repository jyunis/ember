"""`import_skill` — install a user-uploaded skill file (BC-7).

Companion to `skill_save_manifest` / `skill_draft_python`: those tools
write skills the model authored. `import_skill` takes a file the user
already uploaded (saved under `data_dir/uploads/`) and copies it into
`<skills_dir>/`, then validates by calling `DirectorySkillLoader._reload_one`
synchronously so the user gets immediate success/failure feedback.

Confirmation-gated (TLC-3 / `requires_confirmation=True`) — installing
executable code into the skills directory is state-changing.

Source path discipline:
- The path argument MUST resolve under `data_dir/uploads/`. Anything
  else is rejected; this tool refuses to read arbitrary files.

Destination layout:
- `*.py` → `<skills_dir>/<basename>` (top-level imperative skill).
- `*.toml` → `<skills_dir>/<basename>` (declarative manifest).
- `<name>.SKILL.md` → `<skills_dir>/<name>/SKILL.md` (markdown skill;
  one-level directory layout matched by `DirectorySkillLoader`).

On validation failure (`SkillLoadError`), the destination file (and the
`<skills_dir>/<name>/` directory we just created for markdown) is removed
so the skills directory never holds half-installed skills.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from ember.errors import SkillLoadError
from ember.skills.loader import DirectorySkillLoader
from ember.tools.base import ToolResult


class ImportSkillTool:
    """Install a user-uploaded skill file into `<skills_dir>/`."""

    name: str = "import_skill"
    description: str = (
        "Install a skill from a previously-uploaded file. The source path "
        "must live under /data/uploads/. Supports `.py` (imperative skill), "
        "`.toml` (declarative manifest), and `<name>.SKILL.md` (markdown). "
        "Validates the skill before keeping it; on failure no file is left "
        "in the skills directory."
    )
    requires_confirmation: bool = True

    def __init__(self, *, loader: DirectorySkillLoader, data_dir: Path, skills_dir: Path) -> None:
        self._loader = loader
        self._data_dir = Path(data_dir).resolve()
        self._uploads_dir = (self._data_dir / "uploads").resolve()
        self._skills_dir = Path(skills_dir).resolve()
        self.input_schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Absolute path to the uploaded skill file. Must "
                        "resolve under /data/uploads/."
                    ),
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        }

    async def call(self, args: dict[str, Any]) -> ToolResult:
        raw = args.get("path")
        if not isinstance(raw, str) or not raw:
            return ToolResult(
                output={"ok": False, "name": "", "message": "`path` is required"},
                is_error=True,
            )

        try:
            src = Path(raw).resolve()
        except OSError as e:
            return ToolResult(
                output={"ok": False, "name": "", "message": f"path resolution failed: {e}"},
                is_error=True,
            )

        try:
            src.relative_to(self._uploads_dir)
        except ValueError:
            return ToolResult(
                output={
                    "ok": False,
                    "name": "",
                    "message": f"refusing to import: source path {raw!r} is outside uploads_dir",
                },
                is_error=True,
            )

        if not src.is_file():
            return ToolResult(
                output={"ok": False, "name": "", "message": f"source path {raw!r} not found"},
                is_error=True,
            )

        # Decide destination + cleanup target. `cleanup_dir` is non-None
        # only for markdown skills, where we create a per-skill directory
        # we should also remove on validation failure.
        dest, cleanup_dir, derived_name = self._resolve_destination(src)
        if dest is None:
            return ToolResult(
                output={
                    "ok": False,
                    "name": "",
                    "message": (
                        f"unsupported skill file {src.name!r}: must be `.py`, "
                        "`.toml`, or `<name>.SKILL.md`"
                    ),
                },
                is_error=True,
            )

        try:
            self._skills_dir.mkdir(parents=True, exist_ok=True)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
        except OSError as e:
            return ToolResult(
                output={
                    "ok": False,
                    "name": derived_name,
                    "message": f"failed to copy skill file: {e}",
                },
                is_error=True,
            )

        try:
            await self._loader._reload_one(dest)  # noqa: SLF001 — loader's own helper
        except SkillLoadError as e:
            self._cleanup(dest, cleanup_dir)
            return ToolResult(
                output={
                    "ok": False,
                    "name": derived_name,
                    "message": f"skill validation failed: {e}",
                },
                is_error=True,
            )
        except Exception as e:
            self._cleanup(dest, cleanup_dir)
            return ToolResult(
                output={
                    "ok": False,
                    "name": derived_name,
                    "message": f"skill validation crashed: {type(e).__name__}: {e}",
                },
                is_error=True,
            )

        # On success the loader already replaced the registry entry; we
        # don't need to re-resolve the skill name from the registry — the
        # filename-derived name is the canonical one for the manifest /
        # markdown loaders. Imperative `.py` modules carry an explicit
        # `name = ...` attr; surface that when we can.
        registered_name = self._registered_name(dest, derived_name)

        return ToolResult(
            output={
                "ok": True,
                "name": registered_name,
                "message": f"installed skill {registered_name!r} at {dest}",
            }
        )

    # --- internals --------------------------------------------------------

    def _resolve_destination(
        self,
        src: Path,
    ) -> tuple[Path | None, Path | None, str]:
        """Return `(dest_path, cleanup_dir, derived_name)`.

        `dest_path` is None when the file type isn't supported.
        `cleanup_dir` is the per-skill directory to remove on rollback
        (markdown skills only); None for `.py` / `.toml`.
        `derived_name` is the best-effort skill name from the filename.
        """
        suffix = src.suffix.lower()
        # `<name>.SKILL.md` lands as `<skills_dir>/<name>/SKILL.md`.
        if src.name.endswith(".SKILL.md"):
            stem = src.name[: -len(".SKILL.md")]
            if not stem:
                return None, None, ""
            target_dir = self._skills_dir / stem
            return target_dir / "SKILL.md", target_dir, stem
        if suffix == ".py":
            return self._skills_dir / src.name, None, src.stem
        if suffix == ".toml":
            return self._skills_dir / src.name, None, src.stem
        return None, None, ""

    @staticmethod
    def _cleanup(dest: Path, cleanup_dir: Path | None) -> None:
        try:
            if dest.exists():
                dest.unlink()
        except OSError:
            pass
        if cleanup_dir is not None:
            try:
                # Only remove the directory if it's empty; avoids
                # nuking unrelated content the user may have created.
                cleanup_dir.rmdir()
            except OSError:
                pass

    def _registered_name(self, dest: Path, fallback: str) -> str:
        """Look up the registered skill's `name` if we can; fall back to
        the filename-derived name when the loader didn't expose it."""
        try:
            registry = self._loader._registry  # noqa: SLF001
            for skill in registry.list():
                if getattr(skill, "source_path", "") == str(dest):
                    return skill.name
        except Exception:
            pass
        return fallback
