"""`/skill [list|set|clear]` slash command (FC-12, UoW-9).

- `list` (default): one row per loaded skill:
    `name | description first line | tools count | [active]?`
  Works in any context.
- `set target:<name>`: marks `<name>` active on this conversation
  (validates registry membership). Conversation-only.
- `clear`: clears the active skill on this conversation.
  Conversation-only.

Unknown skill on `set` → user-facing error. No DB mutation.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ember.commands.base import CommandContext, CommandOption

if TYPE_CHECKING:
    from ember.skills.base import SkillRegistry
    from ember.store.repo import Store

_log = logging.getLogger("ember.commands.skill")


_NEEDS_THREAD = "Run `/skill` in a conversation thread."


def _first_line(text: str | None) -> str:
    if not text:
        return ""
    return text.splitlines()[0]


class SkillCommand:
    """List/set/clear the active skill on a conversation."""

    name: str = "skill"
    description: str = "List loaded skills, or set/clear the active skill on this thread."
    options: list[CommandOption] = [
        CommandOption(
            name="action",
            description="list | set | clear",
            default="list",
            choices=["list", "set", "clear"],
        ),
        CommandOption(
            name="target",
            description="skill name (for `set`)",
            required=False,
            default="",
            autocomplete=True,
        ),
    ]

    def __init__(self, *, skill_registry: "SkillRegistry", store: "Store") -> None:
        self._registry = skill_registry
        self._store = store

    async def autocomplete(self, ctx: CommandContext, option: str, current: str) -> list[str]:
        """Complete `target` against the loaded skill names."""
        if option != "target":
            return []
        cur = current.strip().lower()
        return [n for n in self._registry.names() if cur in n.lower()][:25]

    async def run(self, ctx: CommandContext) -> str:
        action = (ctx.args.get("action") or "list").strip()
        target = (ctx.args.get("target") or "").strip()

        if action == "list":
            return await self._render_list(ctx)
        if action == "set":
            return await self._set(ctx, target)
        if action == "clear":
            return await self._clear(ctx)
        return "Unknown action. Use `list`, `set`, or `clear`."

    # --- list -------------------------------------------------------------

    async def _render_list(self, ctx: CommandContext) -> str:
        skills = list(self._registry.list())
        if not skills:
            return "**Skills**\n_No skills loaded._"

        active_name: str | None = None
        if ctx.conversation_key is not None:
            try:
                conv = await self._store.get_or_create_conversation("discord", ctx.conversation_key)
                active_name = conv.active_skill
            except Exception:
                _log.debug("failed to look up active skill for /skill list", exc_info=True)

        lines = ["**Skills**"]
        for skill in skills:
            tools = getattr(skill, "tools", ()) or ()
            tool_count = len(tools)
            desc_line = _first_line(getattr(skill, "description", "") or "")
            marker = " [active]" if skill.name == active_name else ""
            lines.append(f"- `{skill.name}` | {desc_line} | {tool_count} tools{marker}")
        return "\n".join(lines)

    # --- set --------------------------------------------------------------

    async def _set(self, ctx: CommandContext, target: str) -> str:
        if ctx.conversation_key is None:
            return _NEEDS_THREAD
        if not target:
            return "Usage: `/skill action:set target:<skill_name>`"
        try:
            skill = self._registry.get(target)
        except KeyError:
            return f"❓ unknown skill: `{target}`"

        conv = await self._store.get_or_create_conversation("discord", ctx.conversation_key)
        await self._store.set_active_skill(conv.id, skill.name)

        tools = getattr(skill, "tools", ()) or ()
        first = _first_line(getattr(skill, "system_prompt", None))
        prompt_line = f"\n_System prompt:_ {first}" if first else ""
        return f"✅ active skill set to `{skill.name}`. {len(tools)} tools available.{prompt_line}"

    # --- clear ------------------------------------------------------------

    async def _clear(self, ctx: CommandContext) -> str:
        if ctx.conversation_key is None:
            return _NEEDS_THREAD
        conv = await self._store.get_or_create_conversation("discord", ctx.conversation_key)
        await self._store.set_active_skill(conv.id, None)
        return "✅ active skill cleared"
