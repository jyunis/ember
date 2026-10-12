"""`/trim N` — drop the oldest N non-compacted messages.

Marks the oldest N active messages as `compacted=1` so prompt assembly
drops them. Bounded:
- positive integer required (else usage hint);
- if N exceeds the active count, all active messages are trimmed.

Reply: `🪒 trimmed K messages` where K is the actual count (clamped).
"""

from __future__ import annotations

from ember.commands.base import CommandContext, CommandOption

_OUT_OF_THREAD = "ℹ️ `/trim` must be invoked in a conversation thread."
_USAGE = "Usage: `/trim n:<positive integer>`"


class TrimCommand:
    name: str = "trim"
    description: str = "Drop the oldest N messages from this conversation's active context."
    options: list[CommandOption] = [
        CommandOption(
            name="n",
            description="How many oldest messages to trim (positive integer).",
            default="",
        ),
    ]

    async def run(self, ctx: CommandContext) -> str:
        if ctx.conversation_key is None:
            return _OUT_OF_THREAD

        raw = ctx.args.get("n")
        if raw is None or str(raw).strip() == "":
            return _USAGE
        try:
            n = int(str(raw).strip())
        except (TypeError, ValueError):
            return _USAGE
        if n <= 0:
            return _USAGE

        conv = await ctx.store.get_or_create_conversation("discord", ctx.conversation_key)
        marked = await ctx.store.trim_oldest_active(conv.id, n)
        return f"🪒 trimmed {marked} messages"
