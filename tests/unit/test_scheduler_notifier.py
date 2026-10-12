"""Concrete `RunNotifier` (UoW-5 / TLC-7).

The implementer ships a `ChannelRunNotifier` (under
`ember.scheduler.notifier`) that posts non-ok run states (and any
operational notifications) to the `notify_scheduler` surface on the
configured `Channel`.

These tests:
- The notifier is constructed against a `Channel`.
- Calling `notify` posts the message to `Channel.notify_scheduler`.
- The notifier wraps content with a recognizable prefix so the user
  can see at a glance it's a scheduler-channel event (e.g. ❌ / ⏱️).
  Spec is loose on the exact glyph; we assert "❌" or "⏱️" or some
  visible unicode marker is present in non-empty output, since that's
  what the architecture doc's run wrapper sketch showed.
"""

from __future__ import annotations


from tests.fakes.fake_channel import FakeChannel


def _import_notifier():
    from ember.scheduler.notifier import ChannelRunNotifier

    return ChannelRunNotifier


class TestChannelRunNotifier:
    async def test_posts_to_notify_scheduler(self) -> None:
        channel = FakeChannel()
        ChannelRunNotifier = _import_notifier()
        notifier = ChannelRunNotifier(channel)

        await notifier.notify("demo:tick failed: ValueError: kaboom")

        # The notifier wrote to the scheduler-facing surface, not to a
        # conversation thread.
        assert channel.sent == []
        assert len(channel.scheduler_notifications) == 1
        assert "demo:tick" in channel.scheduler_notifications[0]

    async def test_message_carries_visible_marker(self) -> None:
        # The user should see a recognizable prefix on the scheduler
        # channel. The architecture doc sketch uses ❌ for errors and
        # ⏱️ for timeouts; the notifier's `notify` is the single
        # entrypoint, so it should pass content through with whatever
        # marker the caller provides — but it MUST not strip non-ascii.
        channel = FakeChannel()
        ChannelRunNotifier = _import_notifier()
        notifier = ChannelRunNotifier(channel)

        await notifier.notify("❌ demo:tick failed")
        await notifier.notify("⏱️ demo:tick timed out")

        assert "❌" in channel.scheduler_notifications[0]
        assert "⏱️" in channel.scheduler_notifications[1]

    async def test_multiple_notifications_appended_in_order(self) -> None:
        channel = FakeChannel()
        ChannelRunNotifier = _import_notifier()
        notifier = ChannelRunNotifier(channel)

        await notifier.notify("first")
        await notifier.notify("second")
        await notifier.notify("third")

        assert channel.scheduler_notifications == ["first", "second", "third"]
