"""In-memory `SkillRegistry` + `SkillSource` (FC-5, TLC-12).

Internal `dict[str, Skill]` keyed by `name`. `replace`/`remove`
fire registered change callbacks once per call so the
scheduler service can drive `refresh()` from a watcher tick.

`list_schedules()` is order-stable (TLC-12 spirit): skills sorted by
name, schedules within a skill sorted by `(function, name)`. The
scheduler service relies on this for deterministic refresh diffing.
Declarative skills naturally yield no schedules (TLC-13) — the filter
uses `getattr(skill, "scheduled_runs", ())`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from ember.scheduler.base import ScheduledFunction
from ember.skills.base import Skill

_log = logging.getLogger("ember.skills.registry")


class InMemorySkillRegistry:
    """In-process registry. Single instance per process (TLC-11 spirit)."""

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}
        self._callbacks: list[Callable[[], None]] = []

    # --- SkillRegistry surface --------------------------------------------

    def get(self, name: str) -> Skill:
        return self._skills[name]

    def list(self) -> list[Skill]:
        return [self._skills[n] for n in sorted(self._skills)]

    def names(self) -> list[str]:
        return sorted(self._skills)

    def replace(self, skill: Skill) -> None:
        """Add or replace by `skill.name`. Fires change callbacks.

        FC-5: in-flight uses of a prior version finish on the prior
        version. With Python's reference semantics, holding a reference
        to the old `Callable` is enough — replacing the dict entry does
        not invalidate any closure already in flight.
        """
        self._skills[skill.name] = skill
        self._fire_callbacks()

    def remove(self, name: str) -> None:
        """Remove by name. No-op if absent. Fires change callbacks."""
        existed = name in self._skills
        self._skills.pop(name, None)
        if existed:
            self._fire_callbacks()

    # --- SkillSource surface ----------------------------------------------

    def list_schedules(self) -> list[ScheduledFunction]:
        """One `ScheduledFunction` per `(skill, ScheduleSpec)`.

        Skills sorted by name; specs within a skill sorted by
        `(function, name)`. Declarative skills carry no schedules
        (TLC-13) — `getattr(..., "scheduled_runs", ())` filters them
        naturally.
        """
        out: list[ScheduledFunction] = []
        for name in sorted(self._skills):
            skill = self._skills[name]
            scheduled_runs = getattr(skill, "scheduled_runs", ())
            if not scheduled_runs:
                continue
            functions = getattr(skill, "functions", {}) or {}
            sorted_specs = sorted(
                scheduled_runs,
                key=lambda s: (s.function, s.name),
            )
            for spec in sorted_specs:
                fn = functions.get(spec.function)
                if fn is None:
                    # Defensive: the loader validates this; surface
                    # diagnostically rather than crashing the scheduler.
                    _log.warning(
                        "skill %s schedule %s references unknown fn %s; skipping",
                        skill.name,
                        spec.name,
                        spec.function,
                    )
                    continue
                out.append(ScheduledFunction(skill=skill.name, spec=spec, fn=fn))
        return out

    def on_change(self, callback: Callable[[], None]) -> None:
        self._callbacks.append(callback)

    # --- internal ---------------------------------------------------------

    def _fire_callbacks(self) -> None:
        for cb in list(self._callbacks):
            try:
                cb()
            except Exception:
                _log.exception("on_change callback raised; continuing")
