"""Inbound turn gate: serialization, coalescing debounce, interruption.

Three guarantees under test:

- **Serialization.** Two messages for one conversation never run
  overlapping turns, no matter how they interleave. This is the
  correctness half — concurrent turns corrupt the claude_cli session
  file, race the consume-once pre-turn short-circuits, and clobber the
  single global `bridge.active` handle.
- **Coalescing.** Messages arriving inside the debounce window fold into
  one turn carrying every message's text and attachments, in order.
- **Interruption.** With `interrupt=True`, a message arriving mid-turn
  cancels that turn. The delicate part is not losing the cancelled
  turn's message: if it had already persisted, the store carries it and
  the batch must NOT be replayed; if it hadn't, the batch must be.

Timings use a tiny debounce (tens of ms) so the suite stays fast; the
production defaults live in `Settings`.
"""

from __future__ import annotations

import asyncio

import pytest

from ember.channels.base import ChannelMessage, InboundAttachment
from ember.channels.gate import TurnGate, merge_messages
from ember.context.interruption import mark_user_message_persisted

_DEBOUNCE = 0.05
_MAX_DEBOUNCE = 0.2


def _msg(text: str, *, key: str = "t1", mid: str = "1", attachments=()) -> ChannelMessage:
    return ChannelMessage(
        channel="discord",
        conversation_key=key,
        user_id="u1",
        text=text,
        message_id=mid,
        attachments=attachments,
    )


def _att(name: str) -> InboundAttachment:
    return InboundAttachment(filename=name, path=f"/tmp/{name}", content_type="", size=1)


class _Recorder:
    """Handler that records each turn and can be made to block."""

    def __init__(self, hold: float = 0.0) -> None:
        self.turns: list[ChannelMessage] = []
        self.hold = hold
        self.concurrent = 0
        self.max_concurrent = 0

    async def __call__(self, msg: ChannelMessage) -> None:
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        self.turns.append(msg)
        try:
            if self.hold:
                await asyncio.sleep(self.hold)
        finally:
            self.concurrent -= 1


def _gate(handler, **kw) -> TurnGate:
    kw.setdefault("debounce_s", _DEBOUNCE)
    kw.setdefault("max_debounce_s", _MAX_DEBOUNCE)
    return TurnGate(handler, **kw)


# --- merge_messages --------------------------------------------------------


class TestMergeMessages:
    def test_single_message_passes_through_identically(self) -> None:
        m = _msg("hello")
        assert merge_messages([m]) is m

    def test_texts_join_in_arrival_order(self) -> None:
        merged = merge_messages([_msg("first"), _msg("second"), _msg("third")])
        assert merged.text == "first\n\nsecond\n\nthird"

    def test_empty_texts_are_dropped(self) -> None:
        # An attachment-only message carries no text; it must not leave a
        # dangling blank paragraph in the merged prompt.
        merged = merge_messages([_msg("look at this"), _msg("", attachments=(_att("a.png"),))])
        assert merged.text == "look at this"

    def test_attachments_concatenate_in_order(self) -> None:
        merged = merge_messages(
            [
                _msg("a", attachments=(_att("one.png"),)),
                _msg("b", attachments=(_att("two.png"), _att("three.png"))),
            ]
        )
        assert [a.filename for a in merged.attachments] == ["one.png", "two.png", "three.png"]

    def test_identity_fields_come_from_newest_message(self) -> None:
        merged = merge_messages([_msg("a", mid="10"), _msg("b", mid="11")])
        assert merged.message_id == "11"
        assert merged.conversation_key == "t1"
        assert merged.channel == "discord"


# --- Coalescing ------------------------------------------------------------


class TestCoalescing:
    async def test_burst_inside_window_runs_one_turn(self) -> None:
        rec = _Recorder()
        gate = _gate(rec)
        t1 = asyncio.create_task(gate.submit(_msg("first", mid="1")))
        await asyncio.sleep(_DEBOUNCE / 4)
        t2 = asyncio.create_task(gate.submit(_msg("and also this", mid="2")))
        await asyncio.gather(t1, t2)

        assert len(rec.turns) == 1
        assert rec.turns[0].text == "first\n\nand also this"

    async def test_message_after_window_runs_its_own_turn(self) -> None:
        rec = _Recorder()
        gate = _gate(rec)
        await gate.submit(_msg("first", mid="1"))
        await gate.submit(_msg("later", mid="2"))

        assert [t.text for t in rec.turns] == ["first", "later"]

    async def test_window_extends_while_messages_keep_arriving(self) -> None:
        rec = _Recorder()
        gate = _gate(rec)
        tasks = []
        for i in range(3):
            tasks.append(asyncio.create_task(gate.submit(_msg(f"m{i}", mid=str(i)))))
            await asyncio.sleep(_DEBOUNCE * 0.6)
        await asyncio.gather(*tasks)

        assert len(rec.turns) == 1
        assert rec.turns[0].text == "m0\n\nm1\n\nm2"

    async def test_max_debounce_caps_an_endless_burst(self) -> None:
        """Someone typing continuously must not defer their turn forever."""
        rec = _Recorder()
        gate = _gate(rec)
        stop = asyncio.Event()
        tasks: list[asyncio.Task] = []

        async def _typist() -> None:
            i = 0
            while not stop.is_set():
                tasks.append(asyncio.create_task(gate.submit(_msg(f"m{i}", mid=str(i)))))
                i += 1
                await asyncio.sleep(_DEBOUNCE * 0.5)

        typist = asyncio.create_task(_typist())
        # Well past max_debounce: the first turn must have fired by now.
        await asyncio.sleep(_MAX_DEBOUNCE * 2)
        stop.set()
        await typist
        assert rec.turns, "turn never started under a continuous burst"
        stop.set()
        await asyncio.gather(*tasks)

    async def test_debounce_zero_starts_immediately(self) -> None:
        rec = _Recorder()
        gate = _gate(rec, debounce_s=0.0)
        await gate.submit(_msg("go"))
        assert [t.text for t in rec.turns] == ["go"]


# --- Serialization ---------------------------------------------------------


class TestSerialization:
    async def test_turns_never_overlap_for_one_conversation(self) -> None:
        # The handler holds far longer than the debounce, so without a
        # lock the second submit would start while the first is running.
        rec = _Recorder(hold=_DEBOUNCE * 6)
        gate = _gate(rec)
        t1 = asyncio.create_task(gate.submit(_msg("first", mid="1")))
        await asyncio.sleep(_DEBOUNCE * 3)  # first turn is now in flight
        t2 = asyncio.create_task(gate.submit(_msg("second", mid="2")))
        await asyncio.gather(t1, t2)

        assert rec.max_concurrent == 1
        assert [t.text for t in rec.turns] == ["first", "second"]

    async def test_messages_arriving_mid_turn_coalesce_into_the_next_one(self) -> None:
        rec = _Recorder(hold=_DEBOUNCE * 6)
        gate = _gate(rec)
        t1 = asyncio.create_task(gate.submit(_msg("first", mid="1")))
        await asyncio.sleep(_DEBOUNCE * 3)
        t2 = asyncio.create_task(gate.submit(_msg("second", mid="2")))
        t3 = asyncio.create_task(gate.submit(_msg("third", mid="3")))
        await asyncio.gather(t1, t2, t3)

        assert len(rec.turns) == 2
        assert rec.turns[1].text == "second\n\nthird"

    async def test_different_conversations_run_concurrently(self) -> None:
        rec = _Recorder(hold=_DEBOUNCE * 4)
        gate = _gate(rec)
        await asyncio.gather(
            gate.submit(_msg("a", key="t1", mid="1")),
            gate.submit(_msg("b", key="t2", mid="2")),
        )
        assert rec.max_concurrent == 2
        assert {t.conversation_key for t in rec.turns} == {"t1", "t2"}

    async def test_handler_exception_does_not_wedge_the_conversation(self) -> None:
        """A raising handler must still release the lock and clean up."""
        calls: list[str] = []

        async def _boom(msg: ChannelMessage) -> None:
            calls.append(msg.text)
            raise RuntimeError("handler exploded")

        gate = _gate(_boom)
        try:
            await gate.submit(_msg("first", mid="1"))
        except RuntimeError:
            pass
        try:
            await gate.submit(_msg("second", mid="2"))
        except RuntimeError:
            pass
        assert calls == ["first", "second"]


# --- Bookkeeping -----------------------------------------------------------


class TestBookkeeping:
    async def test_per_key_state_is_released_after_the_last_submitter(self) -> None:
        """Locks/pending maps must not grow without bound over uptime."""
        rec = _Recorder(hold=_DEBOUNCE * 4)
        gate = _gate(rec)
        await asyncio.gather(
            gate.submit(_msg("a", key="t1", mid="1")),
            gate.submit(_msg("b", key="t1", mid="2")),
            gate.submit(_msg("c", key="t2", mid="3")),
        )
        assert gate._locks == {}
        assert gate._pending == {}
        assert gate._inflight == {}


# --- Interruption ----------------------------------------------------------


class _Interruptible:
    """Handler that blocks until cancelled, recording what it saw.

    `persist` mirrors the agent: when True the handler marks its user
    message persisted before blocking, which is the signal the gate uses
    to decide whether the cancelled batch needs re-queueing.
    """

    def __init__(
        self,
        *,
        persist: bool,
        hold: float = _DEBOUNCE * 4,
        holds: dict[str, float] | None = None,
    ) -> None:
        self.persist = persist
        # Default hold is short so an *uninterrupted* turn finishes fast;
        # `holds` pins a longer one for turns a test needs to keep alive.
        self.hold = hold
        self.holds = holds or {}
        self.turns: list[ChannelMessage] = []
        self.cancelled: list[str] = []
        self.completed: list[str] = []
        self.started = asyncio.Event()

    async def __call__(self, msg: ChannelMessage) -> None:
        self.turns.append(msg)
        if self.persist:
            mark_user_message_persisted()
        self.started.set()
        try:
            await asyncio.sleep(self.holds.get(msg.text, self.hold))
        except asyncio.CancelledError:
            self.cancelled.append(msg.text)
            raise
        self.completed.append(msg.text)


async def _interrupt_scenario(*, persist: bool, notifier=None):
    """Run a turn, interrupt it mid-flight, return (handler, gate)."""
    handler = _Interruptible(persist=persist)
    gate = _gate(handler, interrupt=True, on_interrupt=notifier)
    first = asyncio.create_task(gate.submit(_msg("first", mid="1")))
    await asyncio.wait_for(handler.started.wait(), timeout=2)
    second = asyncio.create_task(gate.submit(_msg("second", mid="2")))
    await asyncio.wait_for(asyncio.gather(first, second), timeout=5)
    return handler, gate


class TestInterruption:
    async def test_new_message_cancels_the_running_turn(self) -> None:
        handler, _ = await _interrupt_scenario(persist=True)
        assert handler.cancelled == ["first"]
        assert "first" not in handler.completed

    async def test_persisted_turn_is_not_replayed(self) -> None:
        """The store already has the message; replaying would duplicate it."""
        handler, _ = await _interrupt_scenario(persist=True)
        assert [t.text for t in handler.turns] == ["first", "second"]

    async def test_unpersisted_turn_is_requeued_into_the_next_turn(self) -> None:
        """Cancelled before the store row landed — the text must survive."""
        handler, _ = await _interrupt_scenario(persist=False)
        assert [t.text for t in handler.turns] == ["first", "first\n\nsecond"]

    async def test_interrupt_notifier_receives_the_conversation_key(self) -> None:
        seen: list[str] = []

        async def _notify(key: str) -> None:
            seen.append(key)

        await _interrupt_scenario(persist=True, notifier=_notify)
        assert seen == ["t1"]

    async def test_notifier_failure_does_not_break_the_gate(self) -> None:
        async def _boom(key: str) -> None:
            raise RuntimeError("discord is down")

        handler, gate = await _interrupt_scenario(persist=True, notifier=_boom)
        assert [t.text for t in handler.turns] == ["first", "second"]
        assert gate._running == {}

    async def test_disabled_interrupt_queues_instead_of_cancelling(self) -> None:
        handler = _Interruptible(persist=True, hold=_DEBOUNCE * 4)
        gate = _gate(handler, interrupt=False)
        first = asyncio.create_task(gate.submit(_msg("first", mid="1")))
        await asyncio.wait_for(handler.started.wait(), timeout=2)
        second = asyncio.create_task(gate.submit(_msg("second", mid="2")))
        await asyncio.wait_for(asyncio.gather(first, second), timeout=5)

        assert handler.cancelled == []
        assert handler.completed == ["first", "second"]

    async def test_only_the_targeted_conversation_is_interrupted(self) -> None:
        # t2's turn is pinned long so it is demonstrably still running
        # when t1 gets interrupted.
        handler = _Interruptible(persist=True, holds={"other": 5.0})
        gate = _gate(handler, interrupt=True)
        other = asyncio.create_task(gate.submit(_msg("other", key="t2", mid="9")))
        await asyncio.wait_for(handler.started.wait(), timeout=2)
        handler.started.clear()
        first = asyncio.create_task(gate.submit(_msg("first", key="t1", mid="1")))
        await asyncio.wait_for(handler.started.wait(), timeout=2)
        second = asyncio.create_task(gate.submit(_msg("second", key="t1", mid="2")))
        await asyncio.wait_for(asyncio.gather(first, second), timeout=5)

        assert handler.cancelled == ["first"]  # t2's turn untouched
        assert not other.done()
        other.cancel()
        with pytest.raises(asyncio.CancelledError):
            await other

    async def test_shutdown_cancellation_propagates(self) -> None:
        """A cancel we did NOT request must not be swallowed as an interrupt.

        Otherwise `submit` would swallow shutdown cancellation and the
        process would hang waiting on the channel task.
        """
        handler = _Interruptible(persist=True)
        gate = _gate(handler, interrupt=True)
        task = asyncio.create_task(gate.submit(_msg("first", mid="1")))
        await asyncio.wait_for(handler.started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert handler.cancelled == ["first"]
        assert gate._running == {}

    async def test_state_is_released_after_an_interrupt(self) -> None:
        _, gate = await _interrupt_scenario(persist=False)
        assert gate._locks == {}
        assert gate._pending == {}
        assert gate._inflight == {}
        assert gate._running == {}
