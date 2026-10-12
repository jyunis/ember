"""Generic MCP outbound client (FC-11).

Connects to an external MCP server, discovers its tools, and exposes
them. Each `McpOutboundClient` instance represents one upstream server.
The connection is held open for the process lifetime; tool calls flow
over it.

The session runs in a dedicated asyncio task — anyio cancel scopes
inside the MCP SDK don't tolerate being entered and exited from
different tasks, so spanning connect/disconnect via an `AsyncExitStack`
across method boundaries is unsafe. Single-task ownership avoids it.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import urllib.parse
from typing import Any

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client

from ember.errors import ToolError, TransportError

_log = logging.getLogger("ember.tools.mcp_client")

_SHUTDOWN = object()


class McpOutboundClient:
    """Holds a single MCP session against an upstream server.

    All session work runs on `_worker`, communicated via `_queue`.
    """

    def __init__(
        self,
        *,
        name: str,
        url: str,
        headers: dict[str, str] | None = None,
        transport: str = "streamableHttp",
    ) -> None:
        self._name = name
        self._url = url
        self._transport = transport
        self._extra_headers = dict(headers) if headers else None
        self._queue: asyncio.Queue[Any] | None = None
        self._task: asyncio.Task[None] | None = None
        self._ready: asyncio.Event = asyncio.Event()
        self._connect_error: BaseException | None = None
        self._last_error: str | None = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def url(self) -> str:
        return self._url

    @property
    def transport(self) -> str:
        return self._transport

    @property
    def status(self) -> str:
        """`connected`, `connecting`, `lost`, or `disconnected`."""
        if self._task is None:
            return "disconnected"
        if not self._ready.is_set():
            return "connecting"
        if self._task.done():
            return "lost"
        return "connected"

    @property
    def last_error(self) -> str | None:
        return self._last_error

    async def connect(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._queue = asyncio.Queue()
        self._ready = asyncio.Event()
        self._connect_error = None
        self._task = asyncio.create_task(self._worker(), name=f"mcp-{self._name}")
        await self._ready.wait()
        if self._connect_error is not None:
            err = self._connect_error
            self._task = None
            # Unwrap TaskGroup / ExceptionGroup so the real HTTP status
            # (e.g. "401 Unauthorized") surfaces instead of the opaque
            # "unhandled errors in a TaskGroup (1 sub-exception)" wrapper
            # MCP SDK / anyio nest connect failures inside.
            detail = _format_exception_chain(err)
            raise TransportError(f"MCP connect failed for {self._url!r}: {detail}") from err
        _log.info("MCP outbound %s connected to %s", self._name, self._url)

    async def disconnect(self) -> None:
        if self._task is None or self._queue is None:
            return
        await self._queue.put(_SHUTDOWN)
        try:
            await self._task
        except Exception:
            _log.debug("MCP %s worker exit raised", self._name, exc_info=True)
        self._task = None
        self._queue = None

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._dispatch(_ListTools())
        return [
            {
                "name": t.name,
                "description": t.description or "",
                "inputSchema": dict(t.inputSchema or {}),
            }
            for t in result.tools
        ]

    async def call_tool(self, tool_name: str, args: dict[str, Any]) -> Any:
        result = await self._dispatch(_CallTool(tool_name, args))
        if result.isError:
            text = self._content_to_text(result.content)
            raise ToolError(f"MCP {self._name} tool {tool_name!r} returned error: {text[:500]}")
        return self._content_to_value(result.content)

    # --- Internal -------------------------------------------------------

    async def _dispatch(self, op: "_SessionOp") -> Any:
        if self._queue is None or self._task is None:
            raise TransportError(f"MCP {self._name} is not connected")
        if self._task.done():
            raise TransportError(f"MCP {self._name} session has terminated unexpectedly")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        await self._queue.put((op, future))
        return await future

    async def _worker(self) -> None:
        assert self._queue is not None
        try:
            headers = self._connect_headers()
            if self._transport == "sse":
                # Legacy two-endpoint flow: GET <url> opens an SSE stream
                # whose first event names the POST endpoint for requests.
                async with sse_client(self._url, headers=headers) as (read, write):
                    await self._run_session(read, write)
            else:
                async with streamablehttp_client(self._url, headers=headers) as (
                    read,
                    write,
                    _,
                ):
                    await self._run_session(read, write)
        except BaseException as e:
            self._connect_error = e
            self._last_error = _format_exception_chain(e)
            self._ready.set()
            # Drain any pending requests with the error.
            try:
                while not self._queue.empty():
                    nxt = self._queue.get_nowait()
                    if nxt is _SHUTDOWN:
                        continue
                    _, future = nxt
                    if not future.done():
                        future.set_exception(TransportError(f"MCP {self._name} session lost: {e}"))
            except Exception:
                pass

    async def _run_session(self, read: Any, write: Any) -> None:
        assert self._queue is not None
        async with ClientSession(read, write) as session:
            await session.initialize()
            self._ready.set()
            while True:
                item = await self._queue.get()
                if item is _SHUTDOWN:
                    return
                op, future = item
                try:
                    result = await op.run(session)
                    future.set_result(result)
                except Exception as e:
                    future.set_exception(e)

    def _connect_headers(self) -> dict[str, str] | None:
        """Compose the outbound HTTP headers for the streamable client.

        Two contributions:
        - Host override for Docker-DNS / private-LAN URLs (FastMCP
          servers default to a localhost-only host allowlist;
          containerized clients reaching them via Docker DNS otherwise
          get HTTP 421). Public FQDNs must keep their real Host —
          shared frontends (e.g. *.cloudfunctions.net) route by it and
          answer 404 to a spoofed one, which the MCP SDK surfaces as
          the opaque "Session terminated".
        - Caller-supplied headers (e.g. Authorization for an upstream
          MCP that gates per-request).

        Caller-supplied keys win on collision so an explicit Host can
        override the default.
        """
        merged: dict[str, str] = {}
        host = (urllib.parse.urlparse(self._url).hostname or "").lower()
        if _needs_localhost_host_override(host):
            merged["Host"] = "localhost"
        if self._extra_headers:
            merged.update(self._extra_headers)
        return merged or None

    @staticmethod
    def _content_to_text(content_blocks: list[Any]) -> str:
        parts: list[str] = []
        for block in content_blocks:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
        return "\n".join(parts)

    @classmethod
    def _content_to_value(cls, content_blocks: list[Any]) -> Any:
        text = cls._content_to_text(content_blocks)
        if not text:
            return None
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            return text


def _needs_localhost_host_override(host: str) -> bool:
    """True for hosts that sit behind FastMCP's localhost-only Host
    allowlist: single-label Docker DNS names (`documents-api`) and
    private-LAN IP literals. localhost/loopback pass the allowlist as
    themselves, and public FQDNs must never be overridden."""
    if host in ("localhost", "127.0.0.1", "::1", ""):
        return False
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return "." not in host


def _format_exception_chain(exc: BaseException) -> str:
    """Render a short, useful description of an exception possibly nested
    inside `BaseExceptionGroup` / `TaskGroup`. The MCP SDK + anyio wrap
    connect-time HTTP errors (e.g. 401 from streamable_http_client) in
    a TaskGroup whose default str is `unhandled errors in a TaskGroup
    (1 sub-exception)` — useless. Pull out the leaf exceptions so the
    real status code reaches the model and the user."""
    leaves: list[BaseException] = []
    _collect_leaves(exc, leaves, depth=0, max_depth=8, max_leaves=5)
    parts: list[str] = []
    for leaf in leaves:
        msg = str(leaf).strip()
        if msg:
            parts.append(f"{type(leaf).__name__}: {msg}")
        else:
            parts.append(type(leaf).__name__)
    return "; ".join(parts) if parts else f"{type(exc).__name__}: {exc}"


def _collect_leaves(
    exc: BaseException,
    out: list[BaseException],
    *,
    depth: int,
    max_depth: int,
    max_leaves: int,
) -> None:
    if len(out) >= max_leaves or depth >= max_depth:
        return
    sub = getattr(exc, "exceptions", None)
    if sub:
        for child in sub:
            _collect_leaves(child, out, depth=depth + 1, max_depth=max_depth, max_leaves=max_leaves)
        return
    out.append(exc)


class _SessionOp:
    async def run(self, session: ClientSession) -> Any:
        raise NotImplementedError


class _ListTools(_SessionOp):
    async def run(self, session: ClientSession) -> Any:
        return await session.list_tools()


class _CallTool(_SessionOp):
    def __init__(self, name: str, args: dict[str, Any]) -> None:
        self.name = name
        self.args = args

    async def run(self, session: ClientSession) -> Any:
        return await session.call_tool(self.name, self.args)
