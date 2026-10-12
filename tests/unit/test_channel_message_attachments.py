"""ChannelMessage / InboundAttachment surface (BC-1).

The attachments field defaults to an empty tuple so existing call sites
that don't pass it continue to work. InboundAttachment is a frozen
dataclass with the four documented fields.
"""

from __future__ import annotations

import dataclasses

from ember.channels.base import ChannelMessage, InboundAttachment


def _make_msg(**overrides):
    base = dict(
        channel="discord",
        conversation_key="thread-1",
        user_id="42",
        text="hello",
        message_id="m1",
    )
    base.update(overrides)
    return ChannelMessage(**base)


class TestChannelMessageAttachmentsDefault:
    def test_attachments_defaults_to_empty_tuple(self) -> None:
        msg = _make_msg()
        assert msg.attachments == ()

    def test_attachments_default_is_a_tuple_not_a_list(self) -> None:
        # BC-1 requires the default to be the empty tuple specifically,
        # so existing code paths that build messages without attachments
        # remain byte-stable.
        msg = _make_msg()
        assert isinstance(msg.attachments, tuple)

    def test_attachments_default_does_not_share_mutable_state(self) -> None:
        # Frozen tuple → can't be mutated; two separate messages don't
        # share a single default object accidentally.
        a = _make_msg(message_id="m1")
        b = _make_msg(message_id="m2")
        # Same content (empty), but identity comparison would also pass
        # for a singleton — what we actually care about is that you cannot
        # accumulate state by using the default.
        assert a.attachments == ()
        assert b.attachments == ()


class TestInboundAttachmentShape:
    def test_has_four_documented_fields(self) -> None:
        att = InboundAttachment(
            filename="x.png",
            path="/data/uploads/1/2/x.png",
            content_type="image/png",
            size=1234,
        )
        assert att.filename == "x.png"
        assert att.path == "/data/uploads/1/2/x.png"
        assert att.content_type == "image/png"
        assert att.size == 1234

    def test_is_frozen(self) -> None:
        att = InboundAttachment(filename="x", path="/p", content_type="", size=0)
        try:
            att.filename = "other"  # type: ignore[misc]
        except dataclasses.FrozenInstanceError:
            return
        except AttributeError:
            return
        raise AssertionError("InboundAttachment should be frozen")

    def test_empty_content_type_is_allowed(self) -> None:
        # The docstring says content_type is "" when the channel didn't
        # report one — that must not raise.
        att = InboundAttachment(filename="f.bin", path="/p/f.bin", content_type="", size=0)
        assert att.content_type == ""


class TestChannelMessageCarriesAttachments:
    def test_can_construct_with_attachments(self) -> None:
        att = InboundAttachment(
            filename="a.txt", path="/p/a.txt", content_type="text/plain", size=10
        )
        msg = _make_msg(attachments=(att,))
        assert msg.attachments == (att,)
        assert msg.attachments[0].filename == "a.txt"

    def test_attachments_field_is_tuple_typed(self) -> None:
        att1 = InboundAttachment(filename="a", path="/a", content_type="", size=1)
        att2 = InboundAttachment(filename="b", path="/b", content_type="", size=2)
        msg = _make_msg(attachments=(att1, att2))
        assert isinstance(msg.attachments, tuple)
        assert len(msg.attachments) == 2
