"""Siri voice channel adapter: HTTP ingress for Apple Shortcuts (doc #55).

A Shortcut POSTs one utterance to `/turn` and blocks for the spoken
reply, so this adapter inverts the usual channel shape: instead of a
persistent gateway pushing messages at us, each HTTP request *is* the
turn. The request registers a waiter (an `asyncio.Future`) under its
conversation key, hands the message to the agent loop, and serves back
whatever `send` resolves the waiter with (SIRI-UoW-2). Waiters are
keyed by conversation key, so a final for one conversation can never
answer a request on another, and every concurrent caller on one key is
answered by the same coalesced final.

Auth precedes everything (SIRI-TLC-4): the bearer token is checked
before the body is even parsed, and construction fails closed — an
enabled ingress with a blank secret refuses to exist at all.

Siri has no thread UI, so conversation continuity is time-based
(SIRI-UoW-1): an invocation within `session_window_s` (default 10 min)
of the newest live siri conversation continues it — consecutive
"Hey Siri, ask Ember…" exchanges share context — and anything later
mints a fresh `siri:<epoch>` key. A conversation holding pending state
(tool confirmation, proposal, Schwab reauth) stays joinable past the
window: the confirmation must remain redeemable however long the user
takes to answer.

Siri also can't wait forever — Shortcuts gives up after ~30s — so a
turn that outruns `deadline_s` gets an honest interim answer
(SIRI-TLC-2): "working on it, check Discord", `finished: false`. The
handler keeps running; its eventual final finds no live waiter and
reroutes through `fallback_notify` (the Discord scheduler surface), so
a slow answer is never silently dropped. Persistence is the agent's
job, not ours — the reply is in the store before `send` is called, so
a lapsed waiter loses delivery, never data (SIRI-TLC-5).

`kind="status"` sends are deliberately delivered nowhere: Discord's
🔧 tool-call stream is noise over voice. The last status per key is
kept only as the fallback reply for a turn that ends without a final
(the agent's error path emits a lone ❌ status; surfacing it beats a
silent shrug).
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ember.channels.base import Channel, ChannelMessage, MessageHandler, SendKind
from ember.store.repo import ChannelSession, Store

_log = logging.getLogger("ember.channels.siri")

WORKING_TEXT = "Working on it — I'll post the result in Discord."
NO_REPLY_TEXT = "That turn ended without a reply — check Discord for details."


def resolve_conversation_key(
    *,
    now: datetime,
    newest: ChannelSession | None,
    new: bool,
    window_s: int,
) -> str:
    """Pure session-key resolution (SIRI-UoW-1). Rules, in order:
    explicit `new` mints; no session mints; pending state continues (a
    pending tool-confirmation/proposal must stay redeemable past the
    window); activity within `window_s` continues; otherwise mint.
    All datetimes are tz-aware UTC."""
    if new or newest is None:
        return f"siri:{int(now.timestamp())}"
    if newest.has_pending:
        return newest.key
    if (now - newest.last_active).total_seconds() <= window_s:
        return newest.key
    return f"siri:{int(now.timestamp())}"


@dataclass
class _Waiter:
    """One blocked `/turn` request: a future the next final resolves,
    plus the last status seen while it waited (the no-final fallback)."""

    future: asyncio.Future[str]
    last_status: str | None = field(default=None)


class SiriChannel(Channel):
    """Starlette/uvicorn-backed adapter implementing the `Channel`
    protocol over a request/response ingress."""

    name: str = "siri"

    def __init__(
        self,
        *,
        token: str,
        store: Store,
        fallback_notify: Callable[[str], Awaitable[None]],
        deadline_s: float = 25.0,
        session_window_s: int = 600,
        bind: str = "0.0.0.0",
        port: int = 9940,
        user_id: str = "siri",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        # SIRI-TLC-4 fails closed: an enabled ingress without a secret
        # must refuse to construct, not serve unauthenticated.
        if not token or not token.strip():
            raise ValueError("SiriChannel requires a non-empty bearer token (SIRI-TLC-4)")
        self._token = token
        self._store = store
        self._fallback_notify = fallback_notify
        self._deadline_s = deadline_s
        self._session_window_s = session_window_s
        self._bind = bind
        self._port = port
        self._user_id = user_id
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc)
        self._handler: MessageHandler | None = None
        self._server: uvicorn.Server | None = None
        self._waiters: dict[str, list[_Waiter]] = {}
        # Public so tests can drive it via httpx's ASGITransport
        # without binding a socket.
        self.app = Starlette(
            routes=[
                Route("/healthz", self._healthz, methods=["GET"]),
                Route("/turn", self._turn, methods=["POST"]),
            ]
        )

    def register_handler(self, handler: MessageHandler) -> None:
        self._handler = handler

    async def start(self, on_message: MessageHandler) -> None:
        self.register_handler(on_message)
        # info-level so uvicorn's access log records every request
        # (method, path, status, client) — including the 405s starlette
        # answers before our handler runs (e.g. a Shortcut left on GET).
        config = uvicorn.Config(self.app, host=self._bind, port=self._port, log_level="info")
        self._server = uvicorn.Server(config)
        try:
            await self._server.serve()
        finally:
            self._server = None

    async def stop(self) -> None:
        # Safe when not started; `serve()` polls `should_exit` and
        # returns promptly once set.
        if self._server is not None:
            self._server.should_exit = True

    # --- HTTP endpoints ---------------------------------------------------

    async def _healthz(self, request: Request) -> JSONResponse:
        # No auth: liveness only, for Shortcut debugging.
        return JSONResponse({"status": "ok"})

    async def _turn(self, request: Request) -> JSONResponse:
        # Rejections are logged with the client address (never the
        # supplied credential): the Shortcut fails silently on a 4xx —
        # no `reply` field, nothing spoken — so the server log is the
        # only place a misconfigured phone shows up at all.
        client = request.client.host if request.client else "?"
        # SIRI-TLC-4: auth before the body is touched. Constant-time
        # compare on encoded bytes; constant error body either way.
        supplied = request.headers.get("authorization", "")
        expected = f"Bearer {self._token}"
        if not hmac.compare_digest(supplied.encode(), expected.encode()):
            _log.info("siri /turn from %s rejected: unauthorized", client)
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        def _bad(reason: str) -> JSONResponse:
            _log.info("siri /turn from %s rejected: bad_request (%s)", client, reason)
            return JSONResponse({"error": "bad_request"}, status_code=400)

        try:
            body = await request.json()
        except Exception:
            return _bad("body is not JSON")
        if not isinstance(body, dict):
            return _bad("body is not an object")
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            return _bad("missing/empty text")
        new = body.get("new", False)
        if not isinstance(new, bool):
            return _bad("non-bool new")

        handler = self._handler
        if handler is None:
            return JSONResponse({"error": "not_ready"}, status_code=503)

        newest = await self._store.newest_channel_session("siri")
        key = resolve_conversation_key(
            now=self._clock(),
            newest=newest,
            new=new,
            window_s=self._session_window_s,
        )

        msg = ChannelMessage(
            channel="siri",
            conversation_key=key,
            user_id=self._user_id,
            text=text,
            message_id=uuid4().hex,
            attachments=(),
        )
        waiter = _Waiter(future=asyncio.get_running_loop().create_future())
        self._waiters.setdefault(key, []).append(waiter)
        task = asyncio.create_task(handler(msg))
        # The task outlives the request on the deadline path; without
        # this guard its exception would vanish unobserved.
        task.add_done_callback(self._log_handler_failure)
        try:
            await asyncio.wait(
                {waiter.future, task},
                timeout=self._deadline_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if waiter.future.done():
                return self._turn_response(waiter.future.result(), key, finished=True)
            if task.done():
                # Handler finished without a final send: surface the
                # last status (the agent's error path emits only a ❌
                # status) rather than a silent shrug.
                reply = waiter.last_status if waiter.last_status is not None else NO_REPLY_TEXT
                return self._turn_response(reply, key, finished=True)
            # Deadline (SIRI-TLC-2): answer honestly now, let the turn
            # run on. Deregister first so the eventual final routes via
            # fallback instead of a dead future — then re-check for a
            # final that landed in the gap.
            self._deregister(key, waiter)
            if waiter.future.done():
                return self._turn_response(waiter.future.result(), key, finished=True)
            waiter.future.cancel()
            return self._turn_response(WORKING_TEXT, key, finished=False)
        finally:
            self._deregister(key, waiter)

    @staticmethod
    def _turn_response(reply: str, key: str, *, finished: bool) -> JSONResponse:
        return JSONResponse({"reply": reply, "conversation_key": key, "finished": finished})

    def _deregister(self, key: str, waiter: _Waiter) -> None:
        waiters = self._waiters.get(key)
        if waiters is None:
            return
        try:
            waiters.remove(waiter)
        except ValueError:
            pass
        if not waiters:
            del self._waiters[key]

    @staticmethod
    def _log_handler_failure(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            _log.error("siri turn handler failed", exc_info=exc)

    # --- Channel protocol -------------------------------------------------

    async def send(
        self,
        conversation_key: str,
        content: str,
        *,
        kind: SendKind = "reply",
    ) -> list[str]:
        if kind == "status":
            # Delivered nowhere (see module docstring); kept only as the
            # no-final fallback for waiters currently blocked on this key.
            for waiter in self._waiters.get(conversation_key, ()):
                waiter.last_status = content
            return []
        waiters = self._waiters.get(conversation_key, [])
        live = [w for w in waiters if not w.future.done()]
        if live:
            # A coalesced turn answers every caller blocked on this key.
            for waiter in live:
                waiter.future.set_result(content)
        else:
            # Waiter lapsed (deadline) or never existed (scheduled turn
            # on a siri key): reroute so the reply is never silently
            # dropped (SIRI-TLC-2).
            await self._notify_fallback(f"[siri] {content}")
        # No channel-native message ids on a transient surface (base.py).
        return []

    async def send_attachments(
        self,
        conversation_key: str,
        paths: list[str],
    ) -> None:
        if not paths:
            return
        # Voice can't carry files; hand every path to the fallback surface.
        listing = ", ".join(paths)
        await self._notify_fallback(
            f"[siri] attachments for {conversation_key} (undeliverable over voice): {listing}"
        )

    async def set_typing(self, conversation_key: str, on: bool = True) -> None:
        pass  # no presence surface

    async def notify_scheduler(self, content: str) -> None:
        # The mux never routes scheduler traffic here (Discord is the
        # scheduler surface); log so a misroute is at least visible.
        _log.debug("notify_scheduler on siri channel ignored: %s", content)

    async def _notify_fallback(self, content: str) -> None:
        """Best-effort fallback delivery: never raise out of a send."""
        try:
            await self._fallback_notify(content)
        except Exception:
            _log.exception("siri fallback_notify failed")
