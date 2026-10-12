"""Per-turn state shared between the turn gate and the agent loop.

The gate can cancel a running turn when a new message arrives
(`TurnGate`, interrupt mode). Cancelling is only safe to treat as "the
message is already recorded" once the agent has persisted the user
message — `_handle_turn_inner` does several things first (the
proposal / tool-confirmation / schwab-reauth short-circuits, which can
themselves make LLM calls), and a turn cancelled inside that window
would drop the user's text on the floor entirely.

So the agent calls `mark_user_message_persisted()` the moment the row
lands, and the gate re-queues the batch when that never happened.

The contextvar carries a *mutable* `TurnState`, not a bare flag: a task
inherits a copy of the parent's context, so a plain `set()` inside the
turn would not be visible to the gate awaiting it. Mutating a shared
object is. Same reason `bridge.active` is a module-level handle.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class TurnState:
    """Handshake for one turn. Created by the gate, mutated by the agent."""

    user_message_persisted: bool = False


current_turn_state: ContextVar[TurnState | None] = ContextVar(
    "current_turn_state",
    default=None,
)


def mark_user_message_persisted() -> None:
    """Signal that this turn's user message is durably in the store.

    No-op outside a gated turn (scheduler runs, tests calling
    `handle_turn` directly), so callers never need to guard.
    """
    state = current_turn_state.get()
    if state is not None:
        state.user_message_persisted = True
