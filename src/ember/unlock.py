"""Per-conversation unlock windows (doc #60, TLC-31 / UoW-36 / FC-37).

A conversation is locked by default. A tool in the locked set (settings
`locked_tools`) does not run in a locked conversation; the first attempt
produces a 🔒 prompt asking for a TOTP code, and a valid code unlocks
that conversation for `ttl_seconds`. State is in-memory on purpose
(Decision #3 / seam 6): a process restart re-locks everything, which is
the fail-closed direction, and no migration is needed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


class UnlockStore:
    """In-memory `conversation_key → unlocked_until`."""

    def __init__(self, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._ttl = timedelta(seconds=ttl_seconds)
        self._until: dict[str, datetime] = {}

    @property
    def ttl(self) -> timedelta:
        return self._ttl

    def ttl_label(self) -> str:
        """`4h`, `90m`, `45s` — for prompts."""
        secs = int(self._ttl.total_seconds())
        if secs % 3600 == 0:
            return f"{secs // 3600}h"
        if secs % 60 == 0:
            return f"{secs // 60}m"
        return f"{secs}s"

    def unlock(self, key: str, *, now: datetime | None = None) -> datetime:
        """Open (or extend) the window; returns its end."""
        now = now or datetime.now(timezone.utc)
        until = now + self._ttl
        self._until[key] = until
        return until

    def lock(self, key: str) -> bool:
        """Close the window. True if one was open."""
        return self._until.pop(key, None) is not None

    def unlocked_until(self, key: str, *, now: datetime | None = None) -> datetime | None:
        until = self._until.get(key)
        if until is None:
            return None
        now = now or datetime.now(timezone.utc)
        if until <= now:
            self._until.pop(key, None)
            return None
        return until

    def is_unlocked(self, key: str | None, *, now: datetime | None = None) -> bool:
        if key is None:
            return False
        return self.unlocked_until(key, now=now) is not None
