"""claude CLI provider.

Subprocesses the `claude` binary in `--print --output-format stream-json
--verbose` mode and parses the line-delimited JSON event stream.

Auth comes from claude's own local credentials (`~/.claude/`), set up
once per container via `claude login`. We never see or touch the auth
material.

v0 disables all tools (`--tools ""`) — the agent loop has no tool
registry yet. Conversation continuity uses claude's `--resume <id>`
mechanism: each turn returns a `session_id` which the agent persists on
the conversation row and passes back on the next call.

See contracts doc § FC-2.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from dataclasses import dataclass
from pathlib import Path

from ember.context.tool_pruner import (
    DEFAULT_POLICY,
    RetentionPolicy,
    prune_session_jsonl,
)
from ember.errors import (
    AuthError,
    ProviderError,
    RateLimitError,
    SessionNotFoundError,
    TransportError,
)
from ember.providers.base import (
    TEXT_PARTS_SEPARATOR,
    Capabilities,
    CompletionResult,
    Message,
    StreamHandler,
    ToolCall,
    ToolSpec,
    Usage,
)

_log = logging.getLogger("ember.providers.claude_cli")

# claude --print emits its init event with MCP servers still "pending" and
# connects them asynchronously; the in-process agent bridge (FastMCP over
# localhost HTTP) usually finishes connecting within ~1s. If the model issues
# its first tool call before then it gets a hard "No such tool available:
# mcp__agent__<name>" error and the whole turn loses every agent tool. A fresh
# subprocess almost always wins the race, so we detect this signature and
# relaunch up to this many total attempts.
_MCP_NOT_ATTACHED_SIGNATURE = "No such tool available: mcp__agent__"

# The CLI reports bridge tools by their MCP-namespaced name
# (`mcp__agent__<name>`); the registry and audit rows use the bare name.
_BRIDGE_TOOL_PREFIX = "mcp__agent__"
_MCP_ATTACH_MAX_ATTEMPTS = 3
# How long an interrupted claude subprocess gets to exit on SIGTERM
# before we SIGKILL it.
_REAP_TIMEOUT_S = 5.0

# Curated model options offered for the claude_cli provider (surfaced by
# `/model` autocomplete + `action:list`). Routing itself stays free-form —
# any string the claude CLI accepts still works via
# `/model action:set value:claude_cli:<model>`; these are just the
# suggestions.
#
# Tier tags, not pinned ids: `--model` resolves a bare tag to the latest
# model in that tier ("Provide an alias for the latest model (e.g. 'fable',
# 'opus', or 'sonnet')"), so these options follow model releases without a
# code change. Tags work on every auth path — it was the *dated* alias
# ("sonnet-4-6") that 404'd under a setup-token OAuth credential, not the
# bare tier tag. Pin a full id (`claude_cli:claude-opus-4-8`) when a
# specific version is wanted; existing pinned routes keep working.
_CURATED_MODELS: tuple[str, ...] = (
    "opus",  # latest Opus-tier — the default
    "fable",  # latest Fable-tier — most capable
    "sonnet",  # latest Sonnet-tier — faster / cheaper
)


@dataclass
class _RunOutcome:
    """Result of one claude subprocess invocation."""

    text: str
    usage: Usage | None
    # v15 (FC-28 / TLC-24): true context size from the LAST assistant
    # event carrying `message.usage` — input + cache_read + cache_creation.
    # None when no assistant event carried usage.
    context_tokens: int | None
    new_session_id: str | None
    return_code: int
    stderr: str
    result_event_seen: bool
    result_is_error: bool
    result_error_text: str
    mcp_not_attached: bool
    # Tool calls the CLI already executed via the MCP bridge, in stream
    # order, with observed result status (UoW-4 step 5 audit).
    executed_tool_calls: list[ToolCall]
    # v16 (FC-33): every non-empty assistant text block, in stream order.
    # Sidechain events (truthy `parent_tool_use_id` — subagent narration)
    # are excluded, mirroring the agent's stream observer so block counts
    # stay aligned between the two (TLC-28 exactly-once by count).
    text_parts: list[str]


class ClaudeCliProvider:
    """Provider that shells out to the `claude` CLI."""

    name: str = "claude_cli"
    capabilities: Capabilities = Capabilities(
        tool_use=False,  # v0; flips to True when MCP bridge lands
        streaming=True,
        prompt_cache=True,
    )

    def __init__(
        self,
        *,
        binary: str = "claude",
        # Tier tag — resolves to the latest Opus the CLI knows about, on
        # every auth path. (The *dated* alias "sonnet-4-6" is the one that
        # 404s under a setup-token OAuth credential.) main.py passes
        # settings.claude_default_model anyway.
        default_model: str = "opus",
        mcp_config_paths: list[str] | None = None,
        allowed_tools: str | None = None,
        disallowed_tools: list[str] | None = None,
        permission_mode: str | None = "bypassPermissions",
        extra_allowed_tools: list[str] | None = None,
        session_jsonl_dir: Path | None = None,
        retention_policy: RetentionPolicy | None = None,
        tool_pruner_enabled: bool = False,
    ) -> None:
        self._binary = binary
        self._default_model = default_model
        # Selectable model options surfaced by `/model` (via the router's
        # `model_routes()`). Default first — it's the fallback for a bare
        # `claude_cli` route — then the curated extras, de-duplicated with
        # order preserved. Advisory only; routing stays free-form.
        self.known_models: tuple[str, ...] = tuple(
            dict.fromkeys(m for m in (default_model, *_CURATED_MODELS) if m)
        )
        self._mcp_config_paths = list(mcp_config_paths) if mcp_config_paths else []
        self._allowed_tools = allowed_tools
        self._disallowed_tools = list(disallowed_tools) if disallowed_tools else []
        self._permission_mode = permission_mode
        # Built-in tools to additionally allow alongside `mcp__agent__*`.
        # Required because claude CLI gates on the intersection of the
        # allowed-tools allowlist and the disallowed-tools denylist —
        # dropping a built-in from `--disallowed-tools` is not enough if
        # it's also outside the allowlist. Used by Ember to re-enable
        # `Read` / `Bash` / `Grep` / `Glob` / `Edit` / `Write` /
        # `NotebookEdit` so the model can interact with user-uploaded
        # files stored under `data_dir/uploads/` (BC-6 / BC-8).
        self._extra_allowed_tools = list(extra_allowed_tools) if extra_allowed_tools else []
        # TLC-18 / UoW-19: tool-output pruner runs over the session jsonl
        # before each --resume. None disables pruning (e.g. tests that
        # don't need it).
        # v15 (FC-31): additionally gated on `tool_pruner_enabled` (wired
        # from TOOL_PRUNER_ENABLED in main.py, default off) — the pruner
        # rewrites an unsupported session-jsonl format and is
        # deprecated-pending-removal now that oversize tool results become
        # payload-store references at the source (FC-30).
        self._session_jsonl_dir = session_jsonl_dir
        self._retention_policy = retention_policy or DEFAULT_POLICY
        self._tool_pruner_enabled = tool_pruner_enabled

    async def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        model: str | None = None,
        stream_handler: StreamHandler | None = None,
        session_id: str | None = None,
    ) -> CompletionResult:
        prompt = self._extract_user_prompt(messages)
        # UoW-19 pre-`--resume` prune. Failures non-fatal — proceed
        # against the unpruned session and log. v15 (FC-31): skipped
        # entirely unless TOOL_PRUNER_ENABLED flips it back on.
        if self._tool_pruner_enabled and session_id and self._session_jsonl_dir is not None:
            self._prune_session(session_id)

        cur_session_id = session_id
        outcome: _RunOutcome | None = None
        # Accumulates across MCP-attach relaunches: the first attempt's
        # (failed) tool calls were real events in the session and belong
        # in the turn's audit trail (TLC-8).
        executed_tool_calls: list[ToolCall] = []
        # v16 (FC-33): text blocks likewise accumulate across relaunches —
        # the stream observer already posted attempt-1 blocks, so dropping
        # them here would desync its posted count from `text_parts`.
        text_parts: list[str] = []
        for attempt in range(1, _MCP_ATTACH_MAX_ATTEMPTS + 1):
            args = self._build_args(
                prompt,
                system=system,
                model=model,
                session_id=cur_session_id,
                tools=tools,
            )
            outcome = await self._run_subprocess(args, stream_handler)
            executed_tool_calls.extend(outcome.executed_tool_calls)
            text_parts.extend(outcome.text_parts)

            # Relaunch only on the MCP-attach race: the bridge didn't connect
            # before the model's first tool call, so the turn lost its agent
            # tools. The subprocess itself exited cleanly (rc 0) — a non-zero
            # exit or provider error is a real failure, not this race. Resume
            # the (now-advanced) session so context carries across the retry.
            if (
                outcome.mcp_not_attached
                and outcome.return_code == 0
                and attempt < _MCP_ATTACH_MAX_ATTEMPTS
            ):
                _log.warning(
                    "agent MCP tools were unavailable (attempt %d/%d); relaunching claude",
                    attempt,
                    _MCP_ATTACH_MAX_ATTEMPTS,
                )
                if outcome.new_session_id:
                    cur_session_id = outcome.new_session_id
                continue
            break

        assert outcome is not None  # loop runs at least once
        if outcome.mcp_not_attached and outcome.return_code == 0:
            _log.error(
                "agent MCP tools still unavailable after %d attempts",
                _MCP_ATTACH_MAX_ATTEMPTS,
            )

        if outcome.return_code != 0 or outcome.result_is_error:
            self._raise_for_failure(outcome.return_code, outcome.stderr, outcome.result_error_text)

        if not outcome.result_event_seen or outcome.usage is None:
            raise ProviderError(
                "claude CLI did not emit a result event "
                f"(rc={outcome.return_code}, stderr={outcome.stderr[:200]!r})"
            )

        # v16 (FC-33): the assistant text blocks are the source of truth
        # for the reply, and `text` is their pinned join — historically
        # `text` was the result event's `result` string, which carries only
        # the FINAL assistant message and silently dropped the narration
        # between tool calls that TLC-28 now delivers. When no text block
        # was seen at all (defensive: a result with no assistant events),
        # fall back to the result text so the invariant
        # `text == TEXT_PARTS_SEPARATOR.join(text_parts)` still holds.
        if text_parts:
            text = TEXT_PARTS_SEPARATOR.join(text_parts)
        else:
            text = outcome.text
            text_parts = [text] if text else []

        return CompletionResult(
            text=text,
            executed_tool_calls=executed_tool_calls,
            usage=outcome.usage,
            session_id=outcome.new_session_id,
            context_tokens=outcome.context_tokens,
            text_parts=tuple(text_parts),
        )

    async def _run_subprocess(
        self,
        args: list[str],
        stream_handler: StreamHandler | None,
    ) -> _RunOutcome:
        """Launch one claude subprocess and parse its stream-json output."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # Default StreamReader limit is 64KB per line; tool_result
                # stream-json events for tools like rss_fetch (25 feeds ×
                # items) easily exceed that and crash readline() with
                # LimitOverrunError. 8MB is comfortably above any realistic
                # single tool result.
                limit=8 * 1024 * 1024,
                # Own process group, so an interrupt can signal the whole
                # tree. claude spawns children (MCP servers, subagents);
                # signalling only the parent leaves them orphaned, still
                # holding our stdout pipe and still doing work. See
                # `_reap`. Harmless on the normal path — the CLI runs
                # non-interactively under `--print`.
                start_new_session=True,
            )
        except FileNotFoundError as e:
            raise AuthError(f"claude binary not found at {self._binary!r}") from e
        except OSError as e:
            raise TransportError(f"failed to start claude CLI: {e}") from e

        # The turn gate cancels this coroutine when the user interrupts.
        # Cancellation only unwinds the `async for` below — the claude
        # process would keep running, keep burning subscription quota,
        # and keep writing to the session jsonl that the *next* turn is
        # about to resume and prune. Reap it explicitly.
        try:
            return await self._consume_stream(proc, stream_handler)
        except asyncio.CancelledError:
            await self._reap(proc)
            raise

    async def _consume_stream(
        self,
        proc: asyncio.subprocess.Process,
        stream_handler: StreamHandler | None,
    ) -> _RunOutcome:
        """Parse one running claude subprocess's stream-json output."""
        text = ""
        usage: Usage | None = None
        # TLC-24: the LAST assistant event whose message carries usage is
        # the honest context measurement for the turn — earlier assistant
        # events (pre-tool-use) undercount, and the result event's
        # aggregate mixes in per-call input. Tracked here, summed below.
        last_assistant_usage: dict | None = None
        new_session_id: str | None = None
        result_event_seen = False
        result_is_error = False
        result_error_text = ""
        mcp_not_attached = False
        # UoW-4 step 5: audit trail of bridge-executed tool calls. Names
        # are stored bare (bridge prefix stripped) so the row matches
        # what an in-process dispatch would have recorded. tool_result
        # events pair by tool_use_id when present, else FIFO — the CLI
        # has emitted tool_use blocks without ids.
        executed: list[dict] = []
        by_id: dict[str, dict] = {}
        unresolved: list[dict] = []
        # v16 (FC-33): ordered non-empty assistant text blocks. Must apply
        # the exact predicate the agent's stream observer uses (non-empty
        # text, non-sidechain event) or the posted-count remainder math
        # breaks (TLC-28).
        text_blocks: list[str] = []

        assert proc.stdout is not None
        async for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            # Cheap substring scan before JSON parse — the signature appears
            # inside a tool_result error event the model receives.
            if _MCP_NOT_ATTACHED_SIGNATURE in line:
                mcp_not_attached = True
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                _log.warning("claude CLI emitted non-JSON line: %s", line[:200])
                continue

            if stream_handler is not None:
                try:
                    await stream_handler(event)
                except Exception:
                    _log.exception("stream_handler raised; continuing")

            etype = event.get("type")
            if etype == "system" and event.get("session_id"):
                new_session_id = event["session_id"]
            elif etype in ("assistant", "user"):
                message = event.get("message") or {}
                if etype == "assistant":
                    event_usage = message.get("usage")
                    if isinstance(event_usage, dict):
                        last_assistant_usage = event_usage
                # Sidechain events (subagent streams) carry a parent
                # tool_use id; their narration is not part of the reply.
                sidechain = bool(event.get("parent_tool_use_id"))
                content = message.get("content")
                for block in content if isinstance(content, list) else []:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if etype == "assistant" and btype == "text" and not sidechain:
                        block_text = str(block.get("text") or "")
                        if block_text:
                            text_blocks.append(block_text)
                    elif etype == "assistant" and btype == "tool_use":
                        name = str(block.get("name") or "")
                        if name.startswith(_BRIDGE_TOOL_PREFIX):
                            name = name[len(_BRIDGE_TOOL_PREFIX) :]
                        args = block.get("input")
                        call = {
                            "id": str(block.get("id") or ""),
                            "name": name,
                            "args": args if isinstance(args, dict) else {},
                            "status": None,
                        }
                        executed.append(call)
                        unresolved.append(call)
                        if call["id"]:
                            by_id[call["id"]] = call
                    elif etype == "user" and btype == "tool_result":
                        tu_id = str(block.get("tool_use_id") or "")
                        call = by_id.get(tu_id) or (unresolved[0] if unresolved else None)
                        if call is None:
                            continue
                        call["status"] = "error" if block.get("is_error") else "ok"
                        if call in unresolved:
                            unresolved.remove(call)
            elif etype == "result":
                result_event_seen = True
                result_is_error = bool(event.get("is_error"))
                if result_is_error:
                    result_error_text = str(event.get("result") or event.get("subtype") or "")
                else:
                    text = str(event.get("result") or "")
                    usage = self._parse_usage(event.get("usage") or {})
                if event.get("session_id"):
                    new_session_id = event["session_id"]

        return_code = await proc.wait()
        stderr_bytes = await proc.stderr.read() if proc.stderr is not None else b""
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        # TLC-24 context sum: the final assistant event's three input
        # fields ARE the context the model saw on its last inference of
        # the turn. None when no assistant event carried usage.
        context_tokens: int | None = None
        if last_assistant_usage is not None:
            context_tokens = (
                int(last_assistant_usage.get("input_tokens") or 0)
                + int(last_assistant_usage.get("cache_read_input_tokens") or 0)
                + int(last_assistant_usage.get("cache_creation_input_tokens") or 0)
            )

        return _RunOutcome(
            text=text,
            usage=usage,
            context_tokens=context_tokens,
            new_session_id=new_session_id,
            return_code=return_code,
            stderr=stderr,
            result_event_seen=result_event_seen,
            result_is_error=result_is_error,
            result_error_text=result_error_text,
            mcp_not_attached=mcp_not_attached,
            executed_tool_calls=[ToolCall(**c) for c in executed],
            text_parts=text_blocks,
        )

    @staticmethod
    def _signal_tree(proc: asyncio.subprocess.Process, sig: int) -> None:
        """Send `sig` to the subprocess's whole process group.

        The group exists because we spawn with `start_new_session=True`.
        Falls back to signalling the direct child if the group lookup
        fails (already exited, or pid reuse).

        Safety: we signal the group ONLY after confirming it is not our
        own. If `start_new_session` ever fails to take effect, the child
        shares Ember's process group and `killpg` would SIGTERM Ember
        itself — in the container, that is PID 1 and the whole service.
        """
        try:
            pgid = os.getpgid(proc.pid)
            if pgid != os.getpgrp():
                os.killpg(pgid, sig)
                return
            _log.warning(
                "claude subprocess shares Ember's process group; "
                "signalling the child alone to avoid self-termination"
            )
        except (ProcessLookupError, PermissionError, OSError):
            _log.debug("killpg of claude subprocess failed; signalling pid", exc_info=True)
        try:
            proc.send_signal(sig)
        except (ProcessLookupError, OSError):
            pass

    @classmethod
    async def _reap(cls, proc: asyncio.subprocess.Process) -> None:
        """Terminate an abandoned claude process tree and wait for it.

        SIGTERM first so the CLI can flush its session file and shut its
        MCP children down cleanly; SIGKILL if the group doesn't go.
        Every failure is swallowed — this runs on the cancellation path,
        where raising would mask the interrupt.
        """
        if proc.returncode is not None:
            return
        cls._signal_tree(proc, signal.SIGTERM)
        try:
            # Shielded: we are already inside a CancelledError unwind, and
            # an unshielded await here would be cancelled immediately,
            # leaving the process unreaped — the exact leak we're closing.
            await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=_REAP_TIMEOUT_S)
        except (TimeoutError, asyncio.TimeoutError):
            _log.warning("claude process group ignored SIGTERM; killing")
            cls._signal_tree(proc, signal.SIGKILL)
            try:
                await asyncio.shield(proc.wait())
            except Exception:
                _log.debug("wait after kill of claude subprocess failed", exc_info=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.debug("wait on terminated claude subprocess failed", exc_info=True)

    # --- Internals --------------------------------------------------------

    def _prune_session(self, session_id: str) -> None:
        """Run the tool-output pruner against the session jsonl. Best
        effort — every failure mode is swallowed inside the pruner and
        returned as a `PruneResult` with `skipped_reason` set."""
        assert self._session_jsonl_dir is not None  # caller's guard
        path = self._session_jsonl_dir / f"{session_id}.jsonl"
        result = prune_session_jsonl(path, self._retention_policy)
        if result.skipped_reason is not None:
            _log.debug(
                "tool_pruner skipped: session=%s reason=%s",
                session_id,
                result.skipped_reason,
            )
        elif result.tool_results_rewritten > 0:
            _log.info(
                "tool_pruner: session=%s stubs=%d bytes_reclaimed=%d",
                session_id,
                result.tool_results_rewritten,
                result.bytes_reclaimed,
            )

    @staticmethod
    def _extract_user_prompt(messages: list[Message]) -> str:
        """Every user message claude has not answered yet, oldest first.

        Usually that is just the newest one: with `--resume`, claude
        reconstructs the rest of the history from its own session
        storage. For first-turn calls (no session_id) it is likewise the
        entire prompt, with no prior assistant content yet.

        But a turn can end with the user's text never reaching claude's
        session at all. The turn gate cancels the in-flight turn when a
        new message arrives, and `_reap` kills a subprocess that has
        often not yet written the prompt into its session jsonl — so the
        interrupted message lives only in *our* store, which claude never
        reads. Sending just the newest message would silently drop it
        (observed: "…heirs would need to sell assets?" interrupted by the
        correction "Heirs" produced a reply to the single word alone).

        So take the whole trailing run of user messages — everything
        after the last assistant turn. Anything older than that reply is
        already in the session file. Re-sending a message that *did*
        reach the session (interrupted late, mid-response) costs a
        duplicate paragraph; dropping one costs the question.
        """
        tail: list[str] = []
        for m in reversed(messages):
            if m.role != "user":
                break
            if m.content:
                tail.append(m.content)
        if tail:
            tail.reverse()
            return "\n\n".join(tail)
        # History doesn't end on a user message — the tool-dispatch loop
        # re-enters with tool results appended. Prompt as before.
        for m in reversed(messages):
            if m.role == "user":
                return m.content
        return ""

    def _build_args(
        self,
        prompt: str,
        *,
        system: str,
        model: str | None,
        session_id: str | None,
        tools: list[ToolSpec] | None = None,
    ) -> list[str]:
        args: list[str] = [
            self._binary,
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
        ]

        # Tool gating.
        # v7: when `tools` is non-None, build the per-call allowlist
        # from the supplied ToolSpecs (strict — empty list disables all
        # MCP tools for this call). When `tools` is None, fall back to
        # the constructor's static `_allowed_tools` (current behavior).
        # v14: in both branches we also append `_extra_allowed_tools`
        # (claude built-ins like `Read` that we re-enabled for upload
        # handling) so they aren't filtered out by the allowlist —
        # `--disallowed-tools` is only the negative gate.
        if tools is not None:
            mcp_allow = [f"mcp__agent__{t.name}" for t in tools]
            allowlist = ",".join(mcp_allow + self._extra_allowed_tools)
            args.extend(["--allowed-tools", allowlist])
            if self._disallowed_tools:
                args.extend(["--disallowed-tools", ",".join(self._disallowed_tools)])
        elif self._allowed_tools or self._mcp_config_paths:
            # v0.5+: bridge is wired; allow our MCP tools and explicitly
            # disallow any built-ins we listed.
            if self._allowed_tools:
                allowlist_parts: list[str] = [self._allowed_tools]
                allowlist_parts.extend(self._extra_allowed_tools)
                args.extend(["--allowed-tools", ",".join(allowlist_parts)])
            if self._disallowed_tools:
                args.extend(["--disallowed-tools", ",".join(self._disallowed_tools)])
        else:
            # v0: no tools at all.
            args.extend(["--tools", ""])

        for path in self._mcp_config_paths:
            args.extend(["--mcp-config", path])

        # Headless deployments need an explicit permission mode. The
        # default mode prompts for per-tool approval and falls through
        # to deny when stdin is a pipe (--print) — tool calls fail
        # intermittently with "Claude requested permissions to use X,
        # but you haven't granted it yet". bridge tools are already
        # gated server-side (TLC-3 + mcp_connector audit log), so
        # Claude CLI's permission layer is redundant.
        #
        # `bypassPermissions` is the right mode for headless container
        # runs: no per-tool prompts, no session-scoped mode persistence
        # surprises on `--resume`. Claude CLI refuses this mode under
        # root, so the container runs as a non-root user (uid 1000)
        # configured in the Dockerfile.
        if self._permission_mode:
            args.extend(["--permission-mode", self._permission_mode])

        chosen_model = model or self._default_model
        if chosen_model:
            args.extend(["--model", chosen_model])
        if session_id:
            args.extend(["--resume", session_id])
        if system:
            args.extend(["--append-system-prompt", system])
        args.append(prompt)
        return args

    @staticmethod
    def _parse_usage(usage_data: dict) -> Usage:
        """All claude_cli turns are subscription-billed by definition.

        We surface `cost_usd=None, subscription=True` regardless of any
        `total_cost_usd` claude reports — keeping the contract clean
        (TLC-5). v15 (FC-28): `cache_creation_input_tokens` maps onto
        `Usage.cache_creation_tokens` so the message row records the
        cache-write volume that was previously dropped.
        """
        return Usage(
            input_tokens=int(usage_data.get("input_tokens") or 0),
            output_tokens=int(usage_data.get("output_tokens") or 0),
            cached_tokens=int(usage_data.get("cache_read_input_tokens") or 0),
            cache_creation_tokens=int(usage_data.get("cache_creation_input_tokens") or 0),
            cost_usd=None,
            subscription=True,
        )

    @staticmethod
    def _raise_for_failure(return_code: int, stderr: str, result_error: str) -> None:
        blob = f"{stderr}\n{result_error}".lower()
        if "rate limit" in blob or "rate-limit" in blob or "too many requests" in blob:
            raise RateLimitError(stderr or result_error or "rate limited")
        if (
            # "authenticat" covers authenticate / authenticated /
            # authentication — the 401 we hit reads "Failed to authenticate.
            # API Error: 401 Invalid authentication credentials".
            "authenticat" in blob or "login" in blob or "unauthor" in blob or "401" in blob
        ):
            raise AuthError(stderr or result_error or "claude CLI auth error")
        if "no conversation found with session id" in blob:
            # The session jsonl we passed to `--resume` is gone (claude
            # transcript cleanup or a /data rebuild). Distinct type so the
            # agent loop can drop the dead id and retry fresh.
            raise SessionNotFoundError(stderr or result_error or "session not found")
        raise ProviderError(f"claude CLI failed (rc={return_code}): {stderr or result_error}")
