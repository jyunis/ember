"""`/pin <text>` — add a pinned-context entry to the conversation.

Pins survive `/trim`, `/compact`, and `/clear` (architecture doc §
Conversation store). Stored via `Store.add_pin(conv.id, text)`.

Reply: `📌 pinned: <text-truncated>`.
"""

from __future__ import annotations

from ember.commands.base import CommandContext, CommandOption

_OUT_OF_THREAD = "ℹ️ `/pin` must be invoked in a conversation thread."
_USAGE = "Usage: `/pin text:<content>`"
_PIN_PREVIEW_MAX = 200


def _preview(text: str, limit: int = _PIN_PREVIEW_MAX) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


class PinCommand:
    name: str = "pin"
    description: str = "Pin a piece of context to this conversation (survives trim/compact/clear)."
    options: list[CommandOption] = [
        CommandOption(
            name="text",
            description="The text to pin.",
            default="",
        ),
    ]

    async def run(self, ctx: CommandContext) -> str:
        if ctx.conversation_key is None:
            return _OUT_OF_THREAD

        text = (ctx.args.get("text") or "").strip()
        if not text:
            return _USAGE

        conv = await ctx.store.get_or_create_conversation("discord", ctx.conversation_key)
        await ctx.store.add_pin(conv.id, text)
        return f"📌 pinned: {_preview(text)}"
