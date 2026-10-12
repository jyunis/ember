"""Channel multiplexer: several real adapters behind one `Channel`.

The agent loop stays single-channel (TLC-14); the mux routes outbound
calls by conversation-key namespace instead of teaching `Agent` about
multiple adapters. Keys of the form ``<ns>:<rest>`` go to the adapter
registered under ``ns``; a key with a namespace no adapter claims
raises (SIRI-TLC-3: every key routes to exactly one adapter, never
best-effort). Keys without a namespace go to the default adapter —
Discord's numeric thread IDs predate namespacing and must keep
resolving to the same `(channel, key)` store rows they always have.

`notify_scheduler` always goes to the designated scheduler adapter
(Discord), no matter which channel a turn came from: the scheduler
surface is a place, not a conversation (see `channels/base.py`).
"""

from __future__ import annotations

import asyncio
import logging

from ember.channels.base import Channel, MessageHandler, SendKind

_log = logging.getLogger("ember.channels.mux")


class ChannelMux(Channel):
    """Implements the `Channel` protocol over a set of real adapters."""

    name: str = "mux"

    def __init__(
        self,
        *,
        adapters: dict[str, Channel],
        default: str,
        scheduler: str | None = None,
    ) -> None:
        if not adapters:
            raise ValueError("ChannelMux requires at least one adapter")
        if default not in adapters:
            raise ValueError(f"default adapter {default!r} not in adapters")
        if scheduler is not None and scheduler not in adapters:
            raise ValueError(f"scheduler adapter {scheduler!r} not in adapters")
        self._adapters = dict(adapters)
        self._default = default
        self._scheduler = scheduler if scheduler is not None else default

    def _route(self, conversation_key: str) -> Channel:
        ns, sep, _rest = conversation_key.partition(":")
        if not sep:
            return self._adapters[self._default]
        adapter = self._adapters.get(ns)
        if adapter is None:
            raise LookupError(
                f"no channel adapter for namespace {ns!r} (conversation key {conversation_key!r})"
            )
        return adapter

    async def start(self, on_message: MessageHandler) -> None:
        """Start every adapter with the same handler.

        Returns (or raises) as soon as the first adapter's `start`
        finishes — a dead gateway should take the process down exactly
        as it did pre-mux — after cancelling and awaiting the rest.
        """
        tasks = [
            asyncio.create_task(adapter.start(on_message), name=f"channel:{ns}")
            for ns, adapter in self._adapters.items()
        ]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            # The mux itself was cancelled (shutdown): propagate the
            # cancellation to every adapter before re-raising.
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()  # re-raise the first finisher's exception, if any

    async def stop(self) -> None:
        """Stop all adapters; attempt every one even if an earlier stop
        raises, then re-raise the first failure."""
        first: Exception | None = None
        for ns, adapter in self._adapters.items():
            try:
                await adapter.stop()
            except Exception as e:
                _log.exception("stop failed for channel %s", ns)
                if first is None:
                    first = e
        if first is not None:
            raise first

    async def send(
        self,
        conversation_key: str,
        content: str,
        *,
        kind: SendKind = "reply",
    ) -> list[str]:
        return await self._route(conversation_key).send(conversation_key, content, kind=kind)

    async def send_attachments(
        self,
        conversation_key: str,
        paths: list[str],
    ) -> None:
        await self._route(conversation_key).send_attachments(conversation_key, paths)

    async def set_typing(self, conversation_key: str, on: bool = True) -> None:
        await self._route(conversation_key).set_typing(conversation_key, on)

    # Liveness surface (v16, FC-32): routed by key namespace exactly like
    # `send` — a siri key lands on SiriChannel's inherited no-ops, so the
    # agent's activity machinery needs no channel-specific branches here.

    async def typing_started(self, conversation_key: str) -> None:
        await self._route(conversation_key).typing_started(conversation_key)

    async def typing_stopped(self, conversation_key: str) -> None:
        await self._route(conversation_key).typing_stopped(conversation_key)

    async def post_status(self, conversation_key: str, text: str) -> str | None:
        return await self._route(conversation_key).post_status(conversation_key, text)

    async def edit_status(self, conversation_key: str, native_id: str, text: str) -> None:
        await self._route(conversation_key).edit_status(conversation_key, native_id, text)

    async def delete_status(self, conversation_key: str, native_id: str) -> None:
        await self._route(conversation_key).delete_status(conversation_key, native_id)

    async def notify_scheduler(self, content: str) -> None:
        await self._adapters[self._scheduler].notify_scheduler(content)
