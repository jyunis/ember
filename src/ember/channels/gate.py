"""Inbound turn gate: per-conversation serialization + coalescing debounce.

Sits between a `Channel` and `Agent.handle_turn`. discord.py dispatches
every `on_message` in its own task, so two messages sent in quick
succession used to start two fully concurrent turns on the same
conversation. That is not merely wasteful — it is incorrect:

  - Both turns read history via `recent_messages()` and append their own
    reply, so each is blind to the other.
  - Both hand the *same* `conversation.claude_cli_session_id` to
    `claude --resume`, so two subprocesses append to one session jsonl
    while `_prune_session` rewrites it underneath them.
  - `bridge.active` is a single module-level handle; the first turn's
    `finally: clear_active()` fires while the second is still running,
    silently dropping the second turn's attachments.
  - The pre-turn short-circuits (`consume_pending_proposal`,
    `consume_pending_tool_confirmation`) are consume-once and race.

`TurnGate` fixes all of the above with one lock per conversation key,
and adds a short debounce window so the common "oops, and also…"
follow-up is folded into a single turn instead of queued behind one.

Semantics
---------
Every submitted message is appended to its conversation's pending list
before the submitter waits on that conversation's lock, so every
message is guaranteed to be picked up by exactly one turn — either its
own submitter's, or that of whichever submitter reaches the lock first.
Whoever holds the lock waits out the debounce window (extending it, up
to `max_debounce_s`, for as long as new messages keep arriving), drains
everything pending, and runs one merged turn. Submitters that find the
pending list already drained return without running anything.

A key's lock/pending/refcount entries are dropped once no submitter for
that key is in flight, so the maps stay bounded over long uptimes.

Interrupt mode
--------------
With `interrupt=True`, a message arriving while a turn is already
running cancels that turn instead of queueing behind it. The cancelled
turn's work is not replayed — it is discarded, and the fresh turn
rebuilds history from the store, which already contains the earlier
user message. Three things make that safe:

  - The turn runs as its own task so it can be cancelled at all, and
    cancellation is requested *before* the new submitter waits on the
    lock (otherwise it would block behind the very turn it wants to
    stop).
  - `CancelledError` is a `BaseException`, so it flows past the agent's
    `except Exception` handler with the `finally` intact: no assistant
    row is appended, the interactive-turn contextvar is reset, and the
    `bridge.active` handle is cleared.
  - A turn cancelled *before* it persisted its user message would lose
    that message outright, so the gate re-queues the batch in that case
    (see `ember.context.interruption`).
  - Rebuilt history is the whole story only for providers that take the
    full messages array. claude CLI takes just this turn's text and
    reconstructs the rest from its own session jsonl — which the reaped
    subprocess typically died before writing. So that provider prompts
    with every trailing user message in the store, not only the newest
    (`ClaudeCliProvider._extract_user_prompt`). Without that, the
    interrupted message was persisted, rebuilt into history, and then
    silently dropped on the way to the model.

What interrupting cannot undo is a side effect that already happened —
a sent email, a written memory, a placed order. `on_interrupt` exists
so the channel can say so out loud.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace

from ember.channels.base import ChannelMessage, MessageHandler
from ember.context.interruption import TurnState, current_turn_state

_log = logging.getLogger("ember.channels.gate")

# Called with the conversation key when a running turn is cancelled, so
# the channel can tell the user that a partially-completed turn was
# dropped (its side effects, if any, already happened).
InterruptNotifier = Callable[[str], Awaitable[None]]

# How long to hold a turn open for follow-up messages before starting it.
DEFAULT_DEBOUNCE_S: float = 1.5
# Hard ceiling on the debounce, so someone typing continuously can't
# defer their own turn indefinitely.
DEFAULT_MAX_DEBOUNCE_S: float = 6.0


def merge_messages(batch: list[ChannelMessage]) -> ChannelMessage:
    """Fold a coalesced batch into one `ChannelMessage`.

    Text is joined with blank lines in arrival order; attachments
    concatenate in the same order. Channel-native identity fields
    (`message_id`, `user_id`) come from the newest message — nothing
    downstream of the gate reads `message_id`, and the channel adapter
    has already deduped on it by this point.
    """
    if len(batch) == 1:
        return batch[0]
    texts = [m.text for m in batch if m.text]
    attachments = tuple(a for m in batch for a in m.attachments)
    return replace(batch[-1], text="\n\n".join(texts), attachments=attachments)


@dataclass
class _RunningTurn:
    """A turn currently executing, and enough state to cancel it safely."""

    task: asyncio.Task
    state: TurnState
    batch: list[ChannelMessage]
    # Set only by `_request_interrupt`, so a `CancelledError` arriving
    # for any other reason (process shutdown) is re-raised rather than
    # mistaken for an interrupt.
    interrupted: bool = field(default=False)


class TurnGate:
    """Serializes and coalesces inbound messages per conversation."""

    def __init__(
        self,
        handler: MessageHandler,
        *,
        debounce_s: float = DEFAULT_DEBOUNCE_S,
        max_debounce_s: float = DEFAULT_MAX_DEBOUNCE_S,
        interrupt: bool = False,
        on_interrupt: InterruptNotifier | None = None,
        serialize_conversations: bool = False,
    ) -> None:
        self._handler = handler
        self._debounce_s = debounce_s
        self._max_debounce_s = max_debounce_s
        self._interrupt = interrupt
        self._on_interrupt = on_interrupt
        # SIRI-TLC-1: with `serialize_conversations`, at most one handler
        # invocation runs at any instant across ALL conversation keys —
        # `bridge/active.py` is a module-global handle that two parallel
        # turns would race. The lock is taken inside the turn task (not
        # by the submitter), so a queued turn stays cancellable by a
        # same-key interrupt while it waits. Per-key debounce/coalesce/
        # interrupt semantics are unchanged.
        self._turn_lock: asyncio.Lock | None = asyncio.Lock() if serialize_conversations else None
        self._locks: dict[str, asyncio.Lock] = {}
        self._pending: dict[str, list[ChannelMessage]] = {}
        self._inflight: dict[str, int] = {}
        self._running: dict[str, _RunningTurn] = {}

    async def submit(self, msg: ChannelMessage) -> None:
        """Entry point handed to `Channel.start` in place of `handle_turn`."""
        key = msg.conversation_key
        self._pending.setdefault(key, []).append(msg)
        self._inflight[key] = self._inflight.get(key, 0) + 1
        # Cancel before queueing for the lock — waiting first would block
        # behind the very turn we intend to stop.
        if self._interrupt:
            self._request_interrupt(key)
        lock = self._locks.setdefault(key, asyncio.Lock())
        try:
            async with lock:
                batch = await self._drain_after_debounce(key)
                if not batch:
                    # Another submitter's turn already took our message.
                    return
                if len(batch) > 1:
                    _log.info(
                        "coalesced %d messages into one turn for conversation %s",
                        len(batch),
                        key,
                    )
                await self._run_turn(key, batch)
        finally:
            self._release(key)

    def _request_interrupt(self, key: str) -> None:
        """Cancel the turn running for `key`, if any."""
        running = self._running.get(key)
        if running is None or running.task.done():
            return
        running.interrupted = True
        running.task.cancel()
        _log.info("interrupting in-flight turn for conversation %s", key)

    async def _run_turn(self, key: str, batch: list[ChannelMessage]) -> None:
        """Run one turn as a cancellable task, tracked for interruption."""
        state = TurnState()
        # The task inherits a copy of the current context, so the shared
        # mutable `TurnState` must be installed before it is created.
        token = current_turn_state.set(state)
        try:
            task = asyncio.create_task(self._invoke(merge_messages(batch)))
        finally:
            current_turn_state.reset(token)

        running = _RunningTurn(task=task, state=state, batch=batch)
        self._running[key] = running
        try:
            await task
        except asyncio.CancelledError:
            if not running.interrupted:
                raise  # shutdown, not an interrupt — propagate
            await self._on_turn_interrupted(key, running)
        finally:
            if self._running.get(key) is running:
                del self._running[key]

    async def _invoke(self, msg: ChannelMessage) -> None:
        """Run the handler, under the cross-conversation lock if one is
        configured. Cancellation while waiting on the lock never enters
        the `async with`, so a cancelled waiter cannot leak the lock."""
        if self._turn_lock is None:
            await self._handler(msg)
            return
        async with self._turn_lock:
            await self._handler(msg)

    async def _on_turn_interrupted(self, key: str, running: _RunningTurn) -> None:
        if not running.state.user_message_persisted:
            # The turn died before its user message reached the store, so
            # nothing downstream will ever see it. Put it back at the
            # front of the queue for the turn that displaced it.
            self._pending.setdefault(key, [])[0:0] = running.batch
            _log.info(
                "interrupted turn for %s had not persisted; re-queued %d message(s)",
                key,
                len(running.batch),
            )
        if self._on_interrupt is not None:
            try:
                await self._on_interrupt(key)
            except Exception:
                _log.exception("on_interrupt notifier failed for %s", key)

    async def _drain_after_debounce(self, key: str) -> list[ChannelMessage]:
        """Hold the turn open briefly, then take everything pending.

        The window extends each time it expires with new arrivals since
        the last check, so a burst of short messages lands in one turn.
        `max_debounce_s` bounds the total wait.
        """
        waited = 0.0
        while self._debounce_s > 0 and self._pending.get(key):
            step = min(self._debounce_s, self._max_debounce_s - waited)
            if step <= 0:
                break
            before = len(self._pending[key])
            await asyncio.sleep(step)
            waited += step
            if len(self._pending.get(key, ())) == before:
                break
        return self._pending.pop(key, [])

    def _release(self, key: str) -> None:
        remaining = self._inflight.get(key, 0) - 1
        if remaining > 0:
            self._inflight[key] = remaining
            return
        # No submitter left for this key, so nothing can be pending:
        # every message is appended before its submitter waits, and an
        # interrupt only re-queues while the interrupting submitter is
        # still in flight. Warn rather than silently drop if that ever
        # stops holding — a lost message is a bug worth seeing.
        orphaned = self._pending.pop(key, [])
        if orphaned:
            _log.warning(
                "dropping %d pending message(s) for %s with no submitter in flight",
                len(orphaned),
                key,
            )
        self._inflight.pop(key, None)
        self._locks.pop(key, None)
        self._running.pop(key, None)
