"""Active-conversation handle for bridge → channel callbacks.

The MCP bridge serves tool calls in its own asyncio tasks (FastMCP
spawns one per request), so contextvars set by the agent loop don't
propagate. To let bridge-routed tools surface side effects (image
attachments, file uploads) into the originating conversation, the agent
sets this module-level handle before each `claude_cli.complete()` and
clears it after. The bridge invoker reads it.

Concurrency caveat: a single global handle assumes one claude_cli turn
runs at a time. For two parallel turns in different conversations the
handle would race. Since the Siri-channel work (doc #55, SIRI-TLC-1)
that assumption is *enforced*, not assumed: `TurnGate` is wired with
`serialize_conversations=True`, so at most one interactive turn runs
process-wide across all channels and conversation keys. If the bridge
ever grows per-turn conversation context (doc #55 Deferrals), the
serialization and this global can both go.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ember.channels.base import Channel

_log = logging.getLogger("ember.bridge.active")

_channel: "Channel | None" = None
_conversation_key: str | None = None


def set_active(channel: "Channel", conversation_key: str) -> None:
    global _channel, _conversation_key
    _channel = channel
    _conversation_key = conversation_key


def active_conversation_key() -> str | None:
    """The conversation driving the current claude_cli turn, if any."""
    return _conversation_key


def clear_active() -> None:
    global _channel, _conversation_key
    _channel = None
    _conversation_key = None


async def forward_attachments(paths: list[str]) -> None:
    """Best-effort: post attachments to whichever conversation is
    currently driving claude_cli. Silent no-op outside an active turn."""
    if not paths:
        return
    if _channel is None or _conversation_key is None:
        _log.debug(
            "forward_attachments: no active conversation; dropping %d path(s)",
            len(paths),
        )
        return
    try:
        await _channel.send_attachments(_conversation_key, paths)
    except Exception:
        _log.exception("forward_attachments: send_attachments failed")
