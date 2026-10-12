"""claude_cli arg construction (FC-2, v0.5 surface).

These tests target the *boundary* of the claude_cli provider: the
constructor's tool-gating arguments and the args list passed to
`asyncio.create_subprocess_exec`. We do NOT exercise subprocess flow
end-to-end — instead we monkeypatch `create_subprocess_exec` to
intercept the args, and we use a private helper (`_build_args`) where
that's a more direct test for the same boundary.

Per the brief: `_build_args` is a private method but is part of the
v0.5-relevant interface for tool gating + MCP config (the constructor
flags it consumes are public). Reading just the signature is allowed.
We exercise both:

  1. The public boundary (construction + intercepted subprocess args)
     — primary test, no internal-method usage.
  2. The `_build_args` direct-call form — secondary test, redundant
     with #1 but more focused / failure-localized.

If `_build_args` is renamed, dropped, or made non-callable in a
refactor, only the secondary tests need updating.
"""

from __future__ import annotations

from typing import Any

import pytest

from ember.providers.base import Message
from ember.providers.claude_cli import ClaudeCliProvider


# --- Subprocess interception fixture -------------------------------------


@pytest.fixture
def captured_args(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Replace asyncio.create_subprocess_exec with a stub that records
    the args list and returns a fake process whose stdout drains to a
    single result event so the provider's complete() can return.

    This lets us verify the args list at the public boundary without
    spawning `claude` or reading more of claude_cli.py than the brief
    allows.
    """
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
            # One minimal `result` event so the provider's loop sees a
            # successful turn. Shape mirrors what claude CLI emits in
            # stream-json mode (per provider docstring).
            result_event = (
                b'{"type": "result", "is_error": false, "result": "ok", '
                b'"session_id": "fake-sess", "usage": {"input_tokens": 1, '
                b'"output_tokens": 1}}\n'
            )
            self.stdout = _FakeStream([result_event])
            self.stderr = _FakeStream([])

        async def wait(self) -> int:
            return 0

    async def _fake_create(*args: Any, **kwargs: Any):  # noqa: ANN401, ARG001
        captured.append(list(args))
        return _FakeProc()

    import asyncio

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    return captured


# --- Public-boundary tests (preferred) -----------------------------------


class TestArgsViaSubprocessIntercept:
    """Exercise arg construction by intercepting create_subprocess_exec.

    These tests don't read claude_cli.py internals — the boundary is
    "what does the provider hand to the OS." That's what we assert.
    """

    async def test_v05_mode_emits_allowed_and_disallowed_and_mcp_config(
        self, captured_args: list[list[str]]
    ) -> None:
        provider = ClaudeCliProvider(
            allowed_tools="mcp__agent__*",
            disallowed_tools=["Bash", "Read"],
            mcp_config_paths=["/data/agent-mcp.json"],
        )
        await provider.complete(system="", messages=[Message("user", "hi")])

        assert len(captured_args) == 1
        argv = captured_args[0]

        # --allowed-tools is set with the single string value.
        assert "--allowed-tools" in argv
        i = argv.index("--allowed-tools")
        assert argv[i + 1] == "mcp__agent__*"

        # --disallowed-tools is comma-joined per the brief.
        assert "--disallowed-tools" in argv
        i = argv.index("--disallowed-tools")
        assert argv[i + 1] == "Bash,Read"

        # --mcp-config flag carries the config path.
        assert "--mcp-config" in argv
        i = argv.index("--mcp-config")
        assert argv[i + 1] == "/data/agent-mcp.json"

        # v0's blanket disable must NOT be present once allowed-tools
        # is set.
        assert "--tools" not in argv

    async def test_v0_mode_emits_blanket_disable_and_no_mcp(
        self, captured_args: list[list[str]]
    ) -> None:
        # Without v0.5 args, the provider must keep v0 behavior:
        # blanket-disable tools, no allowed-tools, no mcp-config.
        provider = ClaudeCliProvider()
        await provider.complete(system="", messages=[Message("user", "hi")])

        assert len(captured_args) == 1
        argv = captured_args[0]

        # v0 blanket disable.
        assert "--tools" in argv
        i = argv.index("--tools")
        assert argv[i + 1] == ""

        assert "--allowed-tools" not in argv
        assert "--disallowed-tools" not in argv
        assert "--mcp-config" not in argv

    async def test_multiple_mcp_configs_each_get_a_flag(
        self, captured_args: list[list[str]]
    ) -> None:
        # Spec ambiguity: the architecture doc shows a single
        # /data/agent-mcp.json. The provider constructor accepts
        # `mcp_config_paths: list[str]`. Multiple-path semantics are
        # not pinned in the contracts doc. We assert what the
        # constructor naming implies — each path becomes a flag.
        provider = ClaudeCliProvider(
            allowed_tools="mcp__agent__*",
            mcp_config_paths=["/data/a.json", "/data/b.json"],
        )
        await provider.complete(system="", messages=[Message("user", "hi")])

        argv = captured_args[0]
        # Count --mcp-config flags.
        assert argv.count("--mcp-config") == 2


# --- Direct `_build_args` tests (more focused failure messages) ----------


class TestBuildArgsDirect:
    """Same boundary, narrower failure mode. If `_build_args` changes
    shape, these break first; the subprocess-intercept tests above
    still cover the public contract."""

    def test_v05_args_present(self) -> None:
        p = ClaudeCliProvider(
            allowed_tools="mcp__agent__*",
            disallowed_tools=["Bash", "Read"],
            mcp_config_paths=["/data/agent-mcp.json"],
        )
        argv = p._build_args(
            "user prompt",
            system="",
            model=None,
            session_id=None,
        )

        assert "--allowed-tools" in argv
        assert argv[argv.index("--allowed-tools") + 1] == "mcp__agent__*"
        assert "--disallowed-tools" in argv
        assert argv[argv.index("--disallowed-tools") + 1] == "Bash,Read"
        assert "--mcp-config" in argv
        assert argv[argv.index("--mcp-config") + 1] == "/data/agent-mcp.json"
        assert "--tools" not in argv
        # User prompt is the final positional arg.
        assert argv[-1] == "user prompt"

    def test_v0_args_present(self) -> None:
        p = ClaudeCliProvider()
        argv = p._build_args(
            "user prompt",
            system="",
            model=None,
            session_id=None,
        )

        assert "--tools" in argv
        assert argv[argv.index("--tools") + 1] == ""
        assert "--allowed-tools" not in argv
        assert "--mcp-config" not in argv
        assert argv[-1] == "user prompt"

    def test_disallowed_tools_only_without_allowed_falls_back_to_v0(self) -> None:
        # Spec ambiguity: the brief flags v0.5 mode by EITHER
        # `allowed_tools` OR `mcp_config_paths` being set. What if
        # only `disallowed_tools` is set? The contracts doc doesn't
        # pin this. We assert the conservative interpretation: without
        # an explicit allowed-tools or mcp config, we're still in v0
        # blanket-disable mode and `disallowed_tools` is not surfaced.
        # If the implementer disagrees, this is a spec-clarification
        # request, not a test bug.
        p = ClaudeCliProvider(disallowed_tools=["Bash"])
        argv = p._build_args(
            "x",
            system="",
            model=None,
            session_id=None,
        )
        assert "--tools" in argv
        assert "--disallowed-tools" not in argv


class TestRaiseForFailure:
    """`_raise_for_failure` maps claude CLI failure text to the typed
    error taxonomy the agent loop branches on (FC-2)."""

    def test_session_not_found_maps_to_session_not_found_error(self) -> None:
        from ember.errors import SessionNotFoundError

        with pytest.raises(SessionNotFoundError):
            ClaudeCliProvider._raise_for_failure(
                1,
                "",
                "No conversation found with session ID: 6195f39a-...",
            )

    def test_session_not_found_in_stderr(self) -> None:
        from ember.errors import SessionNotFoundError

        with pytest.raises(SessionNotFoundError):
            ClaudeCliProvider._raise_for_failure(
                1,
                "Error: No conversation found with session ID: abc",
                "",
            )

    def test_generic_failure_maps_to_provider_error(self) -> None:
        from ember.errors import ProviderError, SessionNotFoundError

        with pytest.raises(ProviderError) as ei:
            ClaudeCliProvider._raise_for_failure(1, "kaboom", "")
        # not the session-not-found subclass
        assert not isinstance(ei.value, SessionNotFoundError)

    def test_401_invalid_credentials_maps_to_auth_error(self) -> None:
        # The exact shape we hit when the OAuth token expired and refresh
        # was rejected: "Failed to authenticate. API Error: 401 Invalid
        # authentication credentials".
        from ember.errors import AuthError

        with pytest.raises(AuthError):
            ClaudeCliProvider._raise_for_failure(
                1,
                "",
                "Failed to authenticate. API Error: 401 Invalid authentication credentials",
            )

    def test_not_authenticated_still_maps_to_auth_error(self) -> None:
        from ember.errors import AuthError

        with pytest.raises(AuthError):
            ClaudeCliProvider._raise_for_failure(1, "not authenticated", "")


# --- known_models (selectable model options surfaced by /model) ----------


class TestKnownModels:
    """The provider advertises a curated set of selectable model ids that
    the `/model` command surfaces (via the router's `model_routes()`).
    Routing itself stays free-form — this is the suggestion set."""

    def test_includes_every_curated_tier_tag(self) -> None:
        # Tier tags, not pinned ids — the CLI resolves them to the latest
        # model in each tier.
        p = ClaudeCliProvider(default_model="opus")
        assert set(p.known_models) == {"opus", "fable", "sonnet"}

    def test_default_model_leads_and_is_not_duplicated(self) -> None:
        # Default first (fallback for a bare `claude_cli` route); deduped.
        p = ClaudeCliProvider(default_model="opus")
        assert p.known_models[0] == "opus"
        assert p.known_models.count("opus") == 1
        assert "fable" in p.known_models

    def test_non_curated_default_is_included_first(self) -> None:
        # A pinned full id still works as the default and leads the list.
        p = ClaudeCliProvider(default_model="claude-sonnet-4-6")
        assert p.known_models[0] == "claude-sonnet-4-6"


class TestCheapTierProviderIsToolless:
    """Regression guard for the `claude_cli_cheap` provider wired in main.py.

    Cheap-tier callers (compactor, tone judge, retrospective extraction) call
    `complete(..., tools=None)`, which means "use the constructor's tool
    config". If the cheap provider were ever constructed with the main
    instance's MCP config / allowlist, a *summarization* call would run with
    live `mcp__agent__*` tools (gmail_send, Schwab, ...) under
    `--permission-mode bypassPermissions`. It must stay blanket-disabled.
    """

    def _cheap_provider(self) -> ClaudeCliProvider:
        # Mirrors the construction in main.py: model only, no tool config.
        return ClaudeCliProvider(default_model="sonnet")

    async def test_tools_none_still_emits_blanket_disable(
        self, captured_args: list[list[str]]
    ) -> None:
        await self._cheap_provider().complete(
            system="Summarize the conversation above.",
            messages=[Message("user", "hi")],
            tools=None,
        )
        argv = captured_args[0]
        assert "--tools" in argv
        assert argv[argv.index("--tools") + 1] == ""
        # No tool surface of any kind reaches the subprocess.
        assert "--allowed-tools" not in argv
        assert "--mcp-config" not in argv
        assert not any(a.startswith("mcp__agent__") for a in argv)

    async def test_no_filesystem_builtins_leak_into_allowlist(
        self, captured_args: list[list[str]]
    ) -> None:
        # `tools=[]` takes the strict branch, which still appends
        # `_extra_allowed_tools` — the cheap provider must not have any.
        await self._cheap_provider().complete(
            system="",
            messages=[Message("user", "hi")],
            tools=[],
        )
        argv = " ".join(captured_args[0])
        for builtin in ("Bash", "Write", "Edit", "Read", "NotebookEdit"):
            assert builtin not in argv
