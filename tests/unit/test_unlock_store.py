"""UnlockStore (doc #60 FC-37): per-conversation TTL windows, in-memory."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ember.unlock import UnlockStore

T0 = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


class TestUnlockStore:
    def test_locked_by_default(self) -> None:
        s = UnlockStore(60)
        assert not s.is_unlocked("k", now=T0)
        assert s.unlocked_until("k", now=T0) is None
        assert not s.is_unlocked(None)

    def test_unlock_then_expire(self) -> None:
        s = UnlockStore(60)
        until = s.unlock("k", now=T0)
        assert until == T0 + timedelta(seconds=60)
        assert s.is_unlocked("k", now=T0 + timedelta(seconds=59))
        assert not s.is_unlocked("k", now=T0 + timedelta(seconds=60))
        # Expiry evicts.
        assert s.unlocked_until("k", now=T0 + timedelta(seconds=61)) is None

    def test_lock_closes_window(self) -> None:
        s = UnlockStore(60)
        s.unlock("k", now=T0)
        assert s.lock("k") is True
        assert not s.is_unlocked("k", now=T0)
        assert s.lock("k") is False

    def test_windows_are_per_key(self) -> None:
        s = UnlockStore(60)
        s.unlock("a", now=T0)
        assert s.is_unlocked("a", now=T0)
        assert not s.is_unlocked("b", now=T0)

    def test_ttl_label(self) -> None:
        assert UnlockStore(14400).ttl_label() == "4h"
        assert UnlockStore(900).ttl_label() == "15m"
        assert UnlockStore(45).ttl_label() == "45s"

    def test_rejects_nonpositive_ttl(self) -> None:
        with pytest.raises(ValueError):
            UnlockStore(0)
