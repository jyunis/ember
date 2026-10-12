"""Discord attachment intake — BC-1, BC-2, BC-3, BC-9.

Drive the real `DiscordChannel._intake_attachments` against fake
`discord.Attachment` objects, mocking the HTTP download via a stub
`httpx.AsyncClient`. Asserts:

- BC-2: saved files land at
  `data_dir/uploads/<conversation_key>/<message_id>/<safe_name>` and the
  sanitized name matches `[A-Za-z0-9._-]+`. Collisions get `__1`, `__2`
  suffixes preserving the extension.
- BC-3: oversize files (> MAX_UPLOAD_BYTES) are skipped; per-message
  cap (MAX_UPLOADS_PER_MESSAGE = 8) drops excess attachments.
- BC-9: skipped/oversize files do NOT appear in the saved attachment
  tuple (they never enter the manifest).
- BC-1: best-effort — partial failures don't prevent successful saves
  from flowing through.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ember.channels.discord import (
    MAX_UPLOAD_BYTES,
    MAX_UPLOADS_PER_MESSAGE,
    DiscordChannel,
)
from ember.config import Settings


# --- Fakes -----------------------------------------------------------------


@dataclass
class FakeDiscordAttachment:
    filename: str
    url: str
    size: int
    content_type: str = "application/octet-stream"
    id: int = 0


class _FakeResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        return None


class _FakeAsyncClient:
    """A drop-in stand-in for `httpx.AsyncClient` used by the intake path.

    Routes per-URL responses configured at construction time. Any URL
    not pre-registered yields a "synthetic" response of the matching
    attachment's `size` bytes — but tests always pre-register what they
    care about so failures are explicit.
    """

    def __init__(
        self,
        *,
        responses: dict[str, bytes] | None = None,
        errors: dict[str, Exception] | None = None,
        default_payload: bytes = b"X",
    ) -> None:
        self._responses = responses or {}
        self._errors = errors or {}
        self._default_payload = default_payload

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        if url in self._errors:
            raise self._errors[url]
        if url in self._responses:
            return _FakeResponse(self._responses[url])
        return _FakeResponse(self._default_payload)


def _settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")
    monkeypatch.setenv("DISCORD_CONVERSATIONS_CHANNEL_ID", "2")
    monkeypatch.setenv("DISCORD_SCHEDULER_CHANNEL_ID", "3")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    s = Settings()  # type: ignore[call-arg]
    return s


@pytest.fixture
def channel_and_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    s = _settings(monkeypatch, tmp_path)
    # Ensure data_dir exists so the intake path can mkdir under it.
    s.data_dir.mkdir(parents=True, exist_ok=True)
    ch = DiscordChannel(s)
    return ch, s


def _patch_httpx(
    monkeypatch: pytest.MonkeyPatch,
    *,
    payloads: dict[str, bytes] | None = None,
    errors: dict[str, Exception] | None = None,
) -> None:
    """Replace `httpx.AsyncClient` symbol used inside discord.py module."""

    def _factory(*args: Any, **kwargs: Any) -> _FakeAsyncClient:
        return _FakeAsyncClient(responses=payloads or {}, errors=errors or {})

    import ember.channels.discord as dmod

    monkeypatch.setattr(dmod.httpx, "AsyncClient", _factory)


def _make_message(*, message_id: int, attachments: list[FakeDiscordAttachment]) -> Any:
    """Mimic the parts of `discord.Message` the intake path reads."""
    return SimpleNamespace(id=message_id, attachments=attachments)


# --- BC-2 sanitization + path layout --------------------------------------


class TestSanitizationAndPathLayout:
    async def test_safe_filename_pattern(
        self, channel_and_settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ch, s = channel_and_settings
        url = "https://cdn.discord/file1"
        atts = [
            FakeDiscordAttachment(
                filename="weird name with spaces & punctuation!.txt",
                url=url,
                size=4,
                content_type="text/plain",
            )
        ]
        _patch_httpx(monkeypatch, payloads={url: b"data"})

        msg = _make_message(message_id=42, attachments=atts)
        saved, status = await ch._intake_attachments(msg, "thread-7")

        assert len(saved) == 1, status
        # Each saved file's name matches `[A-Za-z0-9._-]+`.
        on_disk_name = Path(saved[0].path).name
        assert re.fullmatch(r"[A-Za-z0-9._-]+", on_disk_name), on_disk_name

    async def test_save_path_layout(
        self, channel_and_settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ch, s = channel_and_settings
        url = "https://cdn.discord/p"
        atts = [
            FakeDiscordAttachment(filename="hello.txt", url=url, size=4, content_type="text/plain")
        ]
        _patch_httpx(monkeypatch, payloads={url: b"data"})

        msg = _make_message(message_id=99, attachments=atts)
        saved, _ = await ch._intake_attachments(msg, "conv-A")

        # Path is exactly data_dir/uploads/<conv>/<msg>/<safe>.
        expected_dir = s.data_dir / "uploads" / "conv-A" / "99"
        p = Path(saved[0].path)
        assert p.parent == expected_dir, p
        assert p.is_file()

    async def test_collision_suffixes(
        self, channel_and_settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ch, s = channel_and_settings
        # Two attachments whose sanitized names collide. (Both raw names
        # sanitize identically since spaces become `_`.)
        url1 = "https://cdn.discord/c1"
        url2 = "https://cdn.discord/c2"
        url3 = "https://cdn.discord/c3"
        atts = [
            FakeDiscordAttachment(filename="my file.txt", url=url1, size=2),
            FakeDiscordAttachment(filename="my file.txt", url=url2, size=2),
            FakeDiscordAttachment(filename="my file.txt", url=url3, size=2),
        ]
        _patch_httpx(monkeypatch, payloads={url1: b"a", url2: b"b", url3: b"c"})

        msg = _make_message(message_id=5, attachments=atts)
        saved, _ = await ch._intake_attachments(msg, "k")

        names = [Path(a.path).name for a in saved]
        assert len(names) == 3, names
        # Distinct names.
        assert len(set(names)) == 3, names
        # Collisions get __1, __2 suffixes inserted before the extension.
        # The first one keeps its base; the others carry __1 / __2.
        # We don't pin which order — but per BC-2 collisions get
        # `__1`, `__2`, ... preserving the extension.
        assert any(n.endswith(".txt") for n in names), names
        suffixed = [n for n in names if "__" in n]
        assert len(suffixed) >= 2, names
        for n in suffixed:
            assert n.endswith(".txt"), n
            # Suffix is `__<digit>` before the extension.
            stem = n[: -len(".txt")]
            assert re.search(r"__\d+$", stem), stem


# --- BC-3 caps -------------------------------------------------------------


class TestCaps:
    async def test_oversize_attachment_skipped(
        self, channel_and_settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ch, _ = channel_and_settings
        url_ok = "https://cdn.discord/ok"
        url_big = "https://cdn.discord/big"
        atts = [
            FakeDiscordAttachment(filename="ok.txt", url=url_ok, size=10),
            FakeDiscordAttachment(filename="big.bin", url=url_big, size=MAX_UPLOAD_BYTES + 1),
        ]
        # Don't actually serve the big file — it must never be downloaded.
        _patch_httpx(monkeypatch, payloads={url_ok: b"good"})

        msg = _make_message(message_id=1, attachments=atts)
        saved, status = await ch._intake_attachments(msg, "k")

        # BC-9: oversize file is NOT in the saved tuple.
        names = [a.filename for a in saved]
        assert "big.bin" not in names
        assert "ok.txt" in names
        # Status mentions the skipped file.
        assert any("big.bin" in line for line in status), status

    async def test_overcount_attachments_skipped(
        self, channel_and_settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ch, _ = channel_and_settings
        # Build MAX + 2 attachments with distinct names and URLs.
        n = MAX_UPLOADS_PER_MESSAGE + 2
        urls = {f"u{i}": b"X" for i in range(n)}
        atts = [FakeDiscordAttachment(filename=f"f{i}.txt", url=f"u{i}", size=1) for i in range(n)]
        _patch_httpx(monkeypatch, payloads=urls)

        msg = _make_message(message_id=2, attachments=atts)
        saved, status = await ch._intake_attachments(msg, "k")

        # Exactly MAX accepted.
        assert len(saved) == MAX_UPLOADS_PER_MESSAGE, len(saved)
        # The two excess files are reported in the status.
        assert any("cap" in line.lower() for line in status), status


# --- BC-1 partial-failure tolerance ---------------------------------------


class TestPartialFailureTolerance:
    async def test_one_failed_one_succeeded(
        self, channel_and_settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import httpx

        ch, _ = channel_and_settings
        url_good = "https://cdn.discord/g"
        url_bad = "https://cdn.discord/b"
        atts = [
            FakeDiscordAttachment(filename="good.txt", url=url_good, size=4),
            FakeDiscordAttachment(filename="bad.txt", url=url_bad, size=4),
        ]
        _patch_httpx(
            monkeypatch,
            payloads={url_good: b"good"},
            errors={url_bad: httpx.HTTPError("boom")},
        )

        msg = _make_message(message_id=10, attachments=atts)
        saved, status = await ch._intake_attachments(msg, "k")

        # Successful one flows through; failed one does not.
        names = [a.filename for a in saved]
        assert names == ["good.txt"], names
        # Failure surfaces in the status reply.
        assert any("bad.txt" in line for line in status), status

    async def test_no_attachments_returns_empty(self, channel_and_settings) -> None:
        ch, _ = channel_and_settings
        msg = _make_message(message_id=99, attachments=[])
        saved, status = await ch._intake_attachments(msg, "k")
        assert saved == ()
        assert status == []


# --- BC-9 oversize never enters saved tuple -------------------------------


class TestOversizeNeverEntersSaved:
    async def test_no_partial_download_for_oversize(
        self, channel_and_settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ch, s = channel_and_settings
        urls_seen: list[str] = []

        # A custom client that records GETs so we can assert the big URL
        # was never fetched.
        class _RecordingClient(_FakeAsyncClient):
            async def get(self, url: str, **kwargs: Any) -> _FakeResponse:
                urls_seen.append(url)
                return await super().get(url, **kwargs)

        def _factory(*args: Any, **kwargs: Any) -> _RecordingClient:
            return _RecordingClient(responses={"good_url": b"x"})

        import ember.channels.discord as dmod

        monkeypatch.setattr(dmod.httpx, "AsyncClient", _factory)

        atts = [
            FakeDiscordAttachment(filename="g.txt", url="good_url", size=1),
            FakeDiscordAttachment(filename="huge.bin", url="big_url", size=MAX_UPLOAD_BYTES * 4),
        ]
        msg = _make_message(message_id=11, attachments=atts)
        await ch._intake_attachments(msg, "k")

        # Oversize URL must never have been GET-ed (BC-3: never partially
        # downloaded).
        assert "big_url" not in urls_seen, urls_seen
