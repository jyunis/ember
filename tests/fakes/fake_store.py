"""Programmable Store stub for tests (FC-17 / TLC-16 cost attribution).

Records every state-mutating call so tests can assert that the
sub-agent loop attributed costs to the parent conversation. We don't
mirror the full SQL behavior — just enough of the public Store API for
the FC-17 surface to be exercised.

Implementer flexibility: the spec says "all Provider calls inside the
sub-agent are recorded against the parent conversation's usage table
with `subagent_skill=<name>` set on the message row". The implementer
may shape this as:

  (a) A new keyword arg `subagent_skill` on `append_message`.
  (b) A new method `append_subagent_message(...)` etc.
  (c) Persisting via metadata on `StoredMessage`.

Rather than pin one shape, this fake captures every call to anything
named `append_*` plus generic kwargs. Tests query
`fake_store.subagent_messages` (list of (kwargs)) — we extract these
from any append-style call that carried a `subagent_skill` kwarg.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ember.providers.base import ToolCall, Usage


@dataclass
class AppendCall:
    """One captured `append_message`-style invocation."""

    conversation_id: int
    role: str
    content: str
    kwargs: dict[str, Any] = field(default_factory=dict)


class FakeStore:
    """In-memory `Store` stub for FC-17 unit tests.

    Only implements the methods FC-17 / TLC-16 may touch. If the
    implementer pulls in a method we haven't faked, add it and re-run.
    """

    def __init__(self) -> None:
        self.append_calls: list[AppendCall] = []
        self._next_id = 1
        # Optional: any other recorded state-mutating call (so tests can
        # see that some attribution write happened even if the
        # implementer chose a method shape we didn't anticipate).
        self.misc_calls: list[tuple[str, dict[str, Any]]] = []

    @property
    def subagent_messages(self) -> list[AppendCall]:
        """All append calls that carried a non-None `subagent_skill` kwarg.

        The TLC-16 §6 contract is "subagent_skill=<name> set on the
        message row" — we just check the kwarg landed on the call.
        """
        return [c for c in self.append_calls if c.kwargs.get("subagent_skill") is not None]

    async def append_message(
        self,
        conversation_id: int,
        role: str,
        content: str,
        *,
        provider_route: str | None = None,
        usage: Usage | None = None,
        tool_calls: list[ToolCall] | None = None,
        failover: bool = False,
        # FC-17 / TLC-16 §6: implementer may add subagent_skill +
        # parent_message_id as new kwargs. Accept anything via **extra.
        **extra: Any,
    ) -> int:
        self.append_calls.append(
            AppendCall(
                conversation_id=conversation_id,
                role=role,
                content=content,
                kwargs={
                    "provider_route": provider_route,
                    "usage": usage,
                    "tool_calls": tool_calls,
                    "failover": failover,
                    **extra,
                },
            )
        )
        rid = self._next_id
        self._next_id += 1
        return rid

    async def touch_conversation(self, conversation_id: int) -> None:
        self.misc_calls.append(("touch_conversation", {"conversation_id": conversation_id}))

    # --- Convenience for tests --------------------------------------------

    def reset(self) -> None:
        self.append_calls.clear()
        self.misc_calls.clear()
