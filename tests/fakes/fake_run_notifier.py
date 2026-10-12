"""Programmable RunNotifier stub for tests (UoW-5 / TLC-7).

Records every notify call so tests can assert the runner posts
non-ok terminal states (and only those) to the scheduler channel.

Implements the `RunNotifier` Protocol from `ember.scheduler.base`.
"""

from __future__ import annotations


class FakeRunNotifier:
    """In-memory `RunNotifier` for tests.

    `messages` is a list of every payload passed to `notify`.
    """

    def __init__(self) -> None:
        self.messages: list[str] = []

    async def notify(self, message: str) -> None:
        self.messages.append(message)
