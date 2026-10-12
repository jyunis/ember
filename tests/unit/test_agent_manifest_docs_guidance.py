"""`agent._user_text_with_manifest` — docs_file guidance line (FC-36).

Properties under test:

- A PDF attachment (`application/pdf`) and an image attachment
  (`image/*`) each append the exact extra guidance line, positioned
  after the existing guidance line and immediately before the closing
  `]`.
- A `.txt` attachment and the no-attachment case are byte-identical to
  today's behaviour (BC-4 / BC-5). The expected strings are reproduced
  from the documented manifest format — not computed by calling the
  function twice.
- Mixed attachments (txt + pdf) get the line exactly once.

Documented choice: the existing guidance line text is pinned here
verbatim (it was previously only asserted by substring in
`test_agent_attachment_manifest.py`); if it is ever reworded on purpose
both files need the change.
"""

from __future__ import annotations

from ember.agent import _user_text_with_manifest
from ember.channels.base import ChannelMessage, InboundAttachment

DOCS_LINE = (
    "To file it permanently into the user's documents, call `docs_file` with the path "
    "(only when asked to file/save/keep it)."
)
FILE_LINE = "File attachments: use `Read` (built-in) with the path above to view the contents."
IMAGE_LINE = (
    "Image attachments: use `Read` (built-in) with the path above to "
    "view image content — Read renders images natively. To generate "
    "or edit an image, call `generate_image` and pass the path in "
    "`reference_images` to base output on the user's image."
)


def _msg(text: str, *attachments: InboundAttachment) -> ChannelMessage:
    return ChannelMessage(
        channel="discord",
        conversation_key="thread-1",
        user_id="100",
        text=text,
        message_id="m1",
        attachments=tuple(attachments),
    )


def _att(name: str, mime: str, size: int) -> InboundAttachment:
    return InboundAttachment(
        filename=name, path=f"/data/uploads/thread-1/m1/{name}", content_type=mime, size=size
    )


class TestDocsGuidanceLine:
    def test_pdf_gets_exact_line_before_closing_bracket(self) -> None:
        out = _user_text_with_manifest(
            _msg("file this", _att("receipt.pdf", "application/pdf", 2048))
        )
        expected = (
            "file this\n\n"
            "[Attachments:\n"
            "- /data/uploads/thread-1/m1/receipt.pdf (application/pdf, 2 KB)\n"
            "\n"
            f"{FILE_LINE}\n"
            f"{DOCS_LINE}\n"
            "]"
        )
        assert out == expected

    def test_image_gets_exact_line_before_closing_bracket(self) -> None:
        out = _user_text_with_manifest(_msg("keep this", _att("scan.png", "image/png", 512)))
        expected = (
            "keep this\n\n"
            "[Attachments:\n"
            "- /data/uploads/thread-1/m1/scan.png (image/png, 512 B)\n"
            "\n"
            f"{IMAGE_LINE}\n"
            f"{DOCS_LINE}\n"
            "]"
        )
        assert out == expected

    def test_line_position_after_guidance_before_bracket(self) -> None:
        out = _user_text_with_manifest(_msg("x", _att("a.pdf", "application/pdf", 10)))
        lines = out.split("\n")
        assert lines[-1] == "]"
        assert lines[-2] == DOCS_LINE
        assert lines[-3] == FILE_LINE

    def test_mixed_txt_and_pdf_gets_line_once(self) -> None:
        out = _user_text_with_manifest(
            _msg("both", _att("notes.txt", "text/plain", 100), _att("a.pdf", "application/pdf", 10))
        )
        assert out.count(DOCS_LINE) == 1
        assert out.endswith(f"{DOCS_LINE}\n]")

    def test_content_type_case_insensitive_for_images(self) -> None:
        out = _user_text_with_manifest(_msg("x", _att("a.JPG", "IMAGE/JPEG", 10)))
        assert out.count(DOCS_LINE) == 1


class TestByteStableOtherwise:
    def test_txt_attachment_unchanged(self) -> None:
        out = _user_text_with_manifest(_msg("look at this", _att("a.txt", "text/plain", 100)))
        expected = (
            "look at this\n\n"
            "[Attachments:\n"
            "- /data/uploads/thread-1/m1/a.txt (text/plain, 100 B)\n"
            "\n"
            f"{FILE_LINE}\n"
            "]"
        )
        assert out == expected
        assert "docs_file" not in out

    def test_octet_stream_attachment_unchanged(self) -> None:
        out = _user_text_with_manifest(_msg("bin", _att("f.bin", "", 1024)))
        expected = (
            "bin\n\n"
            "[Attachments:\n"
            "- /data/uploads/thread-1/m1/f.bin (application/octet-stream, 1 KB)\n"
            "\n"
            f"{FILE_LINE}\n"
            "]"
        )
        assert out == expected

    def test_no_attachments_returns_text_unchanged(self) -> None:
        assert _user_text_with_manifest(_msg("hello world")) == "hello world"
        assert _user_text_with_manifest(_msg("")) == ""

    def test_empty_text_with_pdf_manifest_stands_alone(self) -> None:
        out = _user_text_with_manifest(_msg("", _att("a.pdf", "application/pdf", 10)))
        assert out.startswith("[Attachments:\n")
        assert out.endswith(f"{DOCS_LINE}\n]")
