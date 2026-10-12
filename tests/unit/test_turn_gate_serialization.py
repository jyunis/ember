"""TurnGate `serialize_conversations` flag (design doc #55, SIRI-TLC-1).

With `serialize_conversations=True`, at most one handler invocation is
executing at any instant across ALL conversation keys — turns for
different keys queue behind each other. With the flag off (the default),
different keys still run concurrently, which the baseline test pins.

Per-key semantics are unchanged by the flag: debounce coalescing still
merges same-key bursts, and `interrupt=True` still cancels a same-key
turn. A turn parked WAITING for the cross-conversation lock must remain
interruptible by a new same-key message without losing the waiter's text,
and a cancelled waiter must not leave the lock held.

Every test passes `serialize_conversations` explicitly so the whole file
fails with TypeError until the keyword exists.
"""

from __future__ import annotations

import asyncio

from ember.channels.base import ChannelMessage
from ember.channels.gate import TurnGate

_DEBOUNCE = 0.05
_MAX_DEBOUNCE = 0.2


def _msg(text: str, *, key: str = "t1", mid: str = "1") -> ChannelMessage:
    return ChannelMessage(
        channel="discord",
        conversation_key=key,
        user_id="u1",
        text=text,
        message_id=mid,
    )


def _gate(handler, **kw) -> TurnGate:
    # Zero debounce by default: turns contend for the cross-conversation
    # lock immediately, and tests synchronize on events, not sleeps.
    kw.setdefault("debounce_s", 0.0)
    kw.setdefault("max_debounce_s", _MAX_DEBOUNCE)
    return TurnGate(handler, **kw)


class _Probe:
    """Handler recording every turn; blocks turns whose text was `block`ed.

    Tracks live/max concurrency so tests can assert on overlap without
    timing games. `started(text)` returns an event set when a turn whose
    merged text equals `text` begins executing.
    """

    def __init__(self, hold: float = 0.0) -> None:
        self.hold = hold
        self.turns: list[ChannelMessage] = []
        self.cancelled: list[str] = []
        self.completed: list[str] = []
        self.concurrent = 0
        self.max_concurrent = 0
        self._gates: dict[str, asyncio.Event] = {}
        self._started: dict[str, asyncio.Event] = {}

    def block(self, text: str) -> None:
        self._gates[text] = asyncio.Event()

    def release(self, text: str) -> None:
        self._gates[text].set()

    def started(self, text: str) -> asyncio.Event:
        return self._started.setdefault(text, asyncio.Event())

    async def __call__(self, msg: ChannelMessage) -> None:
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        self.turns.append(msg)
        self.started(msg.text).set()
        try:
            gate = self._gates.get(msg.text)
            if gate is not None:
                await gate.wait()
            elif self.hold:
                await asyncio.sleep(self.hold)
        except asyncio.CancelledError:
            self.cancelled.append(msg.text)
            raise
        else:
            self.completed.append(msg.text)
        finally:
            self.concurrent -= 1


# --- Baseline: flag off ----------------------------------------------------


class TestFlagOffBaseline:
    async def test_different_keys_run_concurrently(self) -> None:
        probe = _Probe()
        probe.block("a-turn")
        gate = _gate(probe, serialize_conversations=False)

        a = asyncio.create_task(gate.submit(_msg("a-turn", key="a", mid="1")))
        await asyncio.wait_for(probe.started("a-turn").wait(), timeout=2)

        # While a's turn is still blocked, b's turn starts anyway.
        b = asyncio.create_task(gate.submit(_msg("b-turn", key="b", mid="2")))
        await asyncio.wait_for(probe.started("b-turn").wait(), timeout=2)
        assert probe.max_concurrent == 2

        probe.release("a-turn")
        await asyncio.wait_for(asyncio.gather(a, b), timeout=5)


# --- Cross-conversation serialization (SIRI-TLC-1) -------------------------


class TestSerializeConversations:
    async def test_second_key_waits_until_first_key_finishes(self) -> None:
        probe = _Probe()
        probe.block("a-turn")
        gate = _gate(probe, serialize_conversations=True)

        a = asyncio.create_task(gate.submit(_msg("a-turn", key="a", mid="1")))
        await asyncio.wait_for(probe.started("a-turn").wait(), timeout=2)

        b = asyncio.create_task(gate.submit(_msg("b-turn", key="b", mid="2")))
        await asyncio.sleep(0.02)  # give b every chance to (wrongly) start
        assert not probe.started("b-turn").is_set()
        assert [t.text for t in probe.turns] == ["a-turn"]

        probe.release("a-turn")
        await asyncio.wait_for(asyncio.gather(a, b), timeout=5)
        assert [t.text for t in probe.turns] == ["a-turn", "b-turn"]
        assert probe.max_concurrent == 1

    async def test_many_interleaved_keys_never_overlap(self) -> None:
        probe = _Probe(hold=0.005)
        gate = _gate(probe, serialize_conversations=True)

        msgs = [_msg(f"m{i}", key=f"k{i % 3}", mid=str(i)) for i in range(9)]
        await asyncio.wait_for(
            asyncio.gather(*(gate.submit(m) for m in msgs)),
            timeout=5,
        )

        assert probe.max_concurrent == 1
        assert {t.conversation_key for t in probe.turns} == {"k0", "k1", "k2"}
        assert probe.cancelled == []


# --- Per-key semantics unchanged by the flag -------------------------------


class TestPerKeySemanticsUnchanged:
    async def test_same_key_burst_still_coalesces_into_one_turn(self) -> None:
        probe = _Probe()
        gate = _gate(probe, debounce_s=_DEBOUNCE, serialize_conversations=True)

        t1 = asyncio.create_task(gate.submit(_msg("first", mid="1")))
        await asyncio.sleep(_DEBOUNCE / 4)
        t2 = asyncio.create_task(gate.submit(_msg("and also this", mid="2")))
        await asyncio.wait_for(asyncio.gather(t1, t2), timeout=5)

        assert len(probe.turns) == 1
        assert probe.turns[0].text == "first\n\nand also this"

    async def test_same_key_interrupt_of_a_running_turn_still_works(self) -> None:
        seen: list[str] = []

        async def _notify(key: str) -> None:
            seen.append(key)

        probe = _Probe()
        probe.block("first")
        gate = _gate(probe, serialize_conversations=True, interrupt=True, on_interrupt=_notify)

        first = asyncio.create_task(gate.submit(_msg("first", mid="1")))
        await asyncio.wait_for(probe.started("first").wait(), timeout=2)
        second = asyncio.create_task(gate.submit(_msg("second", mid="2")))
        await asyncio.wait_for(asyncio.gather(first, second), timeout=5)

        assert probe.cancelled == ["first"]
        # The plain handler never persisted, so the cancelled batch is
        # re-queued and merged into the next turn.
        assert [t.text for t in probe.turns] == ["first", "first\n\nsecond"]
        assert seen == ["t1"]


# --- Interrupt while waiting for the cross-conversation lock ---------------


class TestInterruptWhileWaitingForLock:
    async def test_waiting_turn_is_cancellable_and_its_text_survives(self) -> None:
        probe = _Probe()
        probe.block("a-turn")
        gate = _gate(probe, serialize_conversations=True, interrupt=True)

        # a's turn holds the cross-conversation lock.
        a = asyncio.create_task(gate.submit(_msg("a-turn", key="a", mid="1")))
        await asyncio.wait_for(probe.started("a-turn").wait(), timeout=2)

        # b1's turn parks waiting for the lock (debounce is zero).
        b1 = asyncio.create_task(gate.submit(_msg("b-one", key="b", mid="2")))
        await asyncio.sleep(0.02)
        assert not probe.started("b-one").is_set()

        # b2 arrives for the same key: it must cancel the WAITING b1 turn
        # without losing b1's text.
        b2 = asyncio.create_task(gate.submit(_msg("b-two", key="b", mid="3")))
        await asyncio.sleep(0.02)

        probe.release("a-turn")
        await asyncio.wait_for(asyncio.gather(a, b1, b2), timeout=5)

        b_turns = [t for t in probe.turns if t.conversation_key == "b"]
        assert len(b_turns) == 1, "exactly one handler call for key b"
        assert "b-one" in b_turns[0].text
        assert "b-two" in b_turns[0].text
        # a's turn was never collateral damage (interrupt is same-key only).
        assert "a-turn" in probe.completed

    async def test_no_lock_leak_after_an_interrupted_waiter(self) -> None:
        probe = _Probe()
        probe.block("a-turn")
        gate = _gate(probe, serialize_conversations=True, interrupt=True)

        a = asyncio.create_task(gate.submit(_msg("a-turn", key="a", mid="1")))
        await asyncio.wait_for(probe.started("a-turn").wait(), timeout=2)
        b1 = asyncio.create_task(gate.submit(_msg("b-one", key="b", mid="2")))
        await asyncio.sleep(0.02)
        b2 = asyncio.create_task(gate.submit(_msg("b-two", key="b", mid="3")))
        await asyncio.sleep(0.02)
        probe.release("a-turn")
        await asyncio.wait_for(asyncio.gather(a, b1, b2), timeout=5)

        # If the cancelled waiter left the cross-conversation lock held,
        # this submit would hang and trip the timeout.
        await asyncio.wait_for(gate.submit(_msg("c-turn", key="c", mid="4")), timeout=2)
        assert "c-turn" in probe.completed
