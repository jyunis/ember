"""`/compact` and `/summarize` — force a session rotation (v15, TLC-25).

Both commands invoke `SessionRotator.rotate(conv, reason="forced")`:
fresh claude session next turn, one cheap-tier handoff brief replacing
the conversation's summaries, all non-compacted messages folded behind
it. Forced rotation skips the auto thresholds (UoW-28) but still needs
at least 2 non-compacted messages; the other skip reasons
(cheap_tier_unavailable, nothing_to_rotate, provider_error, ...) apply
unchanged and are surfaced verbatim.

`/summarize` is a thin alias subclass — Discord forbids duplicate slash
command names, so two distinct command objects with shared logic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ember.commands.base import CommandContext, CommandOption

if TYPE_CHECKING:
    from ember.context.rotation import SessionRotator


_OUT_OF_THREAD = "ℹ️ `/compact` must be invoked in a conversation thread."


class CompactCommand:
    name: str = "compact"
    description: str = "Rotate this conversation: fresh session + handoff summary."
    options: list[CommandOption] = []

    def __init__(self, *, rotator: "SessionRotator") -> None:
        self._rotator = rotator

    async def run(self, ctx: CommandContext) -> str:
        if ctx.conversation_key is None:
            return _OUT_OF_THREAD

        conv = await ctx.store.get_or_create_conversation("discord", ctx.conversation_key)
        result = await self._rotator.rotate(conv, reason="forced")

        if not result.rotated:
            return f"ℹ️ skipped: {result.skipped_reason}"
        return "✅ rotated: next turn starts a fresh session with a handoff summary"


class SummarizeCommand(CompactCommand):
    """Alias — same behavior as `/compact`, separate slash-command name."""

    name: str = "summarize"
    description: str = "Rotate this conversation into a handoff summary (alias of /compact)."
