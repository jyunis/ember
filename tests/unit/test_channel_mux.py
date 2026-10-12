"""ChannelMux: one Channel-shaped object multiplexing several adapters.

Contract under test (design doc #55, SIRI-TLC-3 — mux totality):

- Every conversation key routes to exactly one adapter. A `"<ns>:<rest>"`
  key goes to the adapter registered under `<ns>` with the FULL key passed
  through unchanged; a bare key (no colon) goes to the `default` adapter;
  a colon key with an unregistered prefix raises `LookupError` — never a
  best-effort post to the default adapter, never a silent drop.
- `notify_scheduler` always reaches the designated scheduler adapter.
- `start` fans one handler out to every adapter and resolves as soon as
  the first adapter's `start` returns or raises, cancelling the rest.
- `stop` attempts every adapter's stop even when one raises, then
  re-raises the first exception.
"""

from __future__ import annotations

import asyncio

import pytest

from ember.channels.base import Channel, ChannelMessage
from ember.channels.mux import ChannelMux
from tests.fakes.fake_channel import FakeChannel


async def _handler(msg: ChannelMessage) -> None:  # pragma: no cover - never invoked
    return None


class _BlockingStart(FakeChannel):
    """Adapter whose `start` records the handler then blocks until cancelled."""

    def __init__(self) -> None:
        super().__init__()
        self.start_entered = asyncio.Event()
        self.start_cancelled = False

    async def start(self, on_message) -> None:
        self._handler = on_message
        self.start_entered.set()
        try:
            await asyncio.Event().wait()  # blocks forever
        except asyncio.CancelledError:
            self.start_cancelled = True
            raise


class _FinishWhen(FakeChannel):
    """Adapter whose `start` returns once `release` is set."""

    def __init__(self, release: asyncio.Event) -> None:
        super().__init__()
        self._release = release

    async def start(self, on_message) -> None:
        self._handler = on_message
        await self._release.wait()


class _RaiseWhen(FakeChannel):
    """Adapter whose `start` raises once `release` is set."""

    def __init__(self, release: asyncio.Event) -> None:
        super().__init__()
        self._release = release

    async def start(self, on_message) -> None:
        self._handler = on_message
        await self._release.wait()
        raise RuntimeError("connect failed")


class _RaisingStop(FakeChannel):
    def __init__(self) -> None:
        super().__init__()
        self.stop_attempted = False

    async def stop(self) -> None:
        self.stop_attempted = True
        raise RuntimeError("stop exploded")


# --- Construction ----------------------------------------------------------


class TestConstruction:
    def test_is_a_channel_named_mux(self) -> None:
        mux = ChannelMux(adapters={"fake": FakeChannel()}, default="fake")
        assert mux.name == "mux"
        assert isinstance(mux, Channel)

    def test_unknown_default_raises(self) -> None:
        with pytest.raises(ValueError):
            ChannelMux(adapters={"fake": FakeChannel()}, default="discord")

    def test_unknown_scheduler_raises(self) -> None:
        with pytest.raises(ValueError):
            ChannelMux(adapters={"fake": FakeChannel()}, default="fake", scheduler="discord")


# --- Routing (SIRI-TLC-3) --------------------------------------------------


class TestRouting:
    def _mux(self) -> tuple[ChannelMux, FakeChannel, FakeChannel]:
        default = FakeChannel()
        siri = FakeChannel()
        mux = ChannelMux(adapters={"fake": default, "siri": siri}, default="fake")
        return mux, default, siri

    async def test_bare_key_send_goes_to_default_unchanged(self) -> None:
        mux, default, siri = self._mux()
        ids = await mux.send("123", "hello", kind="status")
        assert [(m.conversation_key, m.content, m.kind) for m in default.sent] == [
            ("123", "hello", "status")
        ]
        assert ids == [default.sent[0].message_id]  # return value passes through
        assert siri.sent == []

    async def test_bare_key_set_typing_and_attachments_go_to_default(self) -> None:
        mux, default, siri = self._mux()
        await mux.set_typing("123", True)
        await mux.send_attachments("123", ["/tmp/a.png"])
        assert default.typing == {"123": True}
        assert default.sent_attachments == [("123", ["/tmp/a.png"])]
        assert siri.typing == {}
        assert siri.sent_attachments == []

    async def test_prefixed_key_routes_to_that_adapter_with_full_key(self) -> None:
        mux, default, siri = self._mux()
        await mux.send("siri:123", "spoken")
        await mux.set_typing("siri:123", True)
        await mux.send_attachments("siri:123", ["/tmp/b.png"])
        assert [(m.conversation_key, m.content) for m in siri.sent] == [("siri:123", "spoken")]
        assert siri.typing == {"siri:123": True}
        assert siri.sent_attachments == [("siri:123", ["/tmp/b.png"])]
        assert default.sent == []

    async def test_default_namespace_prefix_also_routes_with_full_key(self) -> None:
        mux, default, _ = self._mux()
        await mux.send("fake:9", "hi")
        assert [(m.conversation_key, m.content) for m in default.sent] == [("fake:9", "hi")]

    async def test_unregistered_prefix_raises_and_no_adapter_receives(self) -> None:
        mux, default, siri = self._mux()
        with pytest.raises(LookupError):
            await mux.send("tg:9", "lost?")
        with pytest.raises(LookupError):
            await mux.set_typing("tg:9", True)
        with pytest.raises(LookupError):
            await mux.send_attachments("tg:9", ["/tmp/c.png"])
        for adapter in (default, siri):
            assert adapter.sent == []
            assert adapter.typing == {}
            assert adapter.sent_attachments == []


# --- notify_scheduler ------------------------------------------------------


class TestNotifyScheduler:
    async def test_goes_to_explicit_scheduler_adapter_only(self) -> None:
        discord = FakeChannel()
        siri = FakeChannel()
        mux = ChannelMux(
            adapters={"discord": discord, "siri": siri},
            default="siri",
            scheduler="discord",
        )
        await mux.notify_scheduler("job done")
        assert discord.scheduler_notifications == ["job done"]
        assert siri.scheduler_notifications == []

    async def test_scheduler_defaults_to_the_default_adapter(self) -> None:
        discord = FakeChannel()
        siri = FakeChannel()
        mux = ChannelMux(adapters={"discord": discord, "siri": siri}, default="discord")
        await mux.notify_scheduler("job done")
        assert discord.scheduler_notifications == ["job done"]
        assert siri.scheduler_notifications == []


# --- start / stop ----------------------------------------------------------


class TestStart:
    async def test_single_adapter_is_a_passthrough(self) -> None:
        only = FakeChannel()
        mux = ChannelMux(adapters={"fake": only}, default="fake")
        await asyncio.wait_for(mux.start(_handler), timeout=2)
        assert only._handler is _handler

    async def test_first_finisher_returns_and_others_are_cancelled(self) -> None:
        blocker = _BlockingStart()
        # The finisher waits for the blocker to have entered its `start`
        # body, so both adapters demonstrably received the handler before
        # the mux resolves.
        finisher = _FinishWhen(blocker.start_entered)
        mux = ChannelMux(adapters={"fake": finisher, "block": blocker}, default="fake")

        await asyncio.wait_for(mux.start(_handler), timeout=2)

        assert finisher._handler is _handler
        assert blocker._handler is _handler  # same handler fanned out to all
        assert blocker.start_cancelled  # settled before mux.start returned

    async def test_exception_from_first_finisher_propagates(self) -> None:
        blocker = _BlockingStart()
        raiser = _RaiseWhen(blocker.start_entered)
        mux = ChannelMux(adapters={"boom": raiser, "block": blocker}, default="boom")

        with pytest.raises(RuntimeError, match="connect failed"):
            await asyncio.wait_for(mux.start(_handler), timeout=2)
        assert blocker.start_cancelled


class TestStop:
    async def test_all_stops_attempted_and_first_exception_reraised(self) -> None:
        first = FakeChannel()
        middle = _RaisingStop()
        last = FakeChannel()
        mux = ChannelMux(adapters={"a": first, "b": middle, "c": last}, default="a")

        with pytest.raises(RuntimeError, match="stop exploded"):
            await mux.stop()

        assert first._stopped
        assert middle.stop_attempted
        assert last._stopped  # attempted despite the earlier failure
