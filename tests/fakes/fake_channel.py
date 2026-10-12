"""Programmable Channel stub for tests.

Captures all outbound messages and lets tests inject inbound ones.
Implements the `Channel` protocol from `ember.channels.base`, including
the v16 FC-32 best-effort liveness surface (typing keepalive + activity
status post/edit/delete) with an ordered event log and controllable
failure injection.
"""

from __future__ import annotations

from dataclasses import dataclass

from ember.channels.base import Channel, ChannelMessage, MessageHandler, SendKind


@dataclass
class SentMessage:
    conversation_key: str
    content: str
    kind: SendKind
    message_id: str


@dataclass
class LivenessCall:
    """One recorded call on the FC-32 liveness surface.

    `ok=False` marks an attempt that raised via injected failure — the
    attempt is still logged so tests can count retries.
    """

    op: str  # "typing_started" | "typing_stopped" | "post_status" | "edit_status" | "delete_status"
    conversation_key: str
    text: str | None = None
    native_id: str | None = None
    ok: bool = True


class FakeChannel(Channel):
    """A no-network channel for tests.

    `inject(msg)` calls the registered handler. `sent` records every
    outbound `send` for assertions.

    Liveness surface (FC-32, doc #57): `liveness_calls` is the ordered
    log of every typing/status call — failed attempts included, marked
    `ok=False`. `live_statuses` mirrors the currently-visible activity
    messages (successful post adds, edit rewrites, delete removes), so
    the TLC-27 "no residue" postcondition is simply `live_statuses == {}`.

    Failure injection:
      - `fail_next("delete_status", times=1)` arms N injected failures
        for a liveness op (or for "send"); armed calls raise
        RuntimeError after logging the attempt.
      - `fail_sends_containing.append("marker")` makes the next `send`
        whose content contains the marker raise once (the matcher is
        consumed). Failed sends land in `failed_sends`, never in `sent`.
    """

    name: str = "fake"

    def __init__(self) -> None:
        self.sent: list[SentMessage] = []
        self.sent_attachments: list[tuple[str, list[str]]] = []
        self.scheduler_notifications: list[str] = []
        self.typing: dict[str, bool] = {}
        # --- FC-32 liveness recording / failure injection -----------------
        self.liveness_calls: list[LivenessCall] = []
        self.live_statuses: dict[str, str] = {}
        self.failed_sends: list[tuple[str, str]] = []
        self.fail_sends_containing: list[str] = []
        self._fail_counts: dict[str, int] = {}
        self._handler: MessageHandler | None = None
        self._stopped: bool = False
        self._next_message_id: int = 1

    async def start(self, on_message: MessageHandler) -> None:
        self._handler = on_message

    async def stop(self) -> None:
        self._stopped = True

    async def send(
        self,
        conversation_key: str,
        content: str,
        *,
        kind: SendKind = "reply",
    ) -> list[str]:
        # Injected send failures (by op-count or one-shot content
        # matcher) simulate a Discord API error on a normal message
        # post — UoW-32's "failed block post" path.
        if self._consume_failure("send") or self._consume_send_matcher(content):
            self.failed_sends.append((conversation_key, content))
            raise RuntimeError("FakeChannel: injected send failure")
        # Synthesize a stable native message id per send so tests can map a
        # reply and simulate a reaction against it (UoW-20 / FC-20).
        message_id = str(self._next_message_id)
        self._next_message_id += 1
        self.sent.append(SentMessage(conversation_key, content, kind, message_id))
        return [message_id]

    async def send_attachments(
        self,
        conversation_key: str,
        paths: list[str],
    ) -> None:
        self.sent_attachments.append((conversation_key, list(paths)))

    async def set_typing(self, conversation_key: str, on: bool = True) -> None:
        self.typing[conversation_key] = on

    async def notify_scheduler(self, content: str) -> None:
        self.scheduler_notifications.append(content)

    # --- FC-32 liveness surface -------------------------------------------

    async def typing_started(self, conversation_key: str) -> None:
        """Start (or refresh) the native typing indicator. Each call is
        logged; repeated calls are the keepalive refreshes."""
        ok = not self._consume_failure("typing_started")
        self.liveness_calls.append(LivenessCall("typing_started", conversation_key, ok=ok))
        if not ok:
            raise RuntimeError("FakeChannel: injected typing_started failure")

    async def typing_stopped(self, conversation_key: str) -> None:
        ok = not self._consume_failure("typing_stopped")
        self.liveness_calls.append(LivenessCall("typing_stopped", conversation_key, ok=ok))
        if not ok:
            raise RuntimeError("FakeChannel: injected typing_stopped failure")

    async def post_status(self, conversation_key: str, text: str) -> str | None:
        ok = not self._consume_failure("post_status")
        native_id = str(self._next_message_id) if ok else None
        self.liveness_calls.append(
            LivenessCall("post_status", conversation_key, text=text, native_id=native_id, ok=ok)
        )
        if not ok:
            raise RuntimeError("FakeChannel: injected post_status failure")
        self._next_message_id += 1
        self.live_statuses[native_id] = text
        return native_id

    async def edit_status(self, conversation_key: str, native_id: str, text: str) -> None:
        ok = not self._consume_failure("edit_status")
        self.liveness_calls.append(
            LivenessCall("edit_status", conversation_key, text=text, native_id=native_id, ok=ok)
        )
        if not ok:
            raise RuntimeError("FakeChannel: injected edit_status failure")
        if native_id not in self.live_statuses:
            raise KeyError(f"edit_status on unknown status message {native_id!r}")
        self.live_statuses[native_id] = text

    async def delete_status(self, conversation_key: str, native_id: str) -> None:
        ok = not self._consume_failure("delete_status")
        self.liveness_calls.append(
            LivenessCall("delete_status", conversation_key, native_id=native_id, ok=ok)
        )
        if not ok:
            raise RuntimeError("FakeChannel: injected delete_status failure")
        if native_id not in self.live_statuses:
            raise KeyError(f"delete_status on unknown status message {native_id!r}")
        del self.live_statuses[native_id]

    # --- Liveness assertion helpers ---------------------------------------

    def liveness_ops(self) -> list[str]:
        """Ordered op names of every recorded liveness call."""
        return [c.op for c in self.liveness_calls]

    def status_posts(self) -> list[LivenessCall]:
        """Successful activity-message posts, in order."""
        return [c for c in self.liveness_calls if c.op == "post_status" and c.ok]

    def status_edits(self) -> list[LivenessCall]:
        """Successful activity-message edits, in order."""
        return [c for c in self.liveness_calls if c.op == "edit_status" and c.ok]

    def status_deletes(self) -> list[LivenessCall]:
        """Successful activity-message deletes, in order."""
        return [c for c in self.liveness_calls if c.op == "delete_status" and c.ok]

    # --- Failure injection -------------------------------------------------

    def fail_next(self, op: str, *, times: int = 1) -> None:
        """Arm `times` injected failures for a liveness op or "send"."""
        self._fail_counts[op] = self._fail_counts.get(op, 0) + times

    def _consume_failure(self, op: str) -> bool:
        remaining = self._fail_counts.get(op, 0)
        if remaining <= 0:
            return False
        self._fail_counts[op] = remaining - 1
        return True

    def _consume_send_matcher(self, content: str) -> bool:
        for marker in self.fail_sends_containing:
            if marker in content:
                self.fail_sends_containing.remove(marker)
                return True
        return False

    # --- Test driver ------------------------------------------------------

    async def inject(self, msg: ChannelMessage) -> None:
        """Simulate an inbound message; calls the handler the agent
        registered via `start`."""
        if self._handler is None:
            raise RuntimeError("FakeChannel.inject called before start()")
        await self._handler(msg)
