"""`/help` — self-documenting catalog of registered commands.

Per FC-12: never hand-written; always derived from
`registry.list_commands()`. Two modes:

- `/help` (no args) → commands grouped into scannable categories, one
  line each. Optimized for "what can I even do here?" discovery.
- `/help command:<name>` → full detail for one command: synthesized
  usage line + every option with its choices / autocomplete / required
  state. The `command` option is itself autocompleted.

The grouping taxonomy lives here (not on each command) so adding a
command needs no help-side change — anything uncategorized falls into a
trailing "Other" group so nothing is ever hidden.
"""

from __future__ import annotations

from ember.commands.base import CommandContext, CommandOption, SlashCommand

# Ordered category → member command names. Names not listed anywhere are
# collected under a trailing "Other" group.
_CATEGORIES: list[tuple[str, list[str]]] = [
    ("💬 Conversation & context", ["clear", "trim", "compact", "summarize", "context", "pin"]),
    ("🧠 Model & memory", ["model", "memory_revert"]),
    ("🛠️ Skills & schedules", ["skill", "schedule", "runs", "run"]),
    ("🔐 Accounts", ["schwab-reauth"]),
    ("ℹ️ Meta", ["help"]),
]


def _usage(cmd: SlashCommand) -> str:
    """Synthesize a usage line from a command's declared options:
    `req:<…>` for required, `[opt:<…>]` for optional, `<a|b|c>` for fixed
    choices."""
    parts = [f"/{cmd.name}"]
    for opt in getattr(cmd, "options", []) or []:
        placeholder = "<" + "|".join(opt.choices) + ">" if opt.choices else "<…>"
        token = f"{opt.name}:{placeholder}"
        parts.append(token if opt.required else f"[{token}]")
    return " ".join(parts)


class HelpCommand:
    name: str = "help"
    description: str = "List all commands, or detail one with command:<name>."
    options: list[CommandOption] = [
        CommandOption(
            name="command",
            description="show full detail for a single command",
            required=False,
            default="",
            autocomplete=True,
        ),
    ]

    async def autocomplete(self, ctx: CommandContext, option: str, current: str) -> list[str]:
        if option != "command":
            return []
        cur = current.strip().lower().lstrip("/")
        names = [c.name for c in ctx.registry.list_commands()]
        return [n for n in names if cur in n.lower()][:25]

    async def run(self, ctx: CommandContext) -> str:
        commands = ctx.registry.list_commands()
        by_name = {c.name: c for c in commands}

        requested = (ctx.args.get("command") or "").strip().lstrip("/")
        if requested:
            cmd = by_name.get(requested)
            if cmd is None:
                known = ", ".join(f"`{c.name}`" for c in commands)
                return f"❓ no command `{requested}`. Known: {known}"
            return self._render_detail(cmd)

        return self._render_overview(commands, by_name)

    @staticmethod
    def _render_overview(commands: list[SlashCommand], by_name: dict[str, SlashCommand]) -> str:
        lines = [
            "**Ember commands** — type `/` in a thread to run one, "
            "or `/help command:<name>` for details.",
        ]
        shown: set[str] = set()
        for title, names in _CATEGORIES:
            members = [by_name[n] for n in names if n in by_name]
            if not members:
                continue
            lines.append("")
            lines.append(f"__{title}__")
            for cmd in members:
                shown.add(cmd.name)
                lines.append(f"  `/{cmd.name}` — {cmd.description}")
        leftovers = [c for c in commands if c.name not in shown]
        if leftovers:
            lines.append("")
            lines.append("__Other__")
            for cmd in leftovers:
                lines.append(f"  `/{cmd.name}` — {cmd.description}")
        return "\n".join(lines)

    @staticmethod
    def _render_detail(cmd: SlashCommand) -> str:
        lines = [f"**`/{cmd.name}`** — {cmd.description}", "", f"Usage: `{_usage(cmd)}`"]
        options = getattr(cmd, "options", []) or []
        if options:
            lines.append("")
            lines.append("Options:")
            for opt in options:
                bits: list[str] = []
                if opt.required:
                    bits.append("required")
                else:
                    bits.append(f"default `{opt.default}`" if opt.default else "optional")
                if opt.choices:
                    bits.append("one of: " + ", ".join(f"`{c}`" for c in opt.choices))
                elif opt.autocomplete:
                    bits.append("⌨️ autocompletes")
                lines.append(f"  • `{opt.name}` ({'; '.join(bits)}) — {opt.description}")
        return "\n".join(lines)
