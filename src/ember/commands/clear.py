"""`/clear` — soft-clear the current conversation.

Marks every active message `compacted=1`, deletes summaries rows, nulls
the resumable claude_cli session, and clears any pending profile
proposal. Pins survive (architecture doc § Conversation store: "pinned
... survives trim/compact").

Wraps `Store.clear_conversation` (FC-7); the store does the work.
"""

from __future__ import annotations

from ember.commands.base import CommandContext, CommandOption

_OUT_OF_THREAD = "ℹ️ `/clear` must be invoked in a conversation thread."


class ClearCommand:
    name: str = "clear"
    description: str = "Soft-clear this conversation. Pins survive."
    options: list[CommandOption] = []

    async def run(self, ctx: CommandContext) -> str:
        if ctx.conversation_key is None:
            return _OUT_OF_THREAD

        conv = await ctx.store.get_or_create_conversation("discord", ctx.conversation_key)
        result = await ctx.store.clear_conversation(conv.id)
        session_str = "session reset" if result.session_reset else "no session"
        return (
            f"🧹 cleared {result.messages_marked} messages, "
            f"{result.summaries_deleted} summaries; {session_str}"
        )
