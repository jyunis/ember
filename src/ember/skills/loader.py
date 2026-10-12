"""Imperative skill loader (FC-5, TLC-13).

Walks a directory of `.py` modules; importlib-loads each; validates
the FC-5 contract; registers each result in the wired
`InMemorySkillRegistry`. `start_watching` runs a background watcher
that re-imports changed files and refreshes the registry (debounced
per `Settings.skills_watch_debounce_ms`).

Validation rules (FC-5):
- Module must have `name: str`.
- `description` is required.
- `tools` (optional) becomes `tuple[str, ...]`.
- `scheduled_runs` (optional) — each entry shaped like `ScheduleSpec`.
  Every entry's `function` must resolve to an `async def` on the
  module. Sync functions in a `scheduled_runs` slot are rejected.
  Unresolved function names are rejected.

TLC-13: only imperative skills can be scheduled. A "declarative-style"
module that carries `scheduled_runs` but no `async def` cannot resolve
its functions; that's caught by the unresolved-function path. Either
way: the loader raises `SkillLoadError`.

Hot-reload (FC-5): in-flight uses of a previous version run to
completion on that version. We achieve this naturally — replacing the
registry entry leaves any outstanding `Callable` reference live.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import inspect
import logging
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ember.errors import SkillLoadError
from ember.scheduler.base import ScheduleSpec
from ember.skills.base import ImperativeSkill
from ember.skills.manifest import load_declarative
from ember.skills.markdown import load_markdown
from ember.skills.registry import InMemorySkillRegistry

_log = logging.getLogger("ember.skills.loader")


def _module_name_for(path: Path) -> str:
    """Derive a unique module name for `importlib.util.spec_from_file_location`.

    We namespace under `ember._skills` so multiple loaders / repeated
    reloads don't collide with each other or with the rest of the
    package tree.
    """
    return f"ember._skills.{path.stem}"


def _coerce_specs(raw: Any, source_path: Path) -> tuple[ScheduleSpec, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise SkillLoadError(
            f"{source_path}: scheduled_runs must be a list[dict], got {type(raw).__name__}"
        )
    out: list[ScheduleSpec] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise SkillLoadError(
                f"{source_path}: scheduled_runs entry must be a dict, got {type(entry).__name__}"
            )
        try:
            out.append(ScheduleSpec(**entry))
        except TypeError as e:
            raise SkillLoadError(f"{source_path}: malformed scheduled_runs entry: {e}") from e
    return tuple(out)


def _validate_and_build(module: Any, source_path: Path) -> ImperativeSkill:
    """Apply FC-5 / TLC-13 validation. Returns a built `ImperativeSkill`
    on success; raises `SkillLoadError` otherwise.
    """
    name = getattr(module, "name", None)
    if not isinstance(name, str) or not name:
        raise SkillLoadError(f"{source_path}: module is missing required attribute `name: str`")
    description = getattr(module, "description", None)
    if not isinstance(description, str):
        raise SkillLoadError(
            f"{source_path}: module is missing required attribute `description: str`"
        )

    raw_tools = getattr(module, "tools", ()) or ()
    if not isinstance(raw_tools, (list, tuple)):
        raise SkillLoadError(
            f"{source_path}: tools must be list[str], got {type(raw_tools).__name__}"
        )
    tools = tuple(str(t) for t in raw_tools)

    system_prompt = getattr(module, "system_prompt", None)
    if system_prompt is not None and not isinstance(system_prompt, str):
        raise SkillLoadError(f"{source_path}: system_prompt must be str | None")

    preferred_model = getattr(module, "preferred_model", None)
    if preferred_model is not None and not isinstance(preferred_model, str):
        raise SkillLoadError(f"{source_path}: preferred_model must be str | None")

    raw_specs = getattr(module, "scheduled_runs", None)
    specs = _coerce_specs(raw_specs, source_path)

    # Resolve every function reference and confirm it's a coroutine fn.
    functions: dict[str, Callable[..., Awaitable[Any]]] = {}
    for spec in specs:
        fn = getattr(module, spec.function, None)
        if fn is None:
            raise SkillLoadError(
                f"{source_path}: scheduled_runs references unknown function "
                f"{spec.function!r} (skill {name!r})"
            )
        if not inspect.iscoroutinefunction(fn):
            raise SkillLoadError(
                f"{source_path}: scheduled_runs function {spec.function!r} on "
                f"skill {name!r} must be async (FC-5)"
            )
        functions[spec.function] = fn

    return ImperativeSkill(
        name=name,
        description=description,
        tools=tools,
        system_prompt=system_prompt,
        preferred_model=preferred_model,
        scheduled_runs=specs,
        functions=functions,
        source_path=str(source_path),
    )


def _purge_bytecode_cache(path: Path) -> None:
    """Best-effort: remove `__pycache__` entries for `path`.

    Python's `SourceFileLoader` keys `.pyc` validity on (source mtime
    in *seconds*, source size). Same-second rewrites of equal-size
    source files therefore hit the cached bytecode and miss the new
    source. We sidestep that entirely by deleting the cached bytecode
    before each load, since hot-reload performance is not on the
    critical path.
    """
    try:
        cache_dir = path.parent / "__pycache__"
        if not cache_dir.exists():
            return
        stem = path.stem
        for entry in cache_dir.iterdir():
            if entry.name.startswith(f"{stem}.") and entry.suffix in (".pyc",):
                try:
                    entry.unlink()
                except OSError:
                    pass
    except OSError:
        pass


def _import_module(path: Path) -> Any:
    """Spec-load `path` as a fresh module. Raises any underlying ImportError."""
    _purge_bytecode_cache(path)
    mod_name = _module_name_for(path)
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise SkillLoadError(f"{path}: importlib could not produce a spec")
    module = importlib.util.module_from_spec(spec)
    # Insert into sys.modules under our namespaced name so relative
    # imports inside the skill work; remove on failure to keep the
    # namespace clean.
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(mod_name, None)
        raise
    return module


class DirectorySkillLoader:
    """Loads imperative skills from `directory` into `registry`."""

    # Default debounce window for the file watcher. The wired settings
    # value (`skills_watch_debounce_ms`) overrides at construction time
    # via the optional `debounce_s` kwarg; tests rely on the 100ms
    # default firing within the 4s wait window.
    _DEFAULT_DEBOUNCE_S = 0.1

    def __init__(
        self,
        directory: Path,
        registry: InMemorySkillRegistry,
        *,
        debounce_s: float | None = None,
    ) -> None:
        self._directory = Path(directory)
        self._registry = registry
        self._debounce_s = debounce_s if debounce_s is not None else self._DEFAULT_DEBOUNCE_S
        self._watch_task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event = asyncio.Event()
        # Snapshot of (mtime_ns, size, content_hash) per file, used by
        # the polling watcher to detect changes. mtime_ns has
        # nanosecond resolution but consecutive rapid rewrites of
        # equal-size files can land in the same nanosecond bucket on
        # tmpfs / fast SSDs — the content hash catches those.
        self._stat_snapshot: dict[Path, tuple[int, int, str]] = {}

    # --- public surface ---------------------------------------------------

    async def load_all(self) -> None:
        """Boot-time pass: import every `.py`/`.toml` skill in the directory."""
        for path in self._enumerate_skill_files():
            await self._reload_one(path)
            sig = self._signature(path)
            if sig is not None:
                self._stat_snapshot[path] = sig

    async def start_watching(self) -> None:
        """Begin background watch of the skills directory."""
        if self._watch_task is not None and not self._watch_task.done():
            return
        self._stop_event = asyncio.Event()
        self._watch_task = asyncio.create_task(self._watch_loop(), name="skill-watcher")

    async def stop(self) -> None:
        """Stop the background watcher (if running)."""
        self._stop_event.set()
        task = self._watch_task
        self._watch_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    # --- internal ---------------------------------------------------------

    def _enumerate_skill_files(self) -> list[Path]:
        if not self._directory.exists():
            return []
        files: list[Path] = []
        for pattern in ("*.py", "*.toml"):
            for p in self._directory.glob(pattern):
                if p.is_file() and not p.name.startswith("_"):
                    files.append(p)
        # FC-14: also pick up `<skills_dir>/<name>/SKILL.md` (one level deep).
        # Skip directories starting with `_` (e.g. `_drafts/`) to mirror
        # the existing convention for top-level skill files.
        for p in self._directory.glob("*/SKILL.md"):
            if p.is_file() and not p.parent.name.startswith("_"):
                files.append(p)
        return sorted(files)

    async def _reload_one(self, path: Path) -> None:
        """Re-import a single skill file and refresh its registry entry."""
        if path.suffix == ".toml":
            skill = load_declarative(path)
            self._registry.replace(skill)
            return

        if path.name == "SKILL.md":
            skill = load_markdown(path)
            self._registry.replace(skill)
            return

        try:
            module = _import_module(path)
        except SkillLoadError:
            raise
        except Exception as e:
            raise SkillLoadError(f"{path}: import failed: {e}") from e

        skill = _validate_and_build(module, path)
        self._registry.replace(skill)

    def _signature(self, path: Path) -> tuple[int, int, str] | None:
        """Cheap change-detection key for `path`.

        Returns `(mtime_ns, size, sha256_hex)` or None if the file
        cannot be read. The hash is what saves us when two rapid
        same-size rewrites land in the same mtime nanosecond bucket
        (observed on tmpfs and on fast SSDs).
        """
        try:
            data = path.read_bytes()
            st = path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size, hashlib.sha256(data).hexdigest())

    def _name_for_path(self, path: Path) -> str | None:
        """Best-effort: figure out which registered skill name belongs to
        `path` (used on file-deletion). We look up by `source_path`."""
        for skill in self._registry.list():
            if skill.source_path == str(path):
                return skill.name
        # Fallback: assume name == file stem.
        return path.stem

    async def _watch_loop(self) -> None:
        """Background watcher.

        Implementation is a periodic mtime poll (debounce-driven). We
        avoid `watchfiles.awatch` here because the skills directory is
        small and the test suite is sensitive to inotify startup
        latency vs. immediate file writes — polling is deterministic.
        """
        poll_s = max(0.05, self._debounce_s)
        while not self._stop_event.is_set():
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                return
            except Exception:
                _log.exception("skill watcher poll crashed")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=poll_s)
                # stop_event set → exit
                return
            except asyncio.TimeoutError:
                continue

    async def _poll_once(self) -> None:
        """One pass: detect new/changed/removed skill files."""
        current: dict[Path, tuple[int, int, str]] = {}
        for path in self._enumerate_skill_files():
            sig = self._signature(path)
            if sig is None:
                continue
            current[path] = sig
        previous = self._stat_snapshot

        # Modified or new files.
        for path, sig in current.items():
            old = previous.get(path)
            if old is None or old != sig:
                try:
                    await self._reload_one(path)
                except SkillLoadError:
                    _log.exception("skill reload failed for %s", path)
                except Exception:
                    _log.exception("unexpected reload failure for %s", path)

        # Removed files.
        for path in list(previous.keys()):
            if path not in current:
                name = self._name_for_path(path)
                if name is not None:
                    try:
                        self._registry.remove(name)
                    except Exception:
                        _log.exception("remove(%s) failed during watch", name)

        self._stat_snapshot = current
