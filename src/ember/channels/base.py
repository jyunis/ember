"""Channel adapter interface (minimal, v0).

The architecture doc plans a richer abstraction in v1 (multiple
channels, conversation-key resolution, etc.). v0 needs only enough
shape to keep the agent loop channel-agnostic (TLC-14) and to give
the tester a clean Protocol to write a FakeChannel against.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final, Literal, Protocol, runtime_checkable

SendKind = Literal["reply", "status", "final"]

# --- Liveness timing (v16, FC-32) ------------------------------------------
# Module constants, not config (doc #57: "constants are fine until proven
# otherwise"). The agent's activity manager reads them at call time, so
# tests can shrink them without re-importing consumers.
#
# A Discord turn shows the single activity status message only once it has
# run this long (TLC-27: short turns show typing but never an activity
# message).
ACTIVITY_DELAY_S: Final[float] = 20.0
# Once posted, the activity message is refreshed (edited in place) on this
# cadence so the elapsed counter keeps moving between observer events.
ACTIVITY_EDIT_INTERVAL_S: Final[float] = 5.0
# Native typing indicators expire ~10s after a trigger; re-trigger on this
# cadence to keep the indicator lit for the whole turn (TLC-27: refreshed
# at ≤10s intervals).
TYPING_REFRESH_S: Final[float] = 8.0


@dataclass(frozen=True, slots=True)
class InboundAttachment:
    """A user-uploaded file the channel adapter has already saved to disk.

    `path` is an absolute path under `Settings.data_dir` (the discord
    adapter writes under `data_dir/uploads/...`). `content_type` is the
    MIME the channel reported; empty string when the channel didn't
    send one. `size` is the on-disk byte count.
    """

    filename: str
    path: str
    content_type: str
    size: int


@dataclass(frozen=True, slots=True)
class ChannelMessage:
    """Inbound message from a channel, normalized.

    `conversation_key` is the channel-native thread/topic identifier
    (Discord forum thread ID). `message_id` is the channel's native
    message ID — used for inbound deduplication (TLC-10).
    """

    channel: str
    conversation_key: str
    user_id: str
    text: str
    message_id: str
    attachments: tuple[InboundAttachment, ...] = ()


MessageHandler = Callable[[ChannelMessage], Awaitable[None]]


@runtime_checkable
class Channel(Protocol):
    """Every channel adapter implements this surface."""

    name: str  # "discord", "telegram", ...

    async def start(self, on_message: MessageHandler) -> None:
        """Connect and begin receiving. Authorizes inbound senders
        (TLC-1) before invoking `on_message`."""
        ...

    async def stop(self) -> None: ...

    async def send(
        self,
        conversation_key: str,
        content: str,
        *,
        kind: SendKind = "reply",
    ) -> list[str]:
        """Post `content` to the conversation. Returns the channel-native
        message ids created (a long reply may be split into several). The
        agent loop maps these to the assistant `messages` row so a later
        reaction resolves to its target (UoW-20 / FC-20). Callers that don't
        need the ids may ignore them; implementations that have no native id
        (test fakes, transient surfaces) return `[]`."""
        ...

    async def send_attachments(
        self,
        conversation_key: str,
        paths: list[str],
    ) -> None:
        """Post one or more local file paths as channel attachments.

        Default no-op so test fakes can ignore it; the Discord adapter
        overrides to upload via `discord.File`. Tools surface attachment
        paths in `ToolResult.metadata["attachments"]`; the agent loop
        forwards them through this method right after the tool returns.
        """
        ...

    async def set_typing(self, conversation_key: str, on: bool = True) -> None: ...

    # --- Liveness surface (v16, FC-32) --------------------------------
    # Optional, best-effort methods with no-op defaults: adapters with no
    # presence/status affordance (SiriChannel, test stubs) inherit these
    # and stay correct. All liveness I/O is best-effort by contract
    # (TLC-27) — callers wrap every invocation and a failure here must
    # never affect the turn.

    async def typing_started(self, conversation_key: str) -> None:
        """Trigger the channel's native typing indicator for the
        conversation. Native indicators are short-lived (Discord's lasts
        ~10s), so the caller re-invokes this every `TYPING_REFRESH_S` to
        keep it lit. No-op default."""
        return None

    async def typing_stopped(self, conversation_key: str) -> None:
        """Optional counterpart to `typing_started` for adapters whose
        indicator doesn't expire on its own. Discord's does (and clears
        on the next message), so the agent simply stops refreshing —
        this exists for surface symmetry. No-op default."""
        return None

    async def post_status(self, conversation_key: str, text: str) -> str | None:
        """Post the turn's single activity status message (UoW-31).
        Returns the channel-native message id so the caller can edit and
        delete it, or None when the surface has no such affordance (the
        caller then skips edits/deletes). No-op default returns None."""
        return None

    async def edit_status(self, conversation_key: str, native_id: str, text: str) -> None:
        """Replace the content of a status message previously returned by
        `post_status`. No-op default."""
        return None

    async def delete_status(self, conversation_key: str, native_id: str) -> None:
        """Delete a status message previously returned by `post_status`.
        The activity message is always terminal (TLC-27) — the agent
        calls this on every turn outcome. No-op default."""
        return None

    async def notify_scheduler(self, content: str) -> None:
        """Post `content` to the channel's configured scheduler-facing
        surface (Discord text channel; Telegram channel when added).

        Decoupled from `send` because the scheduler channel is not a
        conversation thread — `conversation_key` would have no meaning.
        Implementations resolve the destination from settings.
        """
        ...
