"""Skill interfaces (FC-5).

v6 shipped imperative skills only. v7 adds declarative manifests:

**Imperative** — Python module under `/data/skills/*.py` exporting
module-level attributes (name, description, tools, system_prompt,
preferred_model, scheduled_runs) plus async coroutines named in
`scheduled_runs`. Sync fns rejected at load.

**Declarative** — TOML manifest at `/data/skills/*.toml` carrying the
same fields *minus* `scheduled_runs` (TLC-13: only imperative skills
can be scheduled) and *plus* `suggest_when` (list of trigger phrases
the agent may use to softly suggest a skill switch — never to
auto-switch, per decision #1).

Both kinds live in the same registry. `Skill` is the union type;
`list_schedules()` filters to imperative.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ember.scheduler.base import ScheduledFunction, ScheduleSpec


@dataclass(frozen=True, slots=True)
class ImperativeSkill:
    """A loaded imperative skill (FC-5).

    `functions` maps every name referenced by `scheduled_runs` (and any
    additional public coroutines) to its bound coroutine. The loader
    validates that every `ScheduleSpec.function` resolves to a
    coroutine here.

    `source_path` is the absolute path of the .py module on disk; the
    loader uses it to drive hot-reload via the file watcher.
    """

    name: str
    description: str
    tools: tuple[str, ...] = ()
    system_prompt: str | None = None
    preferred_model: str | None = None
    scheduled_runs: tuple[ScheduleSpec, ...] = ()
    functions: dict[str, Callable[..., Awaitable[Any]]] = field(default_factory=dict)
    source_path: str | None = None

    def schedules(self) -> list[ScheduledFunction]:
        """Bind each `ScheduleSpec` to its named coroutine.

        Raises KeyError if a spec references a function not present in
        `functions`. The loader is expected to have validated this at
        load time, so this is a defensive check only.
        """
        return [
            ScheduledFunction(skill=self.name, spec=spec, fn=self.functions[spec.function])
            for spec in self.scheduled_runs
        ]


@dataclass(frozen=True, slots=True)
class DeclarativeSkill:
    """A loaded declarative skill manifest (FC-5, v7).

    Pure data — no Python callables. TLC-13 enforced at load: a
    declarative manifest cannot carry `scheduled_runs`.

    `suggest_when` is a list of trigger phrases the agent may use to
    softly suggest a skill switch ("this looks like meal-planning —
    /skill mealie_planner?"). Per decision #1, the agent never
    auto-switches on a match.
    """

    name: str
    description: str
    tools: tuple[str, ...] = ()
    system_prompt: str | None = None
    preferred_model: str | None = None
    suggest_when: tuple[str, ...] = ()
    source_path: str | None = None


# Union of skill flavors held by the registry.
Skill = ImperativeSkill | DeclarativeSkill


@runtime_checkable
class SkillRegistry(Protocol):
    """In-memory registry of loaded skills (imperative + declarative).

    Mutation is driven either by the loader's file watcher (FC-5
    hot-reload) or by the one-shot boot-time directory load. The
    registry is also a `SkillSource` (it implements
    `list_schedules`/`on_change`).
    """

    def get(self, name: str) -> Skill: ...

    def list(self) -> list[Skill]: ...

    def replace(self, skill: Skill) -> None:
        """Add or replace by `skill.name`. Fires registered change
        callbacks once per change. In-flight uses of a prior version
        keep running on the prior version (FC-5 hot-reload semantics)."""
        ...

    def remove(self, name: str) -> None:
        """Remove by name. No-op if absent. Fires change callbacks."""
        ...

    def names(self) -> list[str]: ...


@runtime_checkable
class SkillLoader(Protocol):
    """Loads imperative skills from a directory.

    `load_all` is the boot-time pass; it walks the directory and
    populates the registry. `start_watching` begins a background watch
    that reloads changed modules and updates the registry; `stop` ends
    the watch.
    """

    async def load_all(self) -> None: ...

    async def start_watching(self) -> None: ...

    async def stop(self) -> None: ...
