"""`/run <skill> <function> [run_name]` slash command (FC-12, FC-6).

Per FC-6, manual triggers go through the same wrapped path as cron
fires. The command resolves the `(skill, function, run_name)` tuple
against the loaded `SkillRegistry` and forwards to
`SchedulerService.run_now`.

`run_name` is optional — if omitted and exactly one schedule on the
named function exists, that schedule's `run_name` is used. Multiple
matches require disambiguation.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol

from ember.commands.base import CommandContext, CommandOption
from ember.errors import UnknownScheduleError

if TYPE_CHECKING:
    from ember.scheduler.base import SchedulerService
    from ember.skills.base import ImperativeSkill

_log = logging.getLogger("ember.commands.run")


class _RegistryView(Protocol):
    """Minimal surface required from a `SkillRegistry`."""

    def get(self, name: str) -> "ImperativeSkill": ...


class RunCommand:
    """Manually fire a scheduled run."""

    name: str = "run"
    description: str = "Manually invoke a scheduled skill run (FC-6)."
    options: list[CommandOption] = [
        CommandOption(name="skill", description="skill name", required=True, autocomplete=True),
        CommandOption(
            name="function", description="function name", required=True, autocomplete=True
        ),
        CommandOption(
            name="run_name",
            description="run_name (omit when there is exactly one match)",
            required=False,
            default=None,
            autocomplete=True,
        ),
    ]

    def __init__(
        self,
        *,
        scheduler: "SchedulerService",
        skill_registry: _RegistryView,
    ) -> None:
        self._scheduler = scheduler
        self._registry = skill_registry

    async def autocomplete(self, ctx: CommandContext, option: str, current: str) -> list[str]:
        """Complete against the live job list, narrowing by options already
        chosen: `function` to the selected `skill`, and `run_name` to the
        selected `skill`+`function`."""
        cur = current.strip().lower()
        jobs = list(self._scheduler.list_jobs())
        skill = (ctx.args.get("skill") or "").strip()
        function = (ctx.args.get("function") or "").strip()
        if option == "skill":
            vals = {j.skill for j in jobs}
        elif option == "function":
            vals = {j.function for j in jobs if not skill or j.skill == skill}
        elif option == "run_name":
            vals = {
                j.run_name
                for j in jobs
                if (not skill or j.skill == skill) and (not function or j.function == function)
            }
        else:
            return []
        return [v for v in sorted(vals) if cur in v.lower()][:25]

    async def run(self, ctx: CommandContext) -> str:
        skill_name = (ctx.args.get("skill") or "").strip()
        function = (ctx.args.get("function") or "").strip()
        run_name = ctx.args.get("run_name")
        if isinstance(run_name, str):
            run_name = run_name.strip() or None

        if not skill_name or not function:
            return "Usage: `/run skill:<name> function:<fn> [run_name:<name>]`"

        try:
            skill = self._registry.get(skill_name)
        except KeyError:
            return f"❓ unknown skill `{skill_name}`. See `/schedule list`."

        # Find specs matching this function on the skill.
        matching = [s for s in skill.scheduled_runs if s.function == function]
        if not matching:
            return (
                f"❓ skill `{skill_name}` has no scheduled function `{function}`. "
                "See `/schedule list`."
            )

        if run_name is None:
            if len(matching) == 1:
                run_name = matching[0].name
            else:
                names = ", ".join(s.name for s in matching)
                return (
                    f"❓ ambiguous run_name for `{skill_name}:{function}` — "
                    f"specify one of: {names}. Multiple schedules use this function."
                )
        else:
            # Verify the run_name actually maps to this function.
            if not any(s.name == run_name for s in matching):
                return (
                    f"❓ unknown run_name `{run_name}` on `{skill_name}:{function}`. "
                    f"Try one of: {', '.join(s.name for s in matching)}."
                )

        try:
            await self._scheduler.run_now(skill_name, function, run_name)
        except UnknownScheduleError as e:
            return f"❓ unknown schedule: {e}"
        except Exception as e:  # pragma: no cover — dispatch handles, but be defensive
            _log.exception("run_now failed")
            return f"❌ run failed: {type(e).__name__}: {e}"

        return f"▶️ ran `{skill_name}:{run_name}` ({function})"


# Help mypy — the dispatcher only needs a Protocol-compatible target.
_ = Any  # silence "unused import" with TYPE_CHECKING
