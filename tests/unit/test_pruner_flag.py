"""Pruner gate (v15) — FC-31.

- Config `tool_pruner_enabled: bool = False` (pydantic Settings; the
  pruner ships OFF).
- `ClaudeCliProvider` constructed with pruning disabled must not invoke
  the pruner (`prune_session_jsonl`) anywhere in a completed turn.

The provider test uses the subprocess-faking pattern from
`tests/unit/test_claude_cli_prompt.py`; the pruner is intercepted by
monkeypatching `prune_session_jsonl` at both plausible lookup sites
(the defining module and the provider module's namespace) so the
assertion holds regardless of import style.
"""

from __future__ import annotations

from typing import Any

import pytest

from ember.config import load_settings
from ember.providers.base import Message
from ember.providers.claude_cli import ClaudeCliProvider

# --- Settings surface -------------------------------------------------------

REQUIRED_ENV = {
    "DISCORD_BOT_TOKEN": "secret-token-value",
    "DISCORD_GUILD_ID": "11111",
    "DISCORD_CONVERSATIONS_CHANNEL_ID": "22222",
    "DISCORD_SCHEDULER_CHANNEL_ID": "33333",
}


@pytest.fixture
def isolated_env(tmp_path, monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TOOL_PRUNER_ENABLED", raising=False)
    for k in REQUIRED_ENV:
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


class TestToolPrunerSetting:
    def test_defaults_false(self, isolated_env: pytest.MonkeyPatch) -> None:
        for k, v in REQUIRED_ENV.items():
            isolated_env.setenv(k, v)
        s = load_settings()
        assert s.tool_pruner_enabled is False

    def test_env_override_true(self, isolated_env: pytest.MonkeyPatch) -> None:
        for k, v in REQUIRED_ENV.items():
            isolated_env.setenv(k, v)
        isolated_env.setenv("TOOL_PRUNER_ENABLED", "true")
        s = load_settings()
        assert s.tool_pruner_enabled is True


# --- Provider skips the pruner when disabled --------------------------------


@pytest.fixture
def fake_subprocess(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Intercept create_subprocess_exec with a scripted result event so
    `complete()` finishes a full turn (with a session id — the state in
    which a pruner pass would otherwise have work to do)."""
    captured: list[list[str]] = []

    class _FakeStream:
        def __init__(self, lines: list[bytes]) -> None:
            self._lines = list(lines)

        def __aiter__(self) -> "_FakeStream":
            return self

        async def __anext__(self) -> bytes:
            if not self._lines:
                raise StopAsyncIteration
            return self._lines.pop(0)

        async def read(self) -> bytes:
            return b""

    class _FakeProc:
        def __init__(self) -> None:
            self.stdout = _FakeStream(
                [
                    b'{"type": "result", "is_error": false, "result": "ok", '
                    b'"session_id": "sess-prune", "usage": {"input_tokens": 1, '
                    b'"output_tokens": 1}}\n'
                ]
            )
            self.stderr = _FakeStream([])

        async def wait(self) -> int:
            return 0

    async def _fake_create(*args: Any, **kwargs: Any):  # noqa: ANN401, ARG001
        captured.append(list(args))
        return _FakeProc()

    import asyncio

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    return captured


@pytest.fixture
def prune_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """Record every `prune_session_jsonl` invocation, patched at both
    plausible lookup sites."""
    calls: list[tuple[Any, ...]] = []

    def _recording_prune(*args: Any, **kwargs: Any):  # noqa: ANN401
        calls.append((args, kwargs))
        raise AssertionError("prune_session_jsonl must not be invoked")

    import ember.context.tool_pruner as tool_pruner_mod

    monkeypatch.setattr(tool_pruner_mod, "prune_session_jsonl", _recording_prune)

    import ember.providers.claude_cli as claude_cli_mod

    if hasattr(claude_cli_mod, "prune_session_jsonl"):
        monkeypatch.setattr(claude_cli_mod, "prune_session_jsonl", _recording_prune)

    return calls


class TestPrunerDisabled:
    async def test_disabled_provider_never_invokes_pruner(
        self,
        fake_subprocess: list[list[str]],
        prune_calls: list[tuple[Any, ...]],
    ) -> None:
        provider = ClaudeCliProvider(tool_pruner_enabled=False)

        result = await provider.complete(
            system="",
            messages=[Message("user", "hello")],
            session_id="sess-prune",
        )

        # The turn itself completed normally …
        assert result.text == "ok"
        assert len(fake_subprocess) == 1
        # … and the pruner was never touched.
        assert prune_calls == []

    async def test_disabled_provider_skips_pruner_on_fresh_session_too(
        self,
        fake_subprocess: list[list[str]],
        prune_calls: list[tuple[Any, ...]],
    ) -> None:
        """No `session_id` (fresh session) — still no pruner call."""
        provider = ClaudeCliProvider(tool_pruner_enabled=False)

        await provider.complete(system="", messages=[Message("user", "hello")])

        assert prune_calls == []
