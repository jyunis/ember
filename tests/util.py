"""Shared test helpers."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable

# TC-1: interactive user turns are persisted (and sent) with a leading
# local-timestamp line, e.g. "[Fri 2026-07-11 18:52]\n<text>" — optionally
# carrying a gap note after a long silence ("… · 5 days after previous
# message"). Tests that assert on the user's *typed* text strip that prefix
# with `unstamp()`.
_STAMP_RE = re.compile(r"^\[\w{3} \d{4}-\d\d-\d\d \d\d:\d\d(?: · [^\]]+)?\]$")


def unstamp(content: str) -> str:
    """Drop the TC-1 timestamp prefix line from a persisted user turn.

    Returns `content` unchanged when it carries no stamp — assistant/tool
    rows, or messages inserted directly into the store (which never pass
    through the interactive turn path that stamps them). If the stamp format
    drifts so the first line no longer matches, the prefix is left in place
    and the caller's equality assertion fails loudly rather than silently
    passing.
    """
    head, sep, rest = content.partition("\n")
    return rest if sep and _STAMP_RE.match(head) else content


async def wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = 2.0,
    interval: float = 0.005,
    message: str = "condition",
) -> None:
    """Poll `predicate` until truthy, failing after `timeout` seconds.

    Event-driven tests for the v16 liveness machinery (doc #57) hold a
    turn open and wait for the keepalive/activity task to record calls
    on the FakeChannel; this is the single polling primitive they share
    so timeouts fail loudly with a named condition instead of hanging.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {message}")
