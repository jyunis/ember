"""`/schedule [list|enable|disable]` slash command (FC-12, FC-6).

- `list` (default): one row per loaded job:
    `skill:run_name | cron | next_fire_time | enabled?`
- `disable <skill>:<run_name>`: pause + persist.
- `enable  <skill>:<run_name>`: resume + persist.

Unknown target → user-facing message (UnknownScheduleError → string).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ember.commands.base import CommandContext, CommandOption
from ember.errors import UnknownScheduleError

if TYPE_CHECKING:
    from ember.scheduler.base import SchedulerService

_log = logging.getLogger("ember.commands.schedule")


def _fmt_dt(dt) -> str:
    if dt is None:
        return "-"
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _split_target(target: str) -> tuple[str, str] | None:
    """Parse `<skill>:<run_name>`; return None if malformed."""
    if not target or ":" not in target:
        return None
    skill, _, run_name = target.partition(":")
    skill = skill.strip()
    run_name = run_name.strip()
    if not skill or not run_name:
        return None
    return skill, run_name


class ScheduleCommand:
    """List/enable/disable scheduled jobs."""

    name: str = "schedule"
    description: str = "List or toggle scheduled skill jobs."
    options: list[CommandOption] = [
        CommandOption(
            name="action",
            description="list | enable | disable",
            default="list",
            choices=["list", "enable", "disable"],
        ),
        CommandOption(
            name="target",
            description="skill:run_name (for enable/disable)",
            required=False,
            default="",
            autocomplete=True,
        ),
    ]

    def __init__(self, *, scheduler: "SchedulerService") -> None:
        self._scheduler = scheduler

    async def autocomplete(self, ctx: CommandContext, option: str, current: str) -> list[str]:
        """Complete `target` against the loaded jobs (`skill:run_name`)."""
        if option != "target":
            return []
        cur = current.strip().lower()
        targets = sorted(f"{j.skill}:{j.run_name}" for j in self._scheduler.list_jobs())
        return [t for t in targets if cur in t.lower()][:25]

    async def run(self, ctx: CommandContext) -> str:
        action = (ctx.args.get("action") or "list").strip()
        target = (ctx.args.get("target") or "").strip()

        if action == "list":
            return self._render_list()
        if action in ("enable", "disable"):
            parsed = _split_target(target)
            if parsed is None:
                return (
                    "Usage: `/schedule action:enable target:<skill>:<run_name>` "
                    "(or `action:disable`)"
                )
            skill, run_name = parsed
            enabled = action == "enable"
            try:
                await self._scheduler.set_enabled(skill, run_name, enabled)
            except UnknownScheduleError as e:
                return f"❓ unknown schedule: {e}"
            except Exception as e:  # pragma: no cover — defensive
                _log.exception("set_enabled failed")
                return f"❌ {action} failed: {type(e).__name__}: {e}"
            verb = "enabled" if enabled else "disabled"
            return f"✅ {verb} `{skill}:{run_name}`"
        return "Unknown action. Use `list`, `enable`, or `disable`."

    def _render_list(self) -> str:
        jobs = self._scheduler.list_jobs()
        if not jobs:
            return "No scheduled jobs loaded."
        lines = [
            "**Scheduled jobs**",
            "`skill:run_name | cron | next_fire | enabled`",
        ]
        for j in jobs:
            tag = "✅" if j.enabled else "⏸"
            lines.append(
                f"- {tag} `{j.skill}:{j.run_name}` | `{j.cron}` | "
                f"{_fmt_dt(j.next_fire_time)} | {'on' if j.enabled else 'off'}"
            )
        return "\n".join(lines)
