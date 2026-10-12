"""Cancelling a claude_cli turn must reap the subprocess.

The turn gate cancels `provider.complete()` when the user interrupts.
Cancellation on its own only unwinds the `async for` over stdout — the
`claude` process keeps running, keeps burning subscription quota, and
keeps writing to the session jsonl the *next* turn is about to resume
and prune. So the provider terminates it explicitly.

These drive a **real** subprocess rather than the fake-proc pattern used
by the sibling claude_cli tests: the whole point is what happens to an
actual OS process, which a fake cannot demonstrate.
"""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path

import pytest

from ember.providers import claude_cli as claude_cli_mod
from ember.providers.base import Message
from ember.providers.claude_cli import ClaudeCliProvider


def _script(tmp_path: Path, body: str) -> str:
    """Write an executable stand-in for the `claude` binary."""
    path = tmp_path / "fake-claude"
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return str(path)


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list:
    """Capture the Process objects the provider launches."""
    procs: list = []
    real = asyncio.create_subprocess_exec

    async def _spy(*args, **kwargs):
        proc = await real(*args, **kwargs)
        procs.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spy)
    return procs


async def _cancel_mid_turn(provider: ClaudeCliProvider, spawned: list):
    """Start a turn, wait for the subprocess, cancel, return the Process."""
    task = asyncio.create_task(
        provider.complete(system="s", messages=[Message(role="user", content="hi")])
    )
    for _ in range(200):  # ~2s
        if spawned:
            break
        await asyncio.sleep(0.01)
    assert spawned, "subprocess never launched"
    await asyncio.sleep(0.05)  # let it get into its sleep

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    return spawned[0]


def _alive(pid: int) -> bool:
    """True if pid exists and is not a reaped zombie."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class TestInterruptReapsSubprocess:
    async def test_cancelled_turn_terminates_the_process(
        self, tmp_path: Path, spawned: list
    ) -> None:
        # Emits nothing and hangs — stdout iteration blocks, exactly like
        # a real turn that's still thinking.
        provider = ClaudeCliProvider(binary=_script(tmp_path, "sleep 30"))
        proc = await _cancel_mid_turn(provider, spawned)

        assert proc.returncode is not None, "subprocess left running after cancel"
        assert not _alive(proc.pid)
        assert proc.returncode == -signal.SIGTERM

    async def test_child_processes_die_with_the_parent(self, tmp_path: Path, spawned: list) -> None:
        """Real claude spawns MCP servers and subagents. Signalling only
        the direct child would orphan them: still running, still holding
        our stdout pipe open, still doing work nobody is waiting for."""
        child_pidfile = tmp_path / "child.pid"
        provider = ClaudeCliProvider(
            binary=_script(tmp_path, f"sleep 30 & echo $! > {child_pidfile}\nwait"),
        )
        task = asyncio.create_task(
            provider.complete(system="s", messages=[Message(role="user", content="hi")])
        )
        for _ in range(200):  # wait for the grandchild to record its pid
            if child_pidfile.exists() and child_pidfile.read_text().strip():
                break
            await asyncio.sleep(0.01)
        child_pid = int(child_pidfile.read_text().strip())
        assert _alive(child_pid)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        for _ in range(200):  # SIGTERM delivery is not instantaneous
            if not _alive(child_pid):
                break
            await asyncio.sleep(0.01)
        assert not _alive(child_pid), "grandchild survived the interrupt"

    async def test_process_ignoring_sigterm_is_killed(
        self, tmp_path: Path, spawned: list, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A wedged CLI must not survive the interrupt indefinitely."""
        monkeypatch.setattr(claude_cli_mod, "_REAP_TIMEOUT_S", 0.2)
        provider = ClaudeCliProvider(
            binary=_script(tmp_path, "trap '' TERM\nsleep 30"),
        )
        proc = await _cancel_mid_turn(provider, spawned)

        assert proc.returncode == -signal.SIGKILL
        assert not _alive(proc.pid)

    async def test_uncancelled_turn_is_unaffected(self, tmp_path: Path, spawned: list) -> None:
        """The reap path must not disturb a turn that completes normally."""
        result_line = (
            '{"type":"result","is_error":false,"result":"ok","session_id":"s1",'
            '"usage":{"input_tokens":1,"output_tokens":1}}'
        )
        provider = ClaudeCliProvider(binary=_script(tmp_path, f"echo '{result_line}'"))
        out = await provider.complete(system="s", messages=[Message(role="user", content="hi")])

        assert out.text == "ok"
        assert out.session_id == "s1"
        assert spawned[0].returncode == 0
