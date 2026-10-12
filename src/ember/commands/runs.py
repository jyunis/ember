"""`/runs <skill>` slash command (FC-12, UoW-5).

Renders the most recent `scheduled_runs` rows for `<skill>` from the
`RunRecorder`. Default limit is 20; empty result yields a friendly
"no runs yet" message.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ember.commands.base import CommandContext, CommandOption

if TYPE_CHECKING:
    from ember.scheduler.base import RunRecorder, SchedulerService

_DEFAULT_LIMIT = 20


def _fmt_dt(dt) -> str:
    if dt is None:
        return "-"
    # ISO 8601, second precision is enough for /runs.
    return dt.strftime("%Y-%m-%d %H:%M:%S")


class RunsCommand:
    """Show recent scheduled runs for a skill."""

    name: str = "runs"
    description: str = "Show recent scheduled runs for a skill."
    options: list[CommandOption] = [
        CommandOption(name="skill", description="skill name", required=True, autocomplete=True),
    ]

    def __init__(
        self, *, recorder: "RunRecorder", scheduler: "SchedulerService | None" = None
    ) -> None:
        self._recorder = recorder
        # Only used to enumerate skill names for autocomplete. Optional so
        # existing call sites / tests that don't need completion still work.
        self._scheduler = scheduler

    async def autocomplete(self, ctx: CommandContext, option: str, current: str) -> list[str]:
        """Complete `skill` against skills that have scheduled jobs."""
        if option != "skill" or self._scheduler is None:
            return []
        cur = current.strip().lower()
        skills = sorted({j.skill for j in self._scheduler.list_jobs()})
        return [s for s in skills if cur in s.lower()][:25]

    async def run(self, ctx: CommandContext) -> str:
        skill = (ctx.args.get("skill") or "").strip()
        if not skill:
            return "Usage: `/runs skill:<name>`"

        rows = await self._recorder.list_runs(skill, limit=_DEFAULT_LIMIT)
        if not rows:
            return f"No runs yet for `{skill}`."

        lines = [f"**Recent runs for `{skill}`** (most recent first):"]
        for row in rows:
            ref = _fmt_dt(row.started_at if row.started_at else row.scheduled_for)
            line = f"- `{row.run_name}` [{row.status}] @ {ref}"
            if row.status == "error" and row.error_text:
                # one-line error preview
                first_line = (
                    row.error_text.strip().splitlines()[-1] if row.error_text.strip() else ""
                )
                line += f"  — {first_line[:80]}"
            lines.append(line)
        return "\n".join(lines)
