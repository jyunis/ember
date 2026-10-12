"""Interactive-turn contextvar.

Tools that must refuse to run from automated/scheduled paths read this
flag to decide whether to fail closed. The agent loop sets it True for
the duration of a user-driven turn (`handle_turn`) and clears it in
`finally`. Code paths spawned from the scheduler, health-check job, or
MCP bridge requests originating outside a user turn never see True.

Used by the Schwab brokerage tools (BR-TLC-5): scheduled or proactive
code paths must not be able to place or cancel orders, even via the
TLC-3 confirmation gate, because there is no "next user turn" to
consume the gate.
"""

from __future__ import annotations

from contextvars import ContextVar

in_interactive_turn: ContextVar[bool] = ContextVar(
    "in_interactive_turn",
    default=False,
)
