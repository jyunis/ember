"""Sanity-check the FakeChannel test infrastructure."""

from __future__ import annotations

import pytest

from ember.channels.base import Channel, ChannelMessage
from tests.fakes.fake_channel import FakeChannel, SentMessage


def test_fake_channel_satisfies_protocol() -> None:
    fc = FakeChannel()
    assert isinstance(fc, Channel)
    assert fc.name == "fake"


async def test_inject_before_start_raises() -> None:
    fc = FakeChannel()
    msg = ChannelMessage(
        channel="fake",
        conversation_key="thread-1",
        user_id="u1",
        text="hi",
        message_id="m1",
    )
    with pytest.raises(RuntimeError):
        await fc.inject(msg)


async def test_inject_routes_to_registered_handler() -> None:
    fc = FakeChannel()
    received: list[ChannelMessage] = []

    async def handler(m: ChannelMessage) -> None:
        received.append(m)

    await fc.start(handler)
    msg = ChannelMessage("fake", "k", "u", "hi", "m1")
    await fc.inject(msg)
    assert received == [msg]


async def test_send_records_kind() -> None:
    fc = FakeChannel()
    await fc.send("k", "hello", kind="status")
    await fc.send("k", "answer", kind="final")
    assert fc.sent == [
        SentMessage("k", "hello", "status", "1"),
        SentMessage("k", "answer", "final", "2"),
    ]


async def test_send_returns_native_message_ids() -> None:
    # send() returns the native message ids it created so the agent loop
    # can map them to the assistant `messages` row (UoW-20 / FC-20).
    fc = FakeChannel()
    ids = await fc.send("k", "answer", kind="final")
    assert ids == [fc.sent[-1].message_id]


async def test_set_typing_records_per_conversation() -> None:
    fc = FakeChannel()
    await fc.set_typing("k1", on=True)
    await fc.set_typing("k2", on=False)
    assert fc.typing == {"k1": True, "k2": False}


# --- FC-32 liveness surface (v16, doc #57) --------------------------------


async def test_status_post_edit_delete_round_trip_with_ordered_log() -> None:
    fc = FakeChannel()
    native_id = await fc.post_status("k", "⏳ working — 20s")
    assert native_id is not None
    assert fc.live_statuses == {native_id: "⏳ working — 20s"}

    await fc.edit_status("k", native_id, "⏳ working — 25s · 1 tool call")
    assert fc.live_statuses[native_id] == "⏳ working — 25s · 1 tool call"

    await fc.delete_status("k", native_id)
    assert fc.live_statuses == {}
    assert fc.liveness_ops() == ["post_status", "edit_status", "delete_status"]


async def test_typing_started_and_stopped_are_logged_in_order() -> None:
    fc = FakeChannel()
    await fc.typing_started("k")
    await fc.typing_started("k")  # keepalive refresh
    await fc.typing_stopped("k")
    assert fc.liveness_ops() == ["typing_started", "typing_started", "typing_stopped"]


async def test_status_native_ids_do_not_collide_with_send_ids() -> None:
    fc = FakeChannel()
    (send_id,) = await fc.send("k", "reply")
    status_id = await fc.post_status("k", "⏳ working")
    assert status_id != send_id


async def test_fail_next_injects_n_failures_then_recovers() -> None:
    fc = FakeChannel()
    native_id = await fc.post_status("k", "⏳")
    fc.fail_next("delete_status", times=1)
    with pytest.raises(RuntimeError):
        await fc.delete_status("k", native_id)
    # The failed attempt is logged so retries can be counted.
    attempts = [c for c in fc.liveness_calls if c.op == "delete_status"]
    assert [c.ok for c in attempts] == [False]
    await fc.delete_status("k", native_id)  # armed failures exhausted
    assert fc.live_statuses == {}


async def test_edit_or_delete_on_unknown_status_id_raises() -> None:
    fc = FakeChannel()
    with pytest.raises(KeyError):
        await fc.edit_status("k", "nope", "text")
    with pytest.raises(KeyError):
        await fc.delete_status("k", "nope")


async def test_fail_sends_containing_matcher_is_one_shot() -> None:
    fc = FakeChannel()
    fc.fail_sends_containing.append("BLOCK-TWO")
    await fc.send("k", "BLOCK-ONE")
    with pytest.raises(RuntimeError):
        await fc.send("k", "BLOCK-TWO")
    # Failed send never lands in `sent`, but is recorded for assertions.
    assert [s.content for s in fc.sent] == ["BLOCK-ONE"]
    assert fc.failed_sends == [("k", "BLOCK-TWO")]
    # One-shot: the same content goes through on retry.
    await fc.send("k", "BLOCK-TWO")
    assert [s.content for s in fc.sent] == ["BLOCK-ONE", "BLOCK-TWO"]
