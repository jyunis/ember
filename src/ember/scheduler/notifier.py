"""Concrete `RunNotifier` (UoW-5, TLC-7).

Posts run-failure messages (and other operational notifications) to
the configured scheduler-channel surface on the wired `Channel`.
The recorder is the source of truth (TLC-8); notifications are best-
effort messaging on top of that audit trail.
"""

from __future__ import annotations

import logging

from ember.channels.base import Channel

_log = logging.getLogger("ember.scheduler.notifier")


class ChannelRunNotifier:
    """`RunNotifier` that forwards to `Channel.notify_scheduler`."""

    def __init__(self, channel: Channel) -> None:
        self._channel = channel

    async def notify(self, message: str) -> None:
        try:
            await self._channel.notify_scheduler(message)
        except Exception:
            # Notification failure must not break the run wrapper
            # (TLC-8: the row in `scheduled_runs` is the source of truth).
            _log.exception("notify_scheduler failed; message dropped")
