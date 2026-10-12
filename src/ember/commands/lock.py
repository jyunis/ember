"""`/lock` — end this conversation's unlock window early (doc #60, UoW-36 c)."""

from __future__ import annotations

from ember.commands.base import CommandContext, CommandOption
from ember.unlock import UnlockStore

_OUT_OF_THREAD = "ℹ️ `/lock` must be invoked in a conversation thread."


class LockCommand:
    name: str = "lock"
    description: str = "Re-lock this conversation (ends the current unlock window)."
    options: list[CommandOption] = []

    def __init__(self, *, unlock_store: UnlockStore) -> None:
        self._unlock_store = unlock_store

    async def run(self, ctx: CommandContext) -> str:
        if ctx.conversation_key is None:
            return _OUT_OF_THREAD
        if self._unlock_store.lock(ctx.conversation_key):
            return "🔒 locked — the next protected read will ask for a code again."
        return "🔒 already locked."
