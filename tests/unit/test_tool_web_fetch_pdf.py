"""`web_fetch` PDF handling.

- `application/pdf` responses are run through `_pdf_to_text` and returned
  as text with `content_type == "application/pdf"` (previously a
  "unsupported content-type" ToolError — the model hit this on manuals and
  recipe PDFs).
- An `application/octet-stream` response whose URL path ends in `.pdf` is
  treated the same way; one whose path does not is still rejected.
- Oversized PDFs are refused (a truncated PDF is unparseable).
- `max_chars` truncation applies to the extracted text.

`_pdf_to_text` itself shells out to poppler's `pdftotext`, which the dev
host may lack, so it is stubbed here; the real binary is exercised only in
the container.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from ember.errors import ToolError
from ember.tools import web as web_mod
from ember.tools.web import WebFetchTool

_PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"


def _install(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    real = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _stub_pdftotext(
    monkeypatch: pytest.MonkeyPatch, text: str = "Page one text\n\x0cPage two"
) -> list[bytes]:
    seen: list[bytes] = []

    async def fake(data: bytes) -> str:
        seen.append(data)
        return text

    monkeypatch.setattr(web_mod, "_pdf_to_text", fake)
    return seen


class TestPdfFetch:
    async def test_application_pdf_is_extracted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(
            monkeypatch,
            lambda r: httpx.Response(
                200, content=_PDF_BYTES, headers={"content-type": "application/pdf"}
            ),
        )
        seen = _stub_pdftotext(monkeypatch)
        out = (await WebFetchTool().call({"url": "https://example.com/manual"})).output
        assert seen == [_PDF_BYTES]
        assert out["content"] == "Page one text\n\x0cPage two"
        assert out["content_type"] == "application/pdf"
        assert out["title"] is None
        assert out["may_need_browser"] is False
        assert out["status_code"] == 200

    async def test_octet_stream_with_pdf_path_is_extracted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(
            monkeypatch,
            lambda r: httpx.Response(
                200, content=_PDF_BYTES, headers={"content-type": "application/octet-stream"}
            ),
        )
        _stub_pdftotext(monkeypatch, "hello")
        out = (
            await WebFetchTool().call({"url": "https://example.com/files/Dough.PDF?dl=1"})
        ).output
        assert out["content"] == "hello"
        assert out["content_type"] == "application/pdf"

    async def test_octet_stream_without_pdf_path_still_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(
            monkeypatch,
            lambda r: httpx.Response(
                200, content=b"\x00\x01", headers={"content-type": "application/octet-stream"}
            ),
        )
        seen = _stub_pdftotext(monkeypatch)
        with pytest.raises(ToolError, match="unsupported content-type"):
            await WebFetchTool().call({"url": "https://example.com/blob.bin"})
        assert seen == []

    async def test_oversized_pdf_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(web_mod, "_PDF_MAX_BYTES", 16)
        _install(
            monkeypatch,
            lambda r: httpx.Response(
                200, content=_PDF_BYTES, headers={"content-type": "application/pdf"}
            ),
        )
        seen = _stub_pdftotext(monkeypatch)
        with pytest.raises(ToolError, match="too large"):
            await WebFetchTool().call({"url": "https://example.com/big.pdf"})
        assert seen == []

    async def test_max_chars_truncates_extracted_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(
            monkeypatch,
            lambda r: httpx.Response(
                200, content=_PDF_BYTES, headers={"content-type": "application/pdf"}
            ),
        )
        _stub_pdftotext(monkeypatch, "x" * 500)
        out = (
            await WebFetchTool().call({"url": "https://example.com/a.pdf", "max_chars": 100})
        ).output
        assert out["content"].startswith("x" * 100)
        assert len(out["content"]) <= 100 + 50

    async def test_pdftotext_failure_maps_to_tool_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(
            monkeypatch,
            lambda r: httpx.Response(
                200, content=_PDF_BYTES, headers={"content-type": "application/pdf"}
            ),
        )

        async def boom(data: bytes) -> str:
            raise ToolError("web_fetch: pdftotext failed: Syntax Error")

        monkeypatch.setattr(web_mod, "_pdf_to_text", boom)
        with pytest.raises(ToolError, match="pdftotext failed"):
            await WebFetchTool().call({"url": "https://example.com/a.pdf"})
