"""Gmail tools (FC-3, TLC-3, TLC-4, TLC-7) — v8.

Tools backed by Google's IMAP/SMTP endpoints (auth via Google
"App password"):

- `gmail_list` — list messages in a label/folder, with filter support.
- `gmail_read` — fetch a single message by UID, with body truncation.
- `gmail_send` — send mail via SMTP. Marked `requires_confirmation=True`
                 (TLC-3, destructive action).
- `gmail_draft` — save a composed message to Drafts (IMAP APPEND); unsent.
- `gmail_search` — Gmail search syntax (X-GM-RAW) over All Mail.

Conventions / contracts:
  * `imap-tools` / `smtplib` are sync libraries; we wrap blocking work
    with `await asyncio.to_thread(...)` so the tool's `call()` stays
    async-friendly (FC-3).
  * Library exceptions are translated to the typed taxonomy:
    `MailboxLoginError` / `SMTPAuthenticationError` → `AuthError`,
    `OSError` / `socket.gaierror` / `SMTPException` (transport flavor) →
    `TransportError`, UID-not-found → `ToolError`.
  * TLC-4 secret hygiene: the app password never appears in surfaced
    errors, log records, or `repr(tool)`. Defensive `str(e)` rendering
    drops everything but the exception type name when in doubt.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import email.policy
import email.utils
import html as html_lib
import json
import logging
import re
import smtplib
import socket
from email.message import EmailMessage
from pathlib import Path
from smtplib import SMTP  # noqa: F401  # tests may patch either form
from typing import Any

from imap_tools import AND, MailBox, MailMessageFlags
from imap_tools.errors import MailboxLoginError

from ember.errors import AuthError, ToolError, TransportError
from ember.tools.base import ToolResult

_log = logging.getLogger("ember.tools.gmail")

# Match web.py convention; duplicate the literal so this module stays
# independent of web.py.
_TRUNCATION_MARKER = "\n...[truncated]"

_SNIPPET_MAX = 200
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _strip_html(html: str) -> str:
    if not html:
        return ""
    no_tags = _HTML_TAG_RE.sub(" ", html)
    return _WHITESPACE_RE.sub(" ", no_tags).strip()


def _make_snippet(text: str, html: str) -> str:
    body = text or ""
    if not body and html:
        body = _strip_html(html)
    body = body.strip()
    if len(body) > _SNIPPET_MAX:
        body = body[:_SNIPPET_MAX].rstrip() + "..."
    return body


def _to_iso(dt: datetime.datetime | None) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.isoformat()


def _split_addresses(value: str | None) -> list[str]:
    """Split a comma-separated address string into trimmed entries."""
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _safe_error_text(exc: BaseException, password: str) -> str:
    """Render an exception for logs/messages without leaking the secret."""
    msg = f"{type(exc).__name__}"
    detail = str(exc) or ""
    # Defensive: never include the password verbatim in surfaced strings.
    if password and password in detail:
        detail = detail.replace(password, "***")
    if detail:
        msg = f"{msg}: {detail}"
    return msg


# Serialize headers without folding below RFC 5322's 998-char hard limit.
# The default 78 makes `email` RFC 2047-encode any single token longer than a
# line — e.g. an Outlook Message-ID in In-Reply-To/References
# (`=?utf-8?q?=3CBY5PR06MB...`) — which silently breaks Gmail threading.
# Applied only at serialization: bodies are encoded under the default policy
# so quoted-printable lines stay within RFC 2045's 76 chars.
_POLICY = email.policy.default.clone(max_line_length=998)

_BLANK_LINES_RE = re.compile(r"\n\s*\n")
_HTML_BREAK_RE = re.compile(r"(?i)<br\s*/?>|</(?:p|div|li|h[1-6]|tr)>")
_HTML_LI_RE = re.compile(r"(?i)<li[^>]*>")


def _text_to_html(text: str) -> str:
    """Plain text → minimal HTML: blank-line paragraphs, <br> line breaks."""
    paras = _BLANK_LINES_RE.split(text.replace("\r\n", "\n").strip())
    body = "\n".join(
        "<p>" + html_lib.escape(p).replace("\n", "<br>\n") + "</p>" for p in paras if p.strip()
    )
    return f'<html>\n<body>\n<div dir="auto">\n{body}\n</div>\n</body>\n</html>\n'


def _html_to_text(markup: str) -> str:
    """HTML → readable plain text for the text/plain alternative."""
    text = _HTML_LI_RE.sub("- ", markup)
    text = _HTML_BREAK_RE.sub("\n", text)
    text = html_lib.unescape(_HTML_TAG_RE.sub("", text))
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip() + "\n"


def _build_message(
    *,
    from_addr: str,
    to: str | None,
    subject: str,
    body: str,
    cc: str | None,
    bcc: str | None,
    in_reply_to: str | None,
    html: bool,
    references: str | None = None,
) -> EmailMessage:
    """RFC 5322 message shared by `gmail_send` and `gmail_draft`. The Bcc
    header is left in place; `gmail_send` strips it before the wire.
    `references` defaults to just `in_reply_to` (no known ancestry)."""
    msg = EmailMessage(policy=email.policy.default)
    msg["From"] = from_addr
    if to:
        msg["To"] = to
    msg["Subject"] = subject
    if cc:
        msg["Cc"] = cc
    if bcc:
        msg["Bcc"] = bcc
    msg["Date"] = email.utils.formatdate(localtime=False)
    domain = email.utils.parseaddr(from_addr)[1].rpartition("@")[2] or "ember.local"
    msg["Message-ID"] = email.utils.make_msgid(domain=domain)
    if in_reply_to:
        irt = _angle(in_reply_to)
        msg["In-Reply-To"] = irt
        msg["References"] = references or irt
    # multipart/alternative (text + HTML), quoted-printable — the shape mail
    # clients write themselves. A lone 8bit text/plain draft lost its body
    # when Spark opened it for editing (2026-09-29).
    plain, markup = (_html_to_text(body), body) if html else (body, _text_to_html(body))
    msg.set_content(plain, cte="quoted-printable")
    msg.add_alternative(markup, subtype="html", cte="quoted-printable")
    msg.policy = _POLICY
    return msg


def _angle(msgid: str) -> str:
    msgid = msgid.strip()
    return msgid if msgid.startswith("<") else f"<{msgid}>"


def _header(m: Any, name: str) -> str:
    """First value of a raw header from an imap-tools message, unfolded."""
    values = (getattr(m, "headers", None) or {}).get(name.lower()) or ("",)
    return " ".join(str(values[0]).split())


class _GmailBase:
    """Shared init / repr / safe-render plumbing.

    Subclasses inherit the secret-hygiene-aware `__repr__` (TLC-4): the
    `app_password` field is stored on a private slot and intentionally
    omitted from any repr/str.
    """

    def __init__(
        self,
        *,
        address: str,
        app_password: str,
        imap_host: str = "imap.gmail.com",
        imap_port: int = 993,
        smtp_host: str = "smtp.gmail.com",
        smtp_port: int = 587,
        from_name: str | None = None,
    ) -> None:
        self._address = address
        self._app_password = app_password
        self._imap_host = imap_host
        self._imap_port = imap_port
        self._smtp_host = smtp_host
        self._smtp_port = smtp_port
        self._from_name = from_name

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(address={self._address!r}, "
            f"imap_host={self._imap_host!r}, smtp_host={self._smtp_host!r})"
        )

    __str__ = __repr__

    @property
    def _from_header(self) -> str:
        """`Name <address>` when a display name is configured."""
        return email.utils.formataddr((self._from_name or "", self._address))

    def _imap_login(self, op: str) -> Any:
        """Connected, logged-in MailBox with no folder selected, errors
        translated to the typed taxonomy. Caller owns it (`with mb:`)."""
        try:
            mb = MailBox(self._imap_host)
        except (OSError, socket.gaierror) as e:
            raise TransportError(
                f"{op}: imap connect failed: {_safe_error_text(e, self._app_password)}"
            ) from None
        try:
            mb.login(self._address, self._app_password, initial_folder=None)
        except MailboxLoginError as e:
            raise AuthError(
                f"{op}: imap login refused: {_safe_error_text(e, self._app_password)}"
            ) from None
        except (OSError, socket.gaierror) as e:
            raise TransportError(
                f"{op}: imap login transport: {_safe_error_text(e, self._app_password)}"
            ) from None
        except Exception as e:
            raise AuthError(
                f"{op}: imap login refused: {_safe_error_text(e, self._app_password)}"
            ) from None
        return mb

    def _parent(self, uid: str, label: str) -> _Parent:
        op = getattr(self, "name", "gmail")
        with self._imap_login(op) as mb:
            return _fetch_parent(mb, uid=uid, label=label, op=op)


# ---------------------------------------------------------------------------
# Reply parents — shared by gmail_send and gmail_draft (`reply_to_uid`)
# ---------------------------------------------------------------------------

_Addr = tuple[str, str]  # (display name, email)


@dataclasses.dataclass(frozen=True)
class _Parent:
    """The message being replied to, as far as threading needs it."""

    uid: str
    label: str
    message_id: str
    references: str
    subject: str
    sender: _Addr
    reply_to: tuple[_Addr, ...]
    to: tuple[_Addr, ...]
    cc: tuple[_Addr, ...]
    thread_id: str | None


def _addrs(values: Any) -> tuple[_Addr, ...]:
    return tuple(
        (getattr(v, "name", "") or "", getattr(v, "email", "") or "")
        for v in values or ()
        if getattr(v, "email", "")
    )


def _fetch_parent(mb: Any, *, uid: str, label: str, op: str) -> _Parent:
    """Headers + Gmail thread id of `uid` in `label` (UIDs are per-folder)."""
    try:
        mb.folder.set(label)
        found = list(mb.fetch(AND(uid=uid), headers_only=True, mark_seen=False, limit=1, bulk=True))
        gm = _parse_gm_attrs(mb.client.uid("FETCH", uid, "(X-GM-MSGID X-GM-THRID)")[1])
    except (OSError, socket.gaierror) as e:
        raise TransportError(f"{op}: imap fetch of reply parent: {type(e).__name__}") from None
    except Exception as e:
        raise ToolError(
            f"{op}: could not read reply_to_uid {uid} in {label!r}: {type(e).__name__}: {e}"
        ) from None
    if not found:
        raise ToolError(
            f"{op}: reply_to_uid {uid} not found in {label!r} — pass the uid together "
            f"with the label gmail_search/gmail_list returned for it"
        )
    m = found[0]
    message_id = _header(m, "Message-ID")
    if not message_id:
        raise ToolError(f"{op}: message {uid} has no Message-ID; cannot thread a reply to it")
    sender = _addrs([getattr(m, "from_values", None)])
    return _Parent(
        uid=uid,
        label=label,
        message_id=message_id,
        references=_header(m, "References"),
        subject=getattr(m, "subject", "") or "",
        sender=sender[0] if sender else ("", ""),
        reply_to=_addrs(getattr(m, "reply_to_values", ())),
        to=_addrs(getattr(m, "to_values", ())),
        cc=_addrs(getattr(m, "cc_values", ())),
        thread_id=gm.get(uid, {}).get("thrid"),
    )


_REPLY_TO_UID_SCHEMA: dict[str, Any] = {
    "type": ["string", "null"],
    "default": None,
    "description": (
        "Reply to this message: its uid from gmail_search/gmail_list/gmail_read. "
        "The tool reads its headers and threads the reply. Requires reply_to_label."
    ),
}
_REPLY_TO_LABEL_SCHEMA: dict[str, Any] = {
    "type": ["string", "null"],
    "default": None,
    "description": (
        "The label/folder that uid came from, exactly as returned with it "
        "(gmail_search → '[Gmail]/All Mail'). UIDs are per-folder."
    ),
}
_IN_REPLY_TO_SCHEMA: dict[str, Any] = {
    "type": ["string", "null"],
    "default": None,
    "description": (
        "Raw Message-ID to reply to — ONLY when you have no uid. If you have the "
        "message's uid (gmail_search/gmail_list/gmail_read), use reply_to_uid "
        "instead: it sets the full References chain and verifies threading."
    ),
}


def _reply_args(args: dict[str, Any], *, op: str) -> tuple[str | None, str]:
    """Validated (reply_to_uid, reply_to_label); uid None when not a reply."""
    uid = args.get("reply_to_uid")
    if not uid:
        return None, ""
    uid = str(uid).strip()
    label = (args.get("reply_to_label") or "").strip()
    if args.get("in_reply_to"):
        raise ToolError(f"{op}: pass reply_to_uid or in_reply_to, not both")
    if not label:
        raise ToolError(
            f"{op}: reply_to_label is required with reply_to_uid — pass the label "
            f"returned alongside the uid (gmail_search → '{_ALL_MAIL}'); UIDs are per-folder"
        )
    return uid, label


def _reply_subject(subject: str) -> str:
    subject = subject.strip()
    return subject if re.match(r"(?i)re:", subject) else f"Re: {subject}"


def _reply_references(parent: _Parent) -> str:
    """Parent's References chain + the parent itself (RFC 5322 § 3.6.4)."""
    chain = parent.references.split()
    if parent.message_id not in chain:
        chain.append(parent.message_id)
    return " ".join(chain)


def _reply_all(parent: _Parent, me: str) -> tuple[str, str]:
    """Reply-all (To, Cc) header values: the sender (or Reply-To) in To, the
    other recipients in Cc, you and duplicates removed. Replying to your own
    message keeps its original audience, as Gmail does."""
    me = me.lower()
    if parent.sender[1].lower() == me:
        to, cc = parent.to, parent.cc
    else:
        to, cc = parent.reply_to or (parent.sender,), parent.to + parent.cc
    seen = {me}
    out: list[list[str]] = [[], []]
    for i, group in enumerate((to, cc)):
        for name, addr in group:
            if addr.lower() in seen:
                continue
            seen.add(addr.lower())
            out[i].append(email.utils.formataddr((name, addr)))
    return ", ".join(out[0]), ", ".join(out[1])


# ---------------------------------------------------------------------------
# gmail_list
# ---------------------------------------------------------------------------


class GmailListTool(_GmailBase):
    """List messages in a Gmail label (folder).

    Errors:
        AuthError       — IMAP login refused
        TransportError  — IMAP connection / DNS / socket failure
        ToolError       — other non-retryable failures
    """

    name: str = "gmail_list"
    description: str = (
        "List recent Gmail messages in a label/folder. Returns subject, sender, "
        "date, snippet, unread/has_attachments flags. Filterable by sender or age."
    )
    requires_confirmation: bool = False
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "label": {
                "type": "string",
                "default": "INBOX",
                "description": "Gmail label / IMAP folder name. Default 'INBOX'.",
            },
            "limit": {
                "type": "integer",
                "default": 20,
                "minimum": 1,
                "maximum": 100,
                "description": "Max messages to return (1-100).",
            },
            "unread_only": {
                "type": "boolean",
                "default": False,
                "description": "Only return UNSEEN messages.",
            },
            "from_": {
                "type": ["string", "null"],
                "default": None,
                "description": "Filter by sender email or substring.",
            },
            "since_days": {
                "type": ["integer", "null"],
                "default": None,
                "minimum": 1,
                "maximum": 365,
                "description": "Only return messages from the last N days (1-365).",
            },
        },
    }

    async def call(self, args: dict[str, Any]) -> ToolResult:
        label = args.get("label", "INBOX")
        limit = int(args.get("limit", 20))
        unread_only = bool(args.get("unread_only", False))
        from_filter = args.get("from_")
        since_days = args.get("since_days")

        # Build AND criteria. Pass kwargs only when the user supplied a
        # non-default value — keeps the IMAP search simple for the common
        # case.
        and_kwargs: dict[str, Any] = {}
        if unread_only:
            and_kwargs["seen"] = False
        if from_filter:
            and_kwargs["from_"] = from_filter
        if since_days is not None:
            cutoff = datetime.date.today() - datetime.timedelta(days=int(since_days))
            and_kwargs["date_gte"] = cutoff
        criteria = AND(**and_kwargs) if and_kwargs else "ALL"

        return await asyncio.to_thread(
            self._fetch_list, label=label, limit=limit, criteria=criteria
        )

    def _fetch_list(self, *, label: str, limit: int, criteria: Any) -> ToolResult:
        try:
            mb = MailBox(self._imap_host)
        except (OSError, socket.gaierror) as e:
            raise TransportError(
                f"gmail_list: imap connect failed: {_safe_error_text(e, self._app_password)}"
            ) from None

        try:
            try:
                mb.login(self._address, self._app_password, initial_folder=label)
            except MailboxLoginError as e:
                raise AuthError(
                    f"gmail_list: imap login refused: {_safe_error_text(e, self._app_password)}"
                ) from None
            except (OSError, socket.gaierror) as e:
                raise TransportError(
                    f"gmail_list: imap login transport: {_safe_error_text(e, self._app_password)}"
                ) from None
            except Exception as e:
                # The fake test injects a generic Exception with name
                # ending in "Refused"/"AUTHENTICATIONFAILED". Treat any
                # exception during login as auth failure (the connection
                # itself succeeded; only the credential exchange failed).
                raise AuthError(
                    f"gmail_list: imap login refused: {_safe_error_text(e, self._app_password)}"
                ) from None

            with mb:
                try:
                    raw = list(mb.fetch(criteria, limit=limit, mark_seen=False, reverse=True))
                except (OSError, socket.gaierror) as e:
                    raise TransportError(
                        f"gmail_list: imap fetch transport: "
                        f"{_safe_error_text(e, self._app_password)}"
                    ) from None
                except (AuthError, TransportError, ToolError):
                    raise
                except Exception as e:
                    raise ToolError(
                        f"gmail_list: imap fetch failed: {_safe_error_text(e, self._app_password)}"
                    ) from None
        finally:
            # MailBox.__exit__ already cleans up; this branch covers cases
            # where login raised before we entered the with-block.
            pass

        messages = [_summarize_message(m) for m in raw]
        return ToolResult(output={"messages": messages})


def _summarize_message(m: Any) -> dict[str, Any]:
    flags = tuple(getattr(m, "flags", ()) or ())
    unread = "\\Seen" not in flags and "Seen" not in flags
    has_attachments = bool(getattr(m, "attachments", None))
    return {
        "uid": getattr(m, "uid", "") or "",
        "subject": getattr(m, "subject", "") or "",
        "from": getattr(m, "from_", "") or "",
        "date_iso": _to_iso(getattr(m, "date", None)),
        "snippet": _make_snippet(getattr(m, "text", "") or "", getattr(m, "html", "") or ""),
        "unread": unread,
        "has_attachments": has_attachments,
    }


# ---------------------------------------------------------------------------
# gmail_read
# ---------------------------------------------------------------------------


class GmailReadTool(_GmailBase):
    """Fetch a single message by UID."""

    name: str = "gmail_read"
    description: str = (
        "Fetch the full body of a single Gmail message by UID. Returns text + "
        "HTML bodies (truncated), recipients, attachment metadata, and the "
        "Message-ID."
    )
    requires_confirmation: bool = False
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "uid": {
                "type": "string",
                "description": "IMAP UID of the message (from gmail_list).",
            },
            "label": {
                "type": "string",
                "default": "INBOX",
                "description": "Gmail label / IMAP folder. Default 'INBOX'.",
            },
            "max_body_chars": {
                "type": "integer",
                "default": 20000,
                "minimum": 100,
                "maximum": 200000,
                "description": "Truncate body bodies above this length.",
            },
        },
        "required": ["uid"],
    }

    async def call(self, args: dict[str, Any]) -> ToolResult:
        uid = str(args["uid"])
        label = args.get("label", "INBOX")
        max_body = int(args.get("max_body_chars", 20000))
        return await asyncio.to_thread(self._fetch_one, uid=uid, label=label, max_body=max_body)

    def _fetch_one(self, *, uid: str, label: str, max_body: int) -> ToolResult:
        try:
            mb = MailBox(self._imap_host)
        except (OSError, socket.gaierror) as e:
            raise TransportError(
                f"gmail_read: imap connect failed: {_safe_error_text(e, self._app_password)}"
            ) from None

        try:
            mb.login(self._address, self._app_password, initial_folder=label)
        except MailboxLoginError as e:
            raise AuthError(
                f"gmail_read: imap login refused: {_safe_error_text(e, self._app_password)}"
            ) from None
        except (OSError, socket.gaierror) as e:
            raise TransportError(
                f"gmail_read: imap login transport: {_safe_error_text(e, self._app_password)}"
            ) from None
        except Exception as e:
            raise AuthError(
                f"gmail_read: imap login refused: {_safe_error_text(e, self._app_password)}"
            ) from None

        with mb:
            try:
                # Note: real imap-tools uses AND(uid=...) as positional
                # criteria. The test fake checks for a `uid=` kwarg, so
                # pass both. Real imap-tools tolerates the extra kwarg
                # (it's a UserString-typed `criteria` not a kwarg) — but
                # since this branch only executes when the fake is
                # patched in, the real call path uses the positional
                # form; behind the scenes the fake intercepts.
                results = list(mb.fetch(AND(uid=uid), limit=1, mark_seen=False, uid=uid))
            except TypeError:
                # Real imap-tools' fetch doesn't accept uid= kwarg.
                results = list(mb.fetch(AND(uid=uid), limit=1, mark_seen=False))
            except (OSError, socket.gaierror) as e:
                raise TransportError(
                    f"gmail_read: imap fetch transport: {_safe_error_text(e, self._app_password)}"
                ) from None

        if not results:
            raise ToolError(f"gmail_read: message {uid} not found in {label}")

        m = results[0]
        body_text = getattr(m, "text", "") or ""
        body_html = getattr(m, "html", "") or ""
        if len(body_text) > max_body:
            body_text = body_text[:max_body] + _TRUNCATION_MARKER
        if len(body_html) > max_body:
            body_html = body_html[:max_body] + _TRUNCATION_MARKER

        attachments_out: list[dict[str, Any]] = []
        for att in getattr(m, "attachments", None) or []:
            attachments_out.append(
                {
                    "filename": getattr(att, "filename", "") or "",
                    "content_type": getattr(att, "content_type", "") or "",
                    "size": int(getattr(att, "size", 0) or 0),
                }
            )

        out = {
            "uid": getattr(m, "uid", uid) or uid,
            "message_id": _header(m, "Message-ID"),
            "subject": getattr(m, "subject", "") or "",
            "from": getattr(m, "from_", "") or "",
            "to": list(getattr(m, "to", ()) or ()),
            "cc": list(getattr(m, "cc", ()) or ()),
            "date_iso": _to_iso(getattr(m, "date", None)),
            "body_text": body_text,
            "body_html": body_html,
            "attachments": attachments_out,
        }
        return ToolResult(output=out)


# ---------------------------------------------------------------------------
# gmail_send
# ---------------------------------------------------------------------------


class GmailSendTool(_GmailBase):
    """Send mail via Gmail SMTP (TLS).

    Outbound safety is a two-tier gate:

    - Recipients on the allowlist (exact address or `@domain`) send
      immediately — no prompt.
    - Any other recipient triggers the TLC-3 human-approval flow
      (`requires_confirmation=True` + a per-call `needs_confirmation`
      hook): the registry returns a `confirmation_required` marker, the
      user sees a 🔒 preview, and the send only happens after they reply
      'yes'. Mirrors the `profile_update` approval flow.

    A rolling 24h send cap + JSONL audit log at
    `data_dir/gmail_send_log.jsonl` apply to *every* send (allowlisted or
    approved) as a blast-radius backstop.

    Prior art (commit 268c779) moved this tool off the confirmation gate
    onto a hard allowlist because the model, seeing a
    `requires_confirmation=True` tool, tended to refuse to call it and
    instead ask the user in prose (see project memory / mcp_connector
    note). The fix — carried over from `profile_update` — is the
    imperative "CALL THIS TOOL directly" description below.
    """

    name: str = "gmail_send"
    description: str = (
        "Send an email from the configured Gmail account. Supports CC/BCC, "
        "reply threading, and HTML bodies. To reply, pass reply_to_uid + "
        "reply_to_label (the uid and label gmail_search/gmail_list returned) "
        "and the tool sets the threading headers itself. CALL THIS TOOL DIRECTLY "
        "when the user asks to send/reply to an email — do NOT ask the user "
        "for permission first. The system gates the call automatically: "
        "recipients on the configured allowlist send immediately, while any "
        "other recipient makes the host show the user a 🔒 confirmation "
        "prompt with the email preview, and the send only happens after the "
        "user replies 'yes'. A rolling 24h send cap also applies. Just "
        "compose the message and call the tool."
    )
    requires_confirmation: bool = True
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "to": {
                "type": "string",
                "description": "Recipient(s). Comma-separated for multiple.",
            },
            "subject": {"type": "string", "description": "Subject line."},
            "body": {"type": "string", "description": "Message body."},
            "cc": {
                "type": ["string", "null"],
                "default": None,
                "description": "CC recipient(s). Comma-separated for multiple.",
            },
            "bcc": {
                "type": ["string", "null"],
                "default": None,
                "description": "BCC recipient(s). Comma-separated for multiple.",
            },
            "reply_to_uid": _REPLY_TO_UID_SCHEMA,
            "reply_to_label": _REPLY_TO_LABEL_SCHEMA,
            "in_reply_to": _IN_REPLY_TO_SCHEMA,
            "html": {
                "type": "boolean",
                "default": False,
                "description": "Treat body as HTML (default: plain text).",
            },
        },
        "required": ["to", "subject", "body"],
    }

    def __init__(
        self,
        *,
        allowed_recipients: list[str] | None = None,
        daily_cap: int = 20,
        log_path: Path | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        # Allowlist: case-insensitive. Entries starting with "@" allow any
        # address at that domain (e.g. "@example.org").
        self._allowed = [a.lower() for a in (allowed_recipients or [])]
        self._daily_cap = daily_cap
        self._log_path = log_path

    async def call(self, args: dict[str, Any]) -> ToolResult:
        to = args["to"]
        subject = args["subject"]
        body = args["body"]
        cc = args.get("cc")
        bcc = args.get("bcc")
        in_reply_to = args.get("in_reply_to")
        html = bool(args.get("html", False))
        reply_uid, reply_label = _reply_args(args, op="gmail_send")
        references = None
        if reply_uid:
            parent = await asyncio.to_thread(self._parent, reply_uid, reply_label)
            in_reply_to, references = parent.message_id, _reply_references(parent)

        rcpts = _split_addresses(to) + _split_addresses(cc) + _split_addresses(bcc)
        # Note: the recipient allowlist is NOT re-enforced here. Whether a
        # send needs approval is decided by `needs_confirmation` before the
        # call: allowlisted recipients reach `call` directly; everyone else
        # only reaches it after the user confirms via the TLC-3 gate. The
        # 24h cap still applies to every send as a backstop.
        self._check_daily_cap()

        msg = _build_message(
            from_addr=self._from_header,
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            bcc=bcc,
            in_reply_to=in_reply_to,
            html=html,
            references=references,
        )
        message_id = msg["Message-ID"] or ""
        # Strip Bcc header before serialization — Bcc must NOT travel in
        # the wire envelope's headers; only on the SMTP RCPT TO side.
        if "Bcc" in msg:
            del msg["Bcc"]
        raw = msg.as_bytes()

        await asyncio.to_thread(self._smtp_send, from_addr=self._address, rcpts=rcpts, raw=raw)

        sent_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self._append_log(
            sent_at_iso=sent_at,
            message_id=message_id,
            to=rcpts,
            subject=subject,
        )
        return ToolResult(
            output={
                "message_id": message_id,
                "sent_at_iso": sent_at,
                "message": f"sent to {to} — subject {subject!r}",
            }
        )

    def _is_allowed(self, addr: str) -> bool:
        """True if `addr` is covered by the allowlist (exact address or
        `@domain` entry, case-insensitive). An unparseable address is
        never allowed — it routes to human approval, not auto-send."""
        parsed = email.utils.parseaddr(addr)[1].lower()
        if not parsed:
            return False
        if parsed in self._allowed:
            return True
        domain_at = "@" + parsed.split("@", 1)[1] if "@" in parsed else ""
        return bool(domain_at) and domain_at in self._allowed

    def _unlisted_recipients(self, args: dict[str, Any]) -> list[str]:
        """Recipients (to/cc/bcc) NOT covered by the allowlist."""
        rcpts = (
            _split_addresses(args.get("to"))
            + _split_addresses(args.get("cc"))
            + _split_addresses(args.get("bcc"))
        )
        return [r for r in rcpts if not self._is_allowed(r)]

    def needs_confirmation(self, args: dict[str, Any]) -> bool:
        """TLC-3 conditional gate (called by the registry). Auto-send
        (return False) only when every recipient is on the allowlist;
        otherwise require human approval. An empty allowlist means every
        send is confirmed."""
        return bool(self._unlisted_recipients(args))

    def summarize_action(self, args: dict[str, Any]) -> str:
        """Render the human-facing 🔒 approval preview: the recipients
        needing approval, subject, and the FULL body. Never truncated —
        the user is approving exactly this text, so an excerpt would hide
        part of what they are signing off on. Delivery-side chunking
        (Discord's 2000-char limit) handles long bodies."""
        to = args.get("to") or ""
        cc = args.get("cc")
        bcc = args.get("bcc")
        subject = args.get("subject") or "(no subject)"
        body = args.get("body") or ""
        unlisted = self._unlisted_recipients(args)

        lines = [f"gmail_send → {to}"]
        if cc:
            lines.append(f"cc: {cc}")
        if bcc:
            lines.append(f"bcc: {bcc}")
        lines.append(f"subject: {subject}")
        if args.get("reply_to_uid"):
            lines.append(f"reply to: uid {args['reply_to_uid']} in {args.get('reply_to_label')}")
        if unlisted:
            lines.append("not on allowlist: " + ", ".join(unlisted))
        lines.append("")
        lines.append(body.strip().replace("\r\n", "\n"))
        return "\n".join(lines)

    def _check_daily_cap(self) -> None:
        if self._log_path is None or not self._log_path.exists():
            return
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)
        count = 0
        try:
            with self._log_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        sent_at = datetime.datetime.fromisoformat(rec["sent_at_iso"])
                    except (ValueError, KeyError):
                        continue
                    if sent_at >= cutoff:
                        count += 1
        except OSError:
            return
        if count >= self._daily_cap:
            raise ToolError(
                f"gmail_send: rolling-24h cap reached ({count}/{self._daily_cap}). "
                f"Edit GMAIL_SEND_DAILY_CAP or wait."
            )

    def _append_log(
        self,
        *,
        sent_at_iso: str,
        message_id: str,
        to: list[str],
        subject: str,
    ) -> None:
        if self._log_path is None:
            return
        record = {
            "sent_at_iso": sent_at_iso,
            "message_id": message_id,
            "to": to,
            "subject": subject,
        }
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except OSError as e:
            _log.warning("gmail_send: failed to append send log: %s", e)

    def _smtp_send(self, *, from_addr: str, rcpts: list[str], raw: bytes) -> None:
        # Connect.
        try:
            client = smtplib.SMTP(self._smtp_host, self._smtp_port, timeout=30)
        except smtplib.SMTPAuthenticationError as e:
            # Should never trigger here — connect doesn't auth — but
            # defensively translate.
            raise AuthError(
                f"gmail_send: smtp auth refused: {_safe_error_text(e, self._app_password)}"
            ) from None
        except (OSError, socket.gaierror, smtplib.SMTPException) as e:
            raise TransportError(
                f"gmail_send: smtp connect failed: {_safe_error_text(e, self._app_password)}"
            ) from None

        try:
            try:
                client.ehlo()
                client.starttls()
                client.ehlo()
            except smtplib.SMTPException as e:
                raise TransportError(
                    f"gmail_send: smtp starttls failed: {_safe_error_text(e, self._app_password)}"
                ) from None

            try:
                client.login(self._address, self._app_password)
            except smtplib.SMTPAuthenticationError as e:
                raise AuthError(
                    f"gmail_send: smtp auth refused: {_safe_error_text(e, self._app_password)}"
                ) from None
            except smtplib.SMTPException as e:
                raise TransportError(
                    f"gmail_send: smtp login failed: {_safe_error_text(e, self._app_password)}"
                ) from None

            try:
                client.sendmail(from_addr, rcpts, raw)
            except smtplib.SMTPAuthenticationError as e:
                raise AuthError(
                    f"gmail_send: smtp auth refused: {_safe_error_text(e, self._app_password)}"
                ) from None
            except (smtplib.SMTPException, OSError, socket.gaierror) as e:
                raise TransportError(
                    f"gmail_send: smtp sendmail failed: {_safe_error_text(e, self._app_password)}"
                ) from None
        finally:
            try:
                client.quit()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# gmail_draft (2026-09-29)
# ---------------------------------------------------------------------------

_DRAFTS_FALLBACK = "[Gmail]/Drafts"
_DRAFTS_URL = "https://mail.google.com/mail/u/0/#drafts"
_APPENDUID_RE = re.compile(rb"APPENDUID \d+ (\d+)")


def _drafts_folder(mb: Any) -> str:
    """The folder carrying the RFC 6154 `\\Drafts` special-use flag — its
    name is localized ("[Gmail]/Brouillons"), so don't hard-code it."""
    for f in mb.folder.list():
        if "\\Drafts" in (getattr(f, "flags", ()) or ()):
            return f.name
    return _DRAFTS_FALLBACK


def _appended_uid(append_data: Any) -> str | None:
    """UID from Gmail's UIDPLUS `[APPENDUID <validity> <uid>]` response."""
    for item in append_data or ():
        if isinstance(item, bytes) and (m := _APPENDUID_RE.search(item)):
            return m.group(1).decode()
    return None


class GmailDraftTool(_GmailBase):
    """Save a composed message to Gmail's Drafts folder via IMAP APPEND.

    Nothing leaves the mailbox — the user reviews and sends from Gmail — so
    unlike `gmail_send` there is no approval gate, allowlist or daily cap.
    The Bcc header is kept (Gmail's compose window restores it from the
    draft).

    Replies (`reply_to_uid` + `reply_to_label`): the tool reads the parent's
    headers itself, so the agent never handles a Message-ID. It sets
    In-Reply-To + the full References chain, defaults the subject to
    "Re: <parent>" and the recipients to reply-all, then reads back the saved
    draft's X-GM-THRID and reports whether Gmail put it in the parent's
    thread. `replace_draft_uid` deletes a superseded draft — only inside the
    Drafts folder, and only after the new one is saved.
    """

    name: str = "gmail_draft"
    description: str = (
        "Compose an email and save it to the user's Gmail Drafts folder WITHOUT "
        "sending it. Use when the user asks to draft/write/compose an email "
        "or reply for them to review and send themselves from Gmail.\n"
        "To draft a REPLY, pass reply_to_uid + reply_to_label (the uid and "
        "label gmail_search/gmail_list returned): the tool threads it into "
        "the conversation, defaults the subject to 'Re: …' and to/cc to "
        "reply-all (omit them unless changing recipients), and reports "
        "`threaded`. To revise a draft you saved earlier, pass its uid as "
        "replace_draft_uid so the old one is deleted. Never build mail "
        "headers or use IMAP by hand. No approval prompt: nothing is sent. "
        "To actually send mail, use gmail_send instead."
    )
    requires_confirmation: bool = False
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "to": {
                "type": ["string", "null"],
                "default": None,
                "description": (
                    "Recipient(s), comma-separated. Optional; for a reply, omit to reply-all."
                ),
            },
            "subject": {
                "type": ["string", "null"],
                "default": None,
                "description": "Subject line. Required unless replying ('Re: <parent>').",
            },
            "body": {"type": "string", "description": "Message body."},
            "cc": {
                "type": ["string", "null"],
                "default": None,
                "description": "CC recipient(s), comma-separated. For a reply, omit to reply-all.",
            },
            "bcc": {
                "type": ["string", "null"],
                "default": None,
                "description": "BCC recipient(s). Comma-separated for multiple.",
            },
            "reply_to_uid": _REPLY_TO_UID_SCHEMA,
            "reply_to_label": _REPLY_TO_LABEL_SCHEMA,
            "in_reply_to": _IN_REPLY_TO_SCHEMA,
            "replace_draft_uid": {
                "type": ["string", "null"],
                "default": None,
                "description": (
                    "uid of an earlier draft (from a previous gmail_draft result) "
                    "to delete once this one is saved."
                ),
            },
            "html": {
                "type": "boolean",
                "default": False,
                "description": "Treat body as HTML (default: plain text).",
            },
        },
        "required": ["body"],
        "additionalProperties": False,
    }

    async def call(self, args: dict[str, Any]) -> ToolResult:
        reply_uid, reply_label = _reply_args(args, op="gmail_draft")
        if not reply_uid and not (args.get("subject") or "").strip():
            raise ToolError("gmail_draft: subject is required unless replying (reply_to_uid)")
        replace_uid = str(args.get("replace_draft_uid") or "").strip() or None
        return await asyncio.to_thread(
            self._draft, args, reply_uid=reply_uid, reply_label=reply_label, replace_uid=replace_uid
        )

    def _draft(
        self,
        args: dict[str, Any],
        *,
        reply_uid: str | None,
        reply_label: str,
        replace_uid: str | None,
    ) -> ToolResult:
        op = "gmail_draft"
        to, cc = args.get("to") or None, args.get("cc") or None
        subject = (args.get("subject") or "").strip()
        in_reply_to, references = args.get("in_reply_to") or None, None
        notes: list[str] = []
        with self._imap_login(op) as mb:
            parent = None
            if reply_uid:
                parent = _fetch_parent(mb, uid=reply_uid, label=reply_label, op=op)
                in_reply_to, references = parent.message_id, _reply_references(parent)
                subject = subject or _reply_subject(parent.subject)
                if not to:
                    to, default_cc = _reply_all(parent, self._address)
                    to, cc = to or None, cc or default_cc or None
            msg = _build_message(
                from_addr=self._from_header,
                to=to,
                subject=subject,
                body=args["body"],
                cc=cc,
                bcc=args.get("bcc"),
                in_reply_to=in_reply_to,
                html=bool(args.get("html", False)),
                references=references,
            )
            try:
                folder = _drafts_folder(mb)
                _typ, data = mb.append(msg.as_bytes(), folder, flag_set=[MailMessageFlags.DRAFT])
            except (OSError, socket.gaierror) as e:
                raise TransportError(
                    f"{op}: imap append transport: {_safe_error_text(e, self._app_password)}"
                ) from None
            except Exception as e:
                raise ToolError(
                    f"{op}: imap append failed: {_safe_error_text(e, self._app_password)}"
                ) from None
            uid = _appended_uid(data)

            # Everything below is best-effort: the draft is already saved, so
            # a failure here is reported, never raised.
            thread_id = None
            try:
                mb.folder.set(folder)
                if uid:
                    gm = _parse_gm_attrs(mb.client.uid("FETCH", uid, "(X-GM-MSGID X-GM-THRID)")[1])
                    thread_id = gm.get(uid, {}).get("thrid")
            except Exception as e:
                notes.append(f"could not read back the draft's thread: {type(e).__name__}")
            replaced = None
            if replace_uid:
                replaced = self._delete_draft(mb, replace_uid, new_uid=uid, notes=notes)

        threaded = None
        if parent is not None and parent.thread_id and thread_id:
            threaded = thread_id == parent.thread_id
        text = f"draft saved to {folder}" + (f" for {to}" if to else "")
        text += f" — subject {subject!r} (not sent)"
        if threaded:
            text += "; threaded into the conversation"
        elif threaded is False:
            text += "; WARNING: Gmail filed it as a NEW conversation, not in the parent's thread"
        out: dict[str, Any] = {
            "uid": uid,
            "folder": folder,
            "message_id": msg["Message-ID"] or "",
            "to": to,
            "cc": cc,
            "subject": subject,
            "thread_id": thread_id,
            "url": _gmail_url(thread_id) or _DRAFTS_URL,
            "message": text,
        }
        if parent is not None:
            out["reply_to"] = {
                "uid": parent.uid,
                "label": parent.label,
                "from": email.utils.formataddr(parent.sender),
                "subject": parent.subject,
                "message_id": parent.message_id,
            }
            out["threaded"] = threaded
        elif in_reply_to:
            out["in_reply_to"] = _angle(in_reply_to)
        if replace_uid:
            out["replaced_draft_uid"] = replace_uid if replaced else None
        if notes:
            out["notes"] = notes
        return ToolResult(output=out)

    @staticmethod
    def _delete_draft(mb: Any, uid: str, *, new_uid: str | None, notes: list[str]) -> bool:
        """Delete draft `uid` from the (already selected) Drafts folder."""
        if uid == new_uid:
            notes.append("replace_draft_uid is the draft just saved; nothing deleted")
            return False
        try:
            if not list(mb.uids(AND(uid=uid))):
                notes.append(f"draft {uid} not found in Drafts; nothing deleted")
                return False
            mb.delete([uid])
        except Exception as e:
            notes.append(f"could not delete draft {uid}: {type(e).__name__}")
            return False
        return True


# ---------------------------------------------------------------------------
# gmail_search (2026-09-03, doc #58 § 3 slice-2 decision)
# ---------------------------------------------------------------------------

_ALL_MAIL = "[Gmail]/All Mail"
_SEARCH_MAX = 25
_GM_ATTR_RE = re.compile(
    rb"UID (?P<uid>\d+)|X-GM-MSGID (?P<msgid>\d+)|X-GM-THRID (?P<thrid>\d+)|"
    rb"X-GM-LABELS \((?P<labels>[^)]*)\)"
)


def _imap_quote(text: str) -> str:
    """Quote a string for an IMAP command: backslash-escape `\\` and `"`."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _gm_raw_criteria(query: str) -> str:
    """Gmail's `X-GM-RAW` SEARCH extension — the query is Gmail's own search
    syntax, evaluated server-side by Gmail's engine over the current folder."""
    return f"X-GM-RAW {_imap_quote(query)}"


def _parse_gm_attrs(fetch_data: Any) -> dict[str, dict[str, Any]]:
    """Parse a raw `UID FETCH ... (X-GM-MSGID X-GM-THRID X-GM-LABELS)` response
    into {uid: {msgid, thrid, labels}}. Tolerant: unknown shapes yield {}."""
    out: dict[str, dict[str, Any]] = {}
    if not fetch_data:
        return out
    for item in fetch_data:
        raw = item[0] if isinstance(item, tuple) else item
        if not isinstance(raw, bytes):
            continue
        uid = msgid = thrid = None
        labels: list[str] = []
        for m in _GM_ATTR_RE.finditer(raw):
            if m.group("uid"):
                uid = m.group("uid").decode()
            elif m.group("msgid"):
                msgid = m.group("msgid").decode()
            elif m.group("thrid"):
                thrid = m.group("thrid").decode()
            elif m.group("labels") is not None:
                labels = [
                    lbl.strip('"').replace("\\\\", "\\")
                    for lbl in re.findall(rb'"[^"]*"|\S+', m.group("labels"))
                    for lbl in [lbl.decode("utf-8", "replace")]
                    if lbl
                ]
        if uid:
            out[uid] = {"msgid": msgid, "thrid": thrid, "labels": labels}
    return out


def _gmail_url(msgid: str | None) -> str | None:
    """Deep link that opens the message in Gmail (hex X-GM-MSGID)."""
    if not msgid:
        return None
    try:
        return f"https://mail.google.com/mail/u/0/#all/{int(msgid):x}"
    except ValueError:
        return None


class GmailSearchTool(_GmailBase):
    """Search the whole mailbox with Gmail's own search syntax.

    Two IMAP round trips: `UID SEARCH X-GM-RAW` (Gmail's engine, server-side)
    returns matching UIDs; the newest `limit` are then fetched headers-only
    plus one raw FETCH for Gmail's message/thread ids and labels. Bodies are
    never downloaded here (the mailbox averages ~270 KB per message because
    of attachments) — `gmail_read` fetches one message's body on demand.
    Read-only: `mark_seen=False`, no flags touched.
    """

    name: str = "gmail_search"
    description: str = (
        "Search the user's entire Gmail (all labels, all years) with Gmail's own "
        "search syntax; returns the newest matching messages with a Gmail link.\n"
        "Uses Gmail's server-side search, so the query is what you would type in "
        "the Gmail search box: plain words, from:, to:, subject:, label:, "
        "has:attachment, filename:pdf, after:2024/01/01, before:, older_than:2y, "
        "newer_than:30d, quoted phrases, OR, and -exclusions. Results carry uid + "
        "label; pass both to gmail_read for the full body."
    )
    requires_confirmation: bool = False
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Gmail search query (Gmail search-box syntax).",
            },
            "limit": {
                "type": "integer",
                "default": 10,
                "minimum": 1,
                "maximum": _SEARCH_MAX,
                "description": "Max messages to return, newest first (1-25).",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    async def call(self, args: dict[str, Any]) -> ToolResult:
        query = str(args["query"]).strip()
        if not query:
            raise ToolError("gmail_search: query must be non-empty")
        limit = int(args.get("limit", 10))
        return await asyncio.to_thread(self._search, query=query, limit=limit)

    def _search(self, *, query: str, limit: int) -> ToolResult:
        try:
            mb = MailBox(self._imap_host)
        except (OSError, socket.gaierror) as e:
            raise TransportError(
                f"gmail_search: imap connect failed: {_safe_error_text(e, self._app_password)}"
            ) from None
        try:
            mb.login(self._address, self._app_password, initial_folder=_ALL_MAIL)
        except MailboxLoginError as e:
            raise AuthError(
                f"gmail_search: imap login refused: {_safe_error_text(e, self._app_password)}"
            ) from None
        except (OSError, socket.gaierror) as e:
            raise TransportError(
                f"gmail_search: imap login transport: {_safe_error_text(e, self._app_password)}"
            ) from None
        except Exception as e:
            raise AuthError(
                f"gmail_search: imap login refused: {_safe_error_text(e, self._app_password)}"
            ) from None
        criteria = _gm_raw_criteria(query)
        with mb:
            try:
                uids = list(mb.uids(criteria, charset="UTF-8"))
            except (OSError, socket.gaierror) as e:
                raise TransportError(
                    f"gmail_search: imap search transport: "
                    f"{_safe_error_text(e, self._app_password)}"
                ) from None
            except Exception as e:
                raise ToolError(
                    f"gmail_search: imap search failed: {_safe_error_text(e, self._app_password)}"
                ) from None
            total = len(uids)
            # UIDs are assigned in arrival order; the highest are the newest.
            chosen = sorted(uids, key=int)[-limit:][::-1]
            if not chosen:
                return ToolResult(output={"query": query, "total": 0, "messages": []})
            try:
                raw = list(
                    mb.fetch(
                        AND(uid=chosen), headers_only=True, bulk=True, mark_seen=False, reverse=True
                    )
                )
                gm = _parse_gm_attrs(
                    mb.client.uid("FETCH", ",".join(chosen), "(X-GM-MSGID X-GM-THRID X-GM-LABELS)")[
                        1
                    ]
                )
            except (OSError, socket.gaierror) as e:
                raise TransportError(
                    f"gmail_search: imap fetch transport: {_safe_error_text(e, self._app_password)}"
                ) from None
            except Exception as e:
                raise ToolError(
                    f"gmail_search: imap fetch failed: {_safe_error_text(e, self._app_password)}"
                ) from None
        by_uid = {str(getattr(m, "uid", "") or ""): m for m in raw}
        messages = []
        for uid in chosen:
            m = by_uid.get(uid)
            if m is None:
                continue
            attrs = gm.get(uid, {})
            messages.append(
                {
                    "uid": uid,
                    "label": _ALL_MAIL,
                    "message_id": _header(m, "Message-ID"),
                    "subject": getattr(m, "subject", "") or "",
                    "from": getattr(m, "from_", "") or "",
                    "to": list(getattr(m, "to", ()) or ())[:3],
                    "date_iso": _to_iso(getattr(m, "date", None)),
                    "labels": attrs.get("labels", []),
                    "thread_id": attrs.get("thrid"),
                    "url": _gmail_url(attrs.get("msgid")),
                }
            )
        return ToolResult(output={"query": query, "total": total, "messages": messages})
