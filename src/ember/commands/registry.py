"""Slash command registry (FC-12).

Single registry per process. Channel adapters iterate `list_commands()`
at startup to register each command with the platform's slash-command
system. Dispatch catches exceptions and returns a user-facing string
rather than letting them escape (TLC-7 spirit).
"""

from __future__ import annotations

import logging

from ember.commands.base import CommandContext, SlashCommand

_log = logging.getLogger("ember.commands")

_UNKNOWN_COMMAND = "?  unknown command. /help for the list."


class CommandRegistry:
    def __init__(self) -> None:
        self._commands: dict[str, SlashCommand] = {}

    def register(self, command: SlashCommand) -> None:
        if command.name in self._commands:
            raise ValueError(f"command {command.name!r} is already registered")
        self._commands[command.name] = command

    def get(self, name: str) -> SlashCommand:
        return self._commands[name]

    def list_commands(self) -> list[SlashCommand]:
        return [self._commands[name] for name in sorted(self._commands)]

    async def dispatch(self, name: str, ctx: CommandContext) -> str:
        cmd = self._commands.get(name)
        if cmd is None:
            return _UNKNOWN_COMMAND
        try:
            return await cmd.run(ctx)
        except Exception as e:
            _log.exception("slash command %r failed", name)
            return f"❌ /{name} failed: {type(e).__name__}: {e}"
