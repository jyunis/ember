"""Gmail tool tests (FC-3, TLC-4, TLC-7) — v8 (Gmail), updated for commit 268c779.

Covers `ember.tools.gmail.{GmailListTool, GmailReadTool, GmailSendTool}`:

- FC-3 static surface (name, description, input_schema, requires_confirmation,
  Tool protocol).
- **Outbound safety (revised):** `gmail_send` uses a two-tier gate.
  Allowlisted recipients auto-send; everyone else routes through the
  TLC-3 human-approval flow via the conditional `needs_confirmation`
  hook the registry consults. A rolling 24h cap + audit log apply to
  every send. `_make_send_tool` defaults to a permissive allowlist
  covering the test domains (so happy-path `call()`s auto-send);
  the gate/cap paths are exercised in `TestGmailSendOutboundSafety`.
- TLC-4 secret hygiene: Gmail app password never appears in errors/logs.
- TLC-7 failure surfacing: typed errors for auth and transport failures.

Mocking strategy: monkeypatch the symbols that the implementer is expected
to have imported into `ember.tools.gmail`:
  - `MailBox` (from `imap_tools`) → fake context-manager class for IMAP reads.
  - `smtplib.SMTP` → fake class for SMTP sends.

Tester-flagged ambiguities (also in the report):

1. **`since_days` filter representation** — `imap-tools` accepts `date_gte`
   in `AND(...)` but the spec doesn't pin the exact filter shape. Tests
   assert that *some* date-related criteria flows through to the fetch
   call (a `since_days` argument is captured by the fake fetch and
   non-trivial); they do NOT pin the specific imap-tools API the
   implementer chose.

2. **`fetch` criteria capture** — to avoid pinning the exact `AND(...)`
   construction, the fake `fetch` accepts `*args, **kwargs` and stores
   them. Assertions look for any positional/kw arg that is non-default
   when the corresponding tool input is set (`unread_only`, `from_`,
   `since_days`).

3. **MIME multipart construction** — the spec says HTML vs plain text
   via `html=True`. Tests parse the captured raw bytes via
   `email.message_from_bytes(...)` and check `get_content_type()` /
   header presence rather than pinning the exact MIMEText/MIMEMultipart
   structure.

4. **Snippet length** — spec says "first ~200 chars". Tests assert
   `len(snippet) <= 250` (a 50-char slack for any "..." marker) and that
   it's non-empty for a 1k-char body.

5. **HTML stripping in snippet** — spec says "strip HTML if needed". Tests
   pass plain text (so `text` is set) AND a case where only `html` is set;
   for the second case, the snippet must not contain `<` or `>`.

6. **`message_id` return value** — spec says `{message_id, sent_at_iso}`.
   The implementer can choose to read the Message-ID header from the
   constructed MIME or generate one. Tests assert non-empty string only,
   not a specific format.

7. **AuthError vs ToolError on bad credentials** — `imap-tools` raises
   `imap_tools.errors.MailboxLoginError`; smtplib raises
   `smtplib.SMTPAuthenticationError`. Tests pin the typed `AuthError`
   surface — the implementer must catch the library-specific exception
   and translate.

8. **Message UID lookup failure** — spec says "UID not found → ToolError";
   tests pin `ToolError` (not `ToolRetryable`). The fake fetch returning
   an empty iterable is the failure shape.

9. **Default `from`-filter substring vs exact-match** — spec says "sender
   email or substring". Tests pass an email-shaped value and don't probe
   the exact match semantics; they only assert *some* `from`-criteria
   flows to fetch.

10. **`requires_confirmation` + conditional `needs_confirmation`.** These
    tests pin the `requires_confirmation` attribute and the
    `needs_confirmation(args)` predicate that the registry consults. The
    registry's end-to-end confirmation-prompt flow is a separate test
    surface (`test_registry_confirm.py`, `test_agent_tlc3.py`).
"""

from __future__ import annotations

import datetime
import email
import email.policy
import logging
import socket
from email.message import Message as EmailMessage
from typing import Any

import pytest


# Importing here is intentionally done at the module level so collection
# fails clearly when `ember.tools.gmail` doesn't exist yet (the expected
# baseline for the implementer).
from ember.errors import AuthError, ToolError, TransportError  # noqa: E402
from ember.tools.base import Tool, ToolResult  # noqa: E402
from ember.tools import gmail as gmail_module  # noqa: E402
from ember.tools.gmail import (  # noqa: E402
    GmailListTool,
    GmailReadTool,
    GmailSendTool,
)


# Sentinel password — must NEVER appear in any error message or log line.
SECRET_PASSWORD = "sk-gmail-XXX-SECRET-XXX-YYY"  # noqa: S105 — test sentinel
ADDRESS = "ember@example.com"


# ---------------------------------------------------------------------------
# Fake imap-tools.MailBox
# ---------------------------------------------------------------------------


class _FakeAttachment:
    def __init__(self, filename: str, content_type: str, payload: bytes) -> None:
        self.filename = filename
        self.content_type = content_type
        self.payload = payload
        self.size = len(payload)


class _FakeMessage:
    def __init__(
        self,
        *,
        uid: str,
        subject: str,
        from_: str,
        to: tuple[str, ...] = (),
        cc: tuple[str, ...] = (),
        date: datetime.datetime | None = None,
        text: str = "",
        html: str = "",
        flags: tuple[str, ...] = (),
        attachments: tuple[_FakeAttachment, ...] = (),
    ) -> None:
        self.uid = uid
        self.subject = subject
        self.from_ = from_
        self.to = to
        self.cc = cc
        self.date = date or datetime.datetime(2026, 4, 27, 12, 0, 0, tzinfo=datetime.timezone.utc)
        self.text = text
        self.html = html
        self.flags = flags
        self.attachments = list(attachments)


class _FakeMailBoxState:
    """Shared state across test-controlled MailBox instances.

    The tool may construct MailBox more than once per call (it shouldn't,
    but we don't pin that). We share state so the test can drive behavior
    regardless.
    """

    def __init__(self) -> None:
        self.messages: list[_FakeMessage] = []
        self.fetch_calls: list[dict[str, Any]] = []
        self.login_calls: list[dict[str, Any]] = []
        self.host: str | None = None
        self.login_raises: BaseException | None = None
        self.constructor_raises: BaseException | None = None
        self.fetch_raises: BaseException | None = None


def _install_fake_mailbox(
    monkeypatch: pytest.MonkeyPatch,
    state: _FakeMailBoxState,
):
    """Patch `ember.tools.gmail.MailBox` to a fake."""

    class _FakeMailBox:
        def __init__(self, host: str, *args: Any, **kwargs: Any) -> None:
            if state.constructor_raises is not None:
                raise state.constructor_raises
            state.host = host
            self._user: str | None = None
            self._pw: str | None = None
            self._folder: str = "INBOX"

        def login(self, user: str, password: str, initial_folder: str = "INBOX"):
            state.login_calls.append(
                {"user": user, "password": password, "initial_folder": initial_folder}
            )
            if state.login_raises is not None:
                raise state.login_raises
            self._user = user
            self._pw = password
            self._folder = initial_folder
            return self

        def __enter__(self):
            return self

        def __exit__(self, *exc: Any) -> bool:
            return False

        def fetch(self, *args: Any, **kwargs: Any):
            state.fetch_calls.append({"args": args, "kwargs": kwargs})
            if state.fetch_raises is not None:
                raise state.fetch_raises
            # Apply a uid filter if the caller passed `uid=...` kwarg
            # (gmail_read may use a UID-targeted fetch).
            uid_filter = kwargs.get("uid")
            results = list(state.messages)
            if uid_filter is not None:
                results = [m for m in results if m.uid == str(uid_filter)]
            return iter(results)

    monkeypatch.setattr(gmail_module, "MailBox", _FakeMailBox, raising=True)
    return state


# ---------------------------------------------------------------------------
# Fake smtplib.SMTP
# ---------------------------------------------------------------------------


class _FakeSMTPState:
    def __init__(self) -> None:
        self.starttls_calls = 0
        self.login_calls: list[tuple[str, str]] = []
        self.sendmail_calls: list[dict[str, Any]] = []
        self.quit_calls = 0
        self.host: str | None = None
        self.port: int | None = None
        self.connect_raises: BaseException | None = None
        self.login_raises: BaseException | None = None
        self.send_raises: BaseException | None = None


def _install_fake_smtp(monkeypatch: pytest.MonkeyPatch, state: _FakeSMTPState):
    class _FakeSMTP:
        def __init__(self, host: str, port: int = 587, timeout: float | None = None) -> None:
            if state.connect_raises is not None:
                raise state.connect_raises
            state.host = host
            state.port = port

        def ehlo(self, *args: Any, **kwargs: Any) -> tuple[int, bytes]:
            return (250, b"ok")

        def starttls(self, *args: Any, **kwargs: Any) -> tuple[int, bytes]:
            state.starttls_calls += 1
            return (220, b"go ahead")

        def login(self, user: str, password: str) -> tuple[int, bytes]:
            state.login_calls.append((user, password))
            if state.login_raises is not None:
                raise state.login_raises
            return (235, b"ok")

        def sendmail(
            self,
            from_addr: str,
            to_addrs: list[str] | str,
            msg: bytes | str,
        ) -> dict[str, Any]:
            if state.send_raises is not None:
                raise state.send_raises
            recips = [to_addrs] if isinstance(to_addrs, str) else list(to_addrs)
            state.sendmail_calls.append({"from": from_addr, "to": recips, "msg": msg})
            return {}

        def quit(self) -> tuple[int, bytes]:
            state.quit_calls += 1
            return (221, b"bye")

        def __enter__(self):
            return self

        def __exit__(self, *exc: Any) -> bool:
            try:
                self.quit()
            except Exception:
                pass
            return False

    # Patch the module's smtplib.SMTP attribute. The implementer is
    # expected to either `import smtplib` and use `smtplib.SMTP(...)` OR
    # `from smtplib import SMTP` and use `SMTP(...)`. We patch both
    # symbols defensively.
    if hasattr(gmail_module, "smtplib"):
        monkeypatch.setattr(gmail_module.smtplib, "SMTP", _FakeSMTP, raising=True)
    if hasattr(gmail_module, "SMTP"):
        monkeypatch.setattr(gmail_module, "SMTP", _FakeSMTP, raising=True)
    return state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_list_tool() -> GmailListTool:
    return GmailListTool(address=ADDRESS, app_password=SECRET_PASSWORD)


def _make_read_tool() -> GmailReadTool:
    return GmailReadTool(address=ADDRESS, app_password=SECRET_PASSWORD)


def _make_send_tool(
    *,
    allowed_recipients: list[str] | None = None,
    daily_cap: int = 20,
    log_path=None,
) -> GmailSendTool:
    # Outbound safety (commit 268c779): GmailSendTool now requires an
    # explicit allowlist + daily cap instead of TLC-3 confirmation. The
    # default allowlist covers the domains used across happy-path tests
    # (`@x.com`, `@y.com`, `@example.com`); individual tests can override
    # via kwargs to exercise the allowlist/cap enforcement paths.
    if allowed_recipients is None:
        allowed_recipients = ["@x.com", "@y.com", "@example.com"]
    return GmailSendTool(
        address=ADDRESS,
        app_password=SECRET_PASSWORD,
        allowed_recipients=allowed_recipients,
        daily_cap=daily_cap,
        log_path=log_path,
    )


def _msg_from_sendmail(msg: bytes | str) -> EmailMessage:
    """Parse a captured sendmail payload back into an email.message.Message."""
    if isinstance(msg, str):
        return email.message_from_string(msg)
    return email.message_from_bytes(msg)


def _flatten_text(message: EmailMessage) -> str:
    """Best-effort dump of all text parts (decoded) for substring assertions."""
    parts: list[str] = []
    if message.is_multipart():
        for part in message.walk():
            if part.is_multipart():
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                parts.append(payload.decode(charset, errors="replace"))
            except (LookupError, UnicodeDecodeError):
                parts.append(payload.decode("utf-8", errors="replace"))
    else:
        payload = message.get_payload(decode=True)
        if payload is None:
            payload_obj = message.get_payload()
            if isinstance(payload_obj, str):
                parts.append(payload_obj)
        else:
            charset = message.get_content_charset() or "utf-8"
            try:
                parts.append(payload.decode(charset, errors="replace"))
            except (LookupError, UnicodeDecodeError):
                parts.append(payload.decode("utf-8", errors="replace"))
    return "\n".join(parts)


def _assert_no_secret_anywhere(
    *,
    caplog: pytest.LogCaptureFixture,
    exc: BaseException | None = None,
) -> None:
    """Pin TLC-4: app password never leaks into logs or surfaced errors."""
    for record in caplog.records:
        msg = record.getMessage()
        assert SECRET_PASSWORD not in msg, f"secret leaked into log record: {record.name}: {msg!r}"
        # Also check formatted args.
        try:
            full = record.getMessage()
        except Exception:
            full = ""
        assert SECRET_PASSWORD not in full
        # Defensive: check all attrs.
        for attr in ("msg", "message", "exc_text"):
            v = getattr(record, attr, None)
            if isinstance(v, str):
                assert SECRET_PASSWORD not in v
    if exc is not None:
        assert SECRET_PASSWORD not in str(exc)
        assert SECRET_PASSWORD not in repr(exc)
        for arg in getattr(exc, "args", ()):
            assert SECRET_PASSWORD not in repr(arg)


# ===========================================================================
# Static surface (FC-3)
# ===========================================================================


class TestStaticSurfaceList:
    def test_name(self) -> None:
        assert GmailListTool.name == "gmail_list"

    def test_description_nonempty(self) -> None:
        assert isinstance(GmailListTool.description, str)
        assert GmailListTool.description.strip() != ""

    def test_not_destructive(self) -> None:
        assert GmailListTool.requires_confirmation is False

    def test_schema_shape(self) -> None:
        s = GmailListTool.input_schema
        assert s["type"] == "object"
        props = s["properties"]
        assert "label" in props
        assert props["label"].get("default") == "INBOX"
        assert "limit" in props
        assert props["limit"]["type"] == "integer"
        assert props["limit"].get("default") == 20
        assert props["limit"].get("minimum") == 1
        assert props["limit"].get("maximum") == 100
        assert "unread_only" in props
        assert props["unread_only"].get("default") is False
        assert "from_" in props
        assert "since_days" in props
        assert props["since_days"].get("minimum") == 1
        assert props["since_days"].get("maximum") == 365

    def test_protocol_compliance(self) -> None:
        assert isinstance(_make_list_tool(), Tool)


class TestStaticSurfaceRead:
    def test_name(self) -> None:
        assert GmailReadTool.name == "gmail_read"

    def test_description_nonempty(self) -> None:
        assert isinstance(GmailReadTool.description, str)
        assert GmailReadTool.description.strip() != ""

    def test_not_destructive(self) -> None:
        assert GmailReadTool.requires_confirmation is False

    def test_schema_shape(self) -> None:
        s = GmailReadTool.input_schema
        assert s["type"] == "object"
        props = s["properties"]
        assert "uid" in props
        assert props["uid"]["type"] == "string"
        assert "label" in props
        assert props["label"].get("default") == "INBOX"
        assert "max_body_chars" in props
        assert props["max_body_chars"]["type"] == "integer"
        assert props["max_body_chars"].get("default") == 20000
        assert props["max_body_chars"].get("minimum") == 100
        assert props["max_body_chars"].get("maximum") == 200000
        assert s["required"] == ["uid"]

    def test_protocol_compliance(self) -> None:
        assert isinstance(_make_read_tool(), Tool)


class TestStaticSurfaceSend:
    def test_name(self) -> None:
        assert GmailSendTool.name == "gmail_send"

    def test_description_nonempty(self) -> None:
        assert isinstance(GmailSendTool.description, str)
        assert GmailSendTool.description.strip() != ""

    def test_send_requires_confirmation(self) -> None:
        # Human-approval flow (post-268c779 revision): GmailSendTool now
        # rides the TLC-3 gate again, but *conditionally* — the allowlist
        # became the auto-send set and everyone else routes through the
        # registry's `needs_confirmation` hook. `requires_confirmation`
        # must be True for the gate to consult that hook at all.
        assert GmailSendTool.requires_confirmation is True

    def test_schema_shape(self) -> None:
        s = GmailSendTool.input_schema
        assert s["type"] == "object"
        props = s["properties"]
        assert props["to"]["type"] == "string"
        assert "subject" in props
        assert "body" in props
        assert "cc" in props
        assert "bcc" in props
        assert "in_reply_to" in props
        assert "html" in props
        assert props["html"].get("default") is False
        assert set(s["required"]) == {"to", "subject", "body"}

    def test_protocol_compliance(self) -> None:
        assert isinstance(_make_send_tool(), Tool)


# ===========================================================================
# gmail_list — happy paths and filters
# ===========================================================================


def _three_messages() -> list[_FakeMessage]:
    return [
        _FakeMessage(
            uid="101",
            subject="First",
            from_="alice@example.com",
            text="Hello world. " * 30,
            flags=(),  # unread (no \\Seen)
            date=datetime.datetime(2026, 4, 25, 9, 0, tzinfo=datetime.timezone.utc),
        ),
        _FakeMessage(
            uid="102",
            subject="Second",
            from_="bob@example.com",
            text="Body 2",
            flags=("\\Seen",),
            attachments=(_FakeAttachment("foo.pdf", "application/pdf", b"%PDF-1.4..."),),
            date=datetime.datetime(2026, 4, 26, 10, 0, tzinfo=datetime.timezone.utc),
        ),
        _FakeMessage(
            uid="103",
            subject="Third",
            from_="carol@example.com",
            text="",
            html="<p>HTML <b>only</b> body</p>" + "x" * 500,
            flags=("\\Seen",),
            date=datetime.datetime(2026, 4, 27, 11, 0, tzinfo=datetime.timezone.utc),
        ),
    ]


class TestGmailListHappyPath:
    async def test_three_messages_returned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = _FakeMailBoxState()
        state.messages = _three_messages()
        _install_fake_mailbox(monkeypatch, state)

        result = await _make_list_tool().call({})

        assert isinstance(result, ToolResult)
        out = result.output
        assert "messages" in out
        msgs = out["messages"]
        assert len(msgs) == 3
        # Order preserved.
        assert [m["uid"] for m in msgs] == ["101", "102", "103"]
        # Each entry has the documented fields.
        for m in msgs:
            assert "uid" in m
            assert "subject" in m
            assert "from" in m  # NB: "from", not "from_"
            assert "date_iso" in m
            assert "snippet" in m
            assert "unread" in m
            assert "has_attachments" in m

    async def test_unread_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = _FakeMailBoxState()
        state.messages = _three_messages()
        _install_fake_mailbox(monkeypatch, state)

        result = await _make_list_tool().call({})

        msgs = {m["uid"]: m for m in result.output["messages"]}
        assert msgs["101"]["unread"] is True  # no \\Seen flag
        assert msgs["102"]["unread"] is False  # has \\Seen
        assert msgs["103"]["unread"] is False

    async def test_has_attachments(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = _FakeMailBoxState()
        state.messages = _three_messages()
        _install_fake_mailbox(monkeypatch, state)

        result = await _make_list_tool().call({})

        msgs = {m["uid"]: m for m in result.output["messages"]}
        assert msgs["101"]["has_attachments"] is False
        assert msgs["102"]["has_attachments"] is True
        assert msgs["103"]["has_attachments"] is False

    async def test_snippet_capped_and_html_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = _FakeMailBoxState()
        state.messages = _three_messages()
        _install_fake_mailbox(monkeypatch, state)

        result = await _make_list_tool().call({})

        msgs = {m["uid"]: m for m in result.output["messages"]}
        # Long plain-text body → snippet is non-empty and capped near 200.
        snip_101 = msgs["101"]["snippet"]
        assert isinstance(snip_101, str)
        assert len(snip_101) > 0
        # "first ~200 chars" — give 50-char slack for any "..." marker.
        assert len(snip_101) <= 250
        # HTML-only body → snippet must not contain raw tags.
        snip_103 = msgs["103"]["snippet"]
        assert "<p>" not in snip_103
        assert "<b>" not in snip_103
        assert ">" not in snip_103 or snip_103.count(">") < snip_103.count("<") + 1

    async def test_date_iso_format(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = _FakeMailBoxState()
        state.messages = _three_messages()
        _install_fake_mailbox(monkeypatch, state)

        result = await _make_list_tool().call({})

        for m in result.output["messages"]:
            iso = m["date_iso"]
            # ISO 8601: parsable, includes a date and a time-of-day separator.
            parsed = datetime.datetime.fromisoformat(iso)
            assert parsed.year == 2026
            # UTC: must carry a tzinfo or end with 'Z'.
            assert parsed.tzinfo is not None or iso.endswith("Z")


class TestGmailListFilters:
    async def test_label_passthrough(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = _FakeMailBoxState()
        state.messages = []
        _install_fake_mailbox(monkeypatch, state)

        await _make_list_tool().call({"label": "Purchases"})

        assert state.login_calls, "login should have been called"
        last = state.login_calls[-1]
        assert last["initial_folder"] == "Purchases"

    async def test_default_label_is_inbox(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = _FakeMailBoxState()
        state.messages = []
        _install_fake_mailbox(monkeypatch, state)

        await _make_list_tool().call({})

        assert state.login_calls[-1]["initial_folder"] == "INBOX"

    async def test_limit_passthrough(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = _FakeMailBoxState()
        state.messages = _three_messages()
        _install_fake_mailbox(monkeypatch, state)

        await _make_list_tool().call({"limit": 5})

        assert state.fetch_calls, "fetch should have been called"
        # Don't pin keyword name; check both args and kwargs.
        last = state.fetch_calls[-1]
        flat = list(last["args"]) + list(last["kwargs"].values())
        assert 5 in flat, f"expected limit=5 to appear in fetch call: {last!r}"

    async def test_unread_only_adds_seen_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = _FakeMailBoxState()
        state.messages = _three_messages()
        _install_fake_mailbox(monkeypatch, state)

        await _make_list_tool().call({"unread_only": True})

        # Don't pin the AND() shape. Look for *some* indication of
        # unseen-only criteria in the captured fetch call (positional or
        # keyword). A repr that mentions "seen" or "unseen" is enough.
        assert state.fetch_calls
        last = state.fetch_calls[-1]
        rendered = repr(last)
        assert "seen" in rendered.lower() or "unseen" in rendered.lower(), (
            f"expected unseen-criteria in fetch call: {last!r}"
        )

    async def test_from_filter_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = _FakeMailBoxState()
        state.messages = _three_messages()
        _install_fake_mailbox(monkeypatch, state)

        await _make_list_tool().call({"from_": "alice@example.com"})

        assert state.fetch_calls
        last = state.fetch_calls[-1]
        rendered = repr(last)
        # The from-filter value should reach the fetch criteria somehow.
        assert "alice@example.com" in rendered, f"expected from-filter to reach fetch: {last!r}"

    async def test_since_days_filter_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = _FakeMailBoxState()
        state.messages = _three_messages()
        _install_fake_mailbox(monkeypatch, state)

        await _make_list_tool().call({"since_days": 7})

        assert state.fetch_calls
        last = state.fetch_calls[-1]
        rendered = repr(last)
        # Don't pin the exact API. Look for *any* date object or "date"-
        # mentioning argument. imap_tools' AND(date_gte=...) reprs include
        # the date string; a manual datetime would also stringify.
        has_date_token = (
            "date" in rendered.lower()
            or "since" in rendered.lower()
            # A date object's ISO form for ~7 days ago contains "2026-".
            or "2026-04-" in rendered
            or "2026-03-" in rendered  # span boundary
        )
        assert has_date_token, f"expected date criteria in fetch: {last!r}"


class TestGmailListErrors:
    async def test_auth_failure_raises_auth_error(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        state = _FakeMailBoxState()

        # Surrogate for imap_tools.errors.MailboxLoginError; the
        # implementer must catch the library exception and translate to
        # AuthError. Use a generic Exception subclass with a recognizable
        # name to make the catch site explicit.
        class _LoginRefused(Exception):
            pass

        state.login_raises = _LoginRefused("AUTHENTICATIONFAILED")
        _install_fake_mailbox(monkeypatch, state)

        with pytest.raises(AuthError) as excinfo:
            await _make_list_tool().call({})

        _assert_no_secret_anywhere(caplog=caplog, exc=excinfo.value)

    async def test_connect_failure_raises_transport_error(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        state = _FakeMailBoxState()
        state.constructor_raises = OSError("connection refused")
        _install_fake_mailbox(monkeypatch, state)

        with pytest.raises(TransportError) as excinfo:
            await _make_list_tool().call({})

        _assert_no_secret_anywhere(caplog=caplog, exc=excinfo.value)

    async def test_gaierror_raises_transport_error(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        state = _FakeMailBoxState()
        state.constructor_raises = socket.gaierror("name resolution failure")
        _install_fake_mailbox(monkeypatch, state)

        with pytest.raises(TransportError) as excinfo:
            await _make_list_tool().call({})

        _assert_no_secret_anywhere(caplog=caplog, exc=excinfo.value)


# ===========================================================================
# gmail_read
# ===========================================================================


class TestGmailReadHappyPath:
    async def test_full_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = _FakeMailBoxState()
        state.messages = [
            _FakeMessage(
                uid="999",
                subject="Hi",
                from_="alice@example.com",
                to=("ember@example.com",),
                cc=("dave@example.com",),
                text="Plain body here.",
                html="<p>Plain body here.</p>",
                attachments=(
                    _FakeAttachment("foo.pdf", "application/pdf", b"%PDF" + b"\x00" * 100),
                    _FakeAttachment("bar.txt", "text/plain", b"hello"),
                ),
            )
        ]
        _install_fake_mailbox(monkeypatch, state)

        result = await _make_read_tool().call({"uid": "999"})

        out = result.output
        assert out["uid"] == "999"
        assert out["subject"] == "Hi"
        assert "alice@example.com" in out["from"]
        assert any(
            "ember@example.com" in t
            for t in (out["to"] if isinstance(out["to"], list) else [out["to"]])
        )
        # cc may be string-or-list depending on impl; accept either.
        cc = out["cc"]
        if isinstance(cc, list):
            assert any("dave@example.com" in c for c in cc)
        else:
            assert "dave@example.com" in cc
        assert "date_iso" in out
        assert out["body_text"] == "Plain body here."
        assert "<p>" in out["body_html"]
        assert isinstance(out["attachments"], list)
        assert len(out["attachments"]) == 2
        a0 = out["attachments"][0]
        assert a0["filename"] == "foo.pdf"
        assert a0["content_type"] == "application/pdf"
        assert isinstance(a0["size"], int)
        assert a0["size"] > 0
        # No content payload in v0 — just metadata.
        assert "content" not in a0 and "payload" not in a0 and "data" not in a0

    async def test_label_passthrough(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = _FakeMailBoxState()
        state.messages = [_FakeMessage(uid="10", subject="x", from_="a@x.com", text="x")]
        _install_fake_mailbox(monkeypatch, state)

        await _make_read_tool().call({"uid": "10", "label": "Purchases"})

        assert state.login_calls[-1]["initial_folder"] == "Purchases"


class TestGmailReadTruncation:
    async def test_body_truncated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        big = "x" * 10_000
        state = _FakeMailBoxState()
        state.messages = [_FakeMessage(uid="1", subject="big", from_="a@x.com", text=big)]
        _install_fake_mailbox(monkeypatch, state)

        result = await _make_read_tool().call({"uid": "1", "max_body_chars": 500})

        body = result.output["body_text"]
        # Content trimmed.
        assert len(body) < len(big)
        # Truncation marker present (matches web.py convention).
        assert "[truncated]" in body

    async def test_body_under_cap_not_truncated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        body_text = "small body"
        state = _FakeMailBoxState()
        state.messages = [_FakeMessage(uid="1", subject="x", from_="a@x.com", text=body_text)]
        _install_fake_mailbox(monkeypatch, state)

        result = await _make_read_tool().call({"uid": "1", "max_body_chars": 1000})

        assert result.output["body_text"] == body_text


class TestGmailReadErrors:
    async def test_uid_not_found_raises_tool_error(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        state = _FakeMailBoxState()
        state.messages = []  # No messages at all.
        _install_fake_mailbox(monkeypatch, state)

        with pytest.raises(ToolError) as excinfo:
            await _make_read_tool().call({"uid": "404"})

        # Ensure it's not a more general subclass like AuthError /
        # TransportError. AuthError and TransportError are *not* subclasses of
        # ToolError so a plain ToolError catch is sufficient. Just confirm
        # the typed shape.
        assert isinstance(excinfo.value, ToolError)
        _assert_no_secret_anywhere(caplog=caplog, exc=excinfo.value)


# ===========================================================================
# gmail_send
# ===========================================================================


class TestGmailSendHappyPath:
    async def test_minimal_send(self, monkeypatch: pytest.MonkeyPatch) -> None:
        smtp_state = _install_fake_smtp(monkeypatch, _FakeSMTPState())

        result = await _make_send_tool().call(
            {"to": "alice@example.com", "subject": "Hello", "body": "Hi there!"}
        )

        # Pin the SMTP dance.
        assert smtp_state.starttls_calls == 1
        assert len(smtp_state.login_calls) == 1
        assert smtp_state.login_calls[0][0] == ADDRESS
        assert smtp_state.login_calls[0][1] == SECRET_PASSWORD
        assert len(smtp_state.sendmail_calls) == 1
        call = smtp_state.sendmail_calls[0]
        assert call["from"] == ADDRESS or ADDRESS in call["from"]
        assert "alice@example.com" in call["to"]
        # Quit / context-exit triggers quit.
        # (Either explicit .quit() or context exit; assert the eventual quit.)
        # We don't pin which path the impl chose.

        # Parse message bytes.
        parsed = _msg_from_sendmail(call["msg"])
        assert parsed["Subject"] == "Hello"
        assert ADDRESS in (parsed["From"] or "")
        assert "alice@example.com" in (parsed["To"] or "")
        text = _flatten_text(parsed)
        assert "Hi there!" in text

        # Result shape.
        out = result.output
        assert "message_id" in out
        assert isinstance(out["message_id"], str) and out["message_id"]
        assert "sent_at_iso" in out
        # ISO parsable.
        datetime.datetime.fromisoformat(out["sent_at_iso"])

    async def test_cc_and_bcc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        smtp_state = _install_fake_smtp(monkeypatch, _FakeSMTPState())

        await _make_send_tool().call(
            {
                "to": "a@x.com",
                "subject": "S",
                "body": "B",
                "cc": "c@x.com",
                "bcc": "b@x.com",
            }
        )

        assert len(smtp_state.sendmail_calls) == 1
        call = smtp_state.sendmail_calls[0]
        # All three should be in the SMTP RCPT TO list.
        recips = call["to"]
        assert "a@x.com" in recips
        assert "c@x.com" in recips
        assert "b@x.com" in recips

        parsed = _msg_from_sendmail(call["msg"])
        cc = parsed["Cc"] or ""
        assert "c@x.com" in cc
        # BCC must NOT appear in the message headers (that's the entire
        # point of a blind copy). It's only in the SMTP envelope.
        bcc = parsed["Bcc"]
        assert bcc is None or "b@x.com" not in bcc

    async def test_in_reply_to_sets_threading_headers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        smtp_state = _install_fake_smtp(monkeypatch, _FakeSMTPState())

        await _make_send_tool().call(
            {
                "to": "a@x.com",
                "subject": "Re: x",
                "body": "B",
                "in_reply_to": "abc@x.com",
            }
        )

        parsed = _msg_from_sendmail(smtp_state.sendmail_calls[0]["msg"])
        irt = parsed["In-Reply-To"] or ""
        refs = parsed["References"] or ""
        # Surround with <> per RFC 5322; tolerate either with-or-without.
        assert "abc@x.com" in irt
        assert "abc@x.com" in refs

    async def test_html_content_type(self, monkeypatch: pytest.MonkeyPatch) -> None:
        smtp_state = _install_fake_smtp(monkeypatch, _FakeSMTPState())

        await _make_send_tool().call(
            {
                "to": "a@x.com",
                "subject": "S",
                "body": "<p>Hi</p>",
                "html": True,
            }
        )

        parsed = _msg_from_sendmail(smtp_state.sendmail_calls[0]["msg"])
        # HTML is *somewhere* in the MIME tree as text/html.
        types: list[str] = []
        if parsed.is_multipart():
            for part in parsed.walk():
                types.append(part.get_content_type())
        else:
            types.append(parsed.get_content_type())
        assert any(t == "text/html" for t in types), f"types={types}"

    async def test_plain_default_content_type(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 2026-09-29: plain bodies go out as multipart/alternative — the text
        # verbatim plus a derived HTML part, both quoted-printable (a lone 8bit
        # text/plain draft lost its body when Spark opened it for editing).
        smtp_state = _install_fake_smtp(monkeypatch, _FakeSMTPState())

        await _make_send_tool().call({"to": "a@x.com", "subject": "S", "body": "plain text body"})

        parsed = email.message_from_bytes(
            smtp_state.sendmail_calls[0]["msg"], policy=email.policy.default
        )
        assert parsed.get_content_type() == "multipart/alternative"
        plain = parsed.get_body(preferencelist=("plain",))
        assert plain.get_content() == "plain text body\n"
        assert plain["Content-Transfer-Encoding"] == "quoted-printable"
        assert "plain text body" in parsed.get_body(preferencelist=("html",)).get_content()

    async def test_comma_separated_to(self, monkeypatch: pytest.MonkeyPatch) -> None:
        smtp_state = _install_fake_smtp(monkeypatch, _FakeSMTPState())

        await _make_send_tool().call(
            {
                "to": "a@x.com, b@x.com",
                "subject": "S",
                "body": "B",
            }
        )

        recips = smtp_state.sendmail_calls[0]["to"]
        assert "a@x.com" in recips
        assert "b@x.com" in recips


class TestGmailSendErrors:
    async def test_smtp_auth_error(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        smtp_state = _FakeSMTPState()
        # Use the real smtplib exception class so the implementer's
        # except-clause is exercised authentically.
        import smtplib

        smtp_state.login_raises = smtplib.SMTPAuthenticationError(
            535, b"5.7.8 Username and Password not accepted"
        )
        _install_fake_smtp(monkeypatch, smtp_state)

        with pytest.raises(AuthError) as excinfo:
            await _make_send_tool().call({"to": "a@x.com", "subject": "S", "body": "B"})

        _assert_no_secret_anywhere(caplog=caplog, exc=excinfo.value)

    async def test_smtp_transport_error_on_connect(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        smtp_state = _FakeSMTPState()
        smtp_state.connect_raises = OSError("connection refused")
        _install_fake_smtp(monkeypatch, smtp_state)

        with pytest.raises(TransportError) as excinfo:
            await _make_send_tool().call({"to": "a@x.com", "subject": "S", "body": "B"})

        _assert_no_secret_anywhere(caplog=caplog, exc=excinfo.value)

    async def test_smtp_transport_error_on_send(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        import smtplib

        smtp_state = _FakeSMTPState()
        smtp_state.send_raises = smtplib.SMTPServerDisconnected("server gone")
        _install_fake_smtp(monkeypatch, smtp_state)

        with pytest.raises(TransportError) as excinfo:
            await _make_send_tool().call({"to": "a@x.com", "subject": "S", "body": "B"})

        _assert_no_secret_anywhere(caplog=caplog, exc=excinfo.value)


# ===========================================================================
# Outbound safety (post-268c779): allowlist + rolling 24h cap
# ===========================================================================


class TestGmailSendOutboundSafety:
    """Post-268c779 *revision*: the allowlist is now the auto-send set,
    and non-allowlisted recipients route through human approval instead
    of a hard rejection. These tests pin the `needs_confirmation` gate
    predicate (consulted by the registry) rather than a `call()` raise —
    `call()` itself no longer re-enforces the allowlist, because it only
    runs for allowlisted or already-approved sends.
    """

    def test_empty_allowlist_confirms_all_sends(self) -> None:
        # An empty allowlist means nothing auto-sends: every recipient
        # needs the user's approval. (Previously this hard-failed; now it
        # routes to the confirmation gate.)
        tool = GmailSendTool(
            address=ADDRESS,
            app_password=SECRET_PASSWORD,
            allowed_recipients=[],
            daily_cap=20,
        )
        assert tool.needs_confirmation({"to": "anyone@example.com", "subject": "S", "body": "B"})

    def test_recipient_outside_allowlist_needs_confirmation(self) -> None:
        tool = _make_send_tool(allowed_recipients=["alice@example.com"])
        args = {"to": "stranger@example.com", "subject": "S", "body": "B"}
        assert tool.needs_confirmation(args) is True
        # The preview names the off-allowlist recipient so the user knows
        # who they're approving.
        assert "stranger@example.com" in tool.summarize_action(args)

    def test_allowlisted_recipient_auto_sends(self) -> None:
        # A fully-allowlisted send does NOT need confirmation — the gate
        # is bypassed and the send happens immediately.
        tool = _make_send_tool(allowed_recipients=["alice@example.com"])
        assert (
            tool.needs_confirmation({"to": "alice@example.com", "subject": "S", "body": "B"})
            is False
        )

    async def test_domain_allowlist_entry_matches_any_address_at_domain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `@example.org` allows `anything@example.org` — auto-send,
        # and `call()` sends without a prompt.
        smtp_state = _FakeSMTPState()
        _install_fake_smtp(monkeypatch, smtp_state)

        tool = _make_send_tool(allowed_recipients=["@example.org"])
        args = {"to": "me@example.org", "subject": "S", "body": "B"}
        assert tool.needs_confirmation(args) is False
        await tool.call(args)
        assert len(smtp_state.sendmail_calls) == 1

    def test_cc_recipient_off_allowlist_needs_confirmation(self) -> None:
        # The gate considers `to`, `cc`, and `bcc` together: one
        # off-allowlist recipient anywhere forces approval.
        tool = _make_send_tool(allowed_recipients=["@x.com"])
        args = {
            "to": "ok@x.com",
            "cc": "stranger@y.com",
            "subject": "S",
            "body": "B",
        }
        assert tool.needs_confirmation(args) is True
        assert "stranger@y.com" in tool.summarize_action(args)

    def test_summarize_action_includes_subject_and_body_excerpt(self) -> None:
        tool = _make_send_tool(allowed_recipients=[])
        preview = tool.summarize_action(
            {"to": "x@stranger.com", "subject": "Quarterly update", "body": "Here are the numbers."}
        )
        assert "gmail_send" in preview
        assert "x@stranger.com" in preview
        assert "Quarterly update" in preview
        assert "Here are the numbers." in preview

    def test_summarize_action_shows_full_long_body(self) -> None:
        # The user is approving exactly this text — the preview must
        # never cut it (Discord-side chunking handles delivery).
        tool = _make_send_tool(allowed_recipients=[])
        body = "\n".join(f"paragraph {i} " + "z" * 200 for i in range(40))
        preview = tool.summarize_action({"to": "x@stranger.com", "subject": "S", "body": body})
        assert "[truncated]" not in preview
        assert preview.endswith(body)

    async def test_daily_cap_enforced_by_log_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        # Pre-seed the log file with `daily_cap` entries inside the
        # 24h window; the next send must be rejected.
        import json

        log_path = tmp_path / "gmail_send_log.jsonl"
        now = datetime.datetime.now(datetime.timezone.utc)
        with log_path.open("w", encoding="utf-8") as f:
            for i in range(3):
                f.write(
                    json.dumps(
                        {
                            "sent_at_iso": (now - datetime.timedelta(hours=1)).isoformat(),
                            "message_id": f"<m{i}@x>",
                            "to": ["a@x.com"],
                            "subject": f"prior-{i}",
                        }
                    )
                    + "\n"
                )

        smtp_state = _FakeSMTPState()
        _install_fake_smtp(monkeypatch, smtp_state)

        tool = _make_send_tool(daily_cap=3, log_path=log_path)
        with pytest.raises(ToolError) as excinfo:
            await tool.call({"to": "a@x.com", "subject": "S", "body": "B"})
        assert "cap" in str(excinfo.value).lower() or "limit" in str(excinfo.value).lower()
        assert smtp_state.sendmail_calls == []

    async def test_daily_cap_ignores_entries_outside_24h_window(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        # Entries older than 24h should not count toward the rolling
        # cap.
        import json

        log_path = tmp_path / "gmail_send_log.jsonl"
        now = datetime.datetime.now(datetime.timezone.utc)
        with log_path.open("w", encoding="utf-8") as f:
            for i in range(10):
                f.write(
                    json.dumps(
                        {
                            "sent_at_iso": (now - datetime.timedelta(hours=48)).isoformat(),
                            "message_id": f"<m{i}@x>",
                            "to": ["a@x.com"],
                            "subject": f"old-{i}",
                        }
                    )
                    + "\n"
                )

        smtp_state = _FakeSMTPState()
        _install_fake_smtp(monkeypatch, smtp_state)

        tool = _make_send_tool(daily_cap=3, log_path=log_path)
        # Should succeed — the 10 prior entries are 48h old and rolled off.
        await tool.call({"to": "a@x.com", "subject": "S", "body": "B"})
        assert len(smtp_state.sendmail_calls) == 1

    async def test_successful_send_appends_to_log(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        import json

        log_path = tmp_path / "gmail_send_log.jsonl"
        smtp_state = _FakeSMTPState()
        _install_fake_smtp(monkeypatch, smtp_state)

        tool = _make_send_tool(log_path=log_path)
        await tool.call({"to": "a@x.com", "subject": "test-subj", "body": "B"})

        assert log_path.exists()
        with log_path.open("r", encoding="utf-8") as f:
            lines = [line for line in f.read().splitlines() if line.strip()]
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["subject"] == "test-subj"
        assert "a@x.com" in rec["to"]
        # Timestamp is a parseable ISO string.
        datetime.datetime.fromisoformat(rec["sent_at_iso"])


# ===========================================================================
# TLC-4 secret hygiene — verified across all three tools under success too
# ===========================================================================


class TestSecretHygiene:
    async def test_password_never_logged_on_list_success(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        state = _FakeMailBoxState()
        state.messages = _three_messages()
        _install_fake_mailbox(monkeypatch, state)

        await _make_list_tool().call({})

        _assert_no_secret_anywhere(caplog=caplog)

    async def test_password_never_logged_on_read_success(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        state = _FakeMailBoxState()
        state.messages = [_FakeMessage(uid="1", subject="x", from_="a@x.com", text="b")]
        _install_fake_mailbox(monkeypatch, state)

        await _make_read_tool().call({"uid": "1"})

        _assert_no_secret_anywhere(caplog=caplog)

    async def test_password_never_logged_on_send_success(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        _install_fake_smtp(monkeypatch, _FakeSMTPState())

        await _make_send_tool().call({"to": "a@x.com", "subject": "S", "body": "B"})

        _assert_no_secret_anywhere(caplog=caplog)

    async def test_password_not_in_repr_of_tool(self) -> None:
        # Defensive: tool's repr/str should not leak the password.
        for tool in (_make_list_tool(), _make_read_tool(), _make_send_tool()):
            assert SECRET_PASSWORD not in repr(tool)
            assert SECRET_PASSWORD not in str(tool)
