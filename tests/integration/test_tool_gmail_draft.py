"""gmail_draft + reply threading — compose, thread and save to Gmail Drafts.

Properties under test (single-author, 2026-09-29; revised after the
snow-removal thread where Ember fell back to raw IMAP in Bash):

- The message is IMAP-APPENDed (never SMTP-sent) with the `\\Draft` flag to
  the folder carrying the `\\Drafts` special-use flag (localized names work);
  `[Gmail]/Drafts` is the fallback when no folder carries the flag.
- Headers never RFC 2047-encode a Message-ID: long Outlook ids in
  In-Reply-To/References survive verbatim (regression: 2026-09-29) — for
  gmail_send too.
- `reply_to_uid` + `reply_to_label`: the tool fetches the parent itself and
  sets In-Reply-To, the full References chain, "Re: <subject>" (no double
  Re:), and reply-all recipients (Reply-To/sender in To, others in Cc, the
  account removed, duplicates removed; own-message replies keep the original
  audience). Explicit to/cc/subject win. The saved draft's X-GM-THRID is
  compared with the parent's → `threaded`.
- `reply_to_uid` without a label, or together with `in_reply_to` → ToolError;
  an unknown uid → ToolError naming the label; a parent without Message-ID →
  ToolError.
- `replace_draft_uid` deletes the old draft only after the new one is saved,
  only within Drafts; a missing uid or delete failure is a note, not an error.
- gmail_read / gmail_search expose `message_id`.
- From carries the configured display name; Bcc is kept in drafts.
- Errors: login refusal → AuthError; socket failure → TransportError;
  APPEND rejection → ToolError; the app password never leaks.
- Registry (TLC-6): missing body / unknown args never reach `call`; not
  confirmation-gated; every gmail tool wraps into an MCP bridge signature.
"""

from __future__ import annotations

import email
import email.policy
import socket
from typing import Any

import pytest

from ember.errors import AuthError, ToolError, TransportError
from ember.tools import gmail as gmail_module
from ember.tools.dynamic import make_bridge_wrapper
from ember.tools.gmail import (
    GmailDraftTool,
    GmailListTool,
    GmailReadTool,
    GmailSearchTool,
    GmailSendTool,
)
from ember.tools.registry import Registry, ToolValidationError

SECRET = "pw-secret-draft"
ME = "alice@example.com"
ALL_MAIL = "[Gmail]/All Mail"
DRAFTS = "[Gmail]/Drafts"
OUTLOOK_ID = "<AB1CD23EF45670A1B2C3D4E5F6A7B8C9@AB1CD23EF4567.namprd06.prod.outlook.com>"
PARENT_REFS = "<11111111-2222-3333@acme-grounds.example>\r\n <44444444-5555@Spark>"
PARENT_THRID = "1811111111111111111"


class _Addr:
    def __init__(self, name: str, addr: str) -> None:
        self.name = name
        self.email = addr


class _Msg:
    """imap-tools MailMessage stand-in: raw `headers`, parsed `*_values`."""

    def __init__(
        self,
        uid: str,
        *,
        message_id: str | None = OUTLOOK_ID,
        references: str | None = PARENT_REFS,
        subject: str = "Service Contract Renewal",
        sender: tuple[str, str] = ("Sam", "info@acme-grounds.example"),
        reply_to: tuple[tuple[str, str], ...] = (),
        to: tuple[tuple[str, str], ...] = (("Alice Example", ME),),
        cc: tuple[tuple[str, str], ...] = (("", "ben@acme-grounds.example"),),
    ) -> None:
        self.uid = uid
        self.subject = subject
        self.from_ = sender[1]
        self.from_values = _Addr(*sender)
        self.reply_to_values = tuple(_Addr(*a) for a in reply_to)
        self.to_values = tuple(_Addr(*a) for a in to)
        self.cc_values = tuple(_Addr(*a) for a in cc)
        self.to = tuple(a[1] for a in to)
        self.cc = tuple(a[1] for a in cc)
        self.date = None
        self.text = "body"
        self.html = ""
        self.flags = ()
        self.attachments = []
        self.headers: dict[str, tuple[str, ...]] = {}
        if message_id is not None:
            self.headers["message-id"] = (message_id,)
        if references is not None:
            self.headers["references"] = (references,)


class _Folder:
    def __init__(self, name: str, flags: tuple[str, ...]) -> None:
        self.name = name
        self.flags = flags


class _State:
    def __init__(self) -> None:
        self.folders = [
            _Folder("INBOX", ("\\HasNoChildren",)),
            _Folder("[Gmail]/Sent Mail", ("\\HasNoChildren", "\\Sent")),
            _Folder(DRAFTS, ("\\HasNoChildren", "\\Drafts")),
        ]
        # folder -> uid -> message
        self.boxes: dict[str, dict[str, _Msg]] = {
            ALL_MAIL: {"348552": _Msg("348552")},
            DRAFTS: {"37527": _Msg("37527", subject="old draft")},
        }
        self.thrids: dict[tuple[str, str], str] = {(ALL_MAIL, "348552"): PARENT_THRID}
        self.draft_thrid = PARENT_THRID  # thread Gmail assigns the appended draft
        self.selected: str | None = None
        self.appends: list[dict[str, Any]] = []
        self.deleted: list[tuple[str, list[str]]] = []
        self.login_calls: list[dict[str, Any]] = []
        self.append_response: tuple[str, list[Any]] = (
            "OK",
            [b"[APPENDUID 11 4242] (Success)"],
        )
        self.ctor_raises: BaseException | None = None
        self.login_raises: BaseException | None = None
        self.append_raises: BaseException | None = None
        self.delete_raises: BaseException | None = None


def _ids(criteria: Any) -> list[str]:
    # imap_tools AND(uid=...) renders as "(UID a,b,c)"
    return str(criteria).strip("()").split(" ", 1)[1].split(",")


def _install(monkeypatch: pytest.MonkeyPatch, st: _State) -> None:
    class _FolderManager:
        def list(self, *a: Any, **k: Any) -> list[_Folder]:
            return list(st.folders)

        def set(self, name: str) -> None:
            st.selected = name

    class _Client:
        def uid(self, cmd: str, uid: str, what: str):
            assert cmd == "FETCH"
            key = (st.selected, uid)
            if key not in st.thrids:
                return ("OK", [None])
            return ("OK", [(f"1 (X-GM-THRID {st.thrids[key]} X-GM-MSGID 99 UID {uid})".encode(),)])

    class _FakeMailBox:
        def __init__(self, host: str, *a: Any, **k: Any) -> None:
            if st.ctor_raises is not None:
                raise st.ctor_raises
            self.folder = _FolderManager()
            self.client = _Client()

        def login(self, user: str, password: str, initial_folder: str | None = "INBOX"):
            st.login_calls.append({"user": user, "initial_folder": initial_folder})
            if st.login_raises is not None:
                raise st.login_raises
            st.selected = initial_folder
            return self

        def __enter__(self):
            return self

        def __exit__(self, *exc: Any) -> bool:
            return False

        def fetch(self, criteria: Any, **kwargs: Any):
            box = st.boxes.get(st.selected or "", {})
            return iter([box[u] for u in _ids(criteria) if u in box])

        def uids(self, criteria: Any, *a: Any, **k: Any) -> list[str]:
            box = st.boxes.get(st.selected or "", {})
            return [u for u in _ids(criteria) if u in box]

        def append(self, message: bytes, folder: str = "INBOX", dt=None, flag_set=None):
            if st.append_raises is not None:
                raise st.append_raises
            st.appends.append({"raw": message, "folder": folder, "flag_set": list(flag_set or [])})
            uid = gmail_module._appended_uid(st.append_response[1])
            if uid:
                st.boxes.setdefault(folder, {})[uid] = _Msg(uid)
                st.thrids[(folder, uid)] = st.draft_thrid
            return st.append_response

        def delete(self, uid_list: list[str]):
            if st.delete_raises is not None:
                raise st.delete_raises
            st.deleted.append((st.selected or "", list(uid_list)))
            for u in uid_list:
                st.boxes.get(st.selected or "", {}).pop(u, None)

    monkeypatch.setattr(gmail_module, "MailBox", _FakeMailBox, raising=True)


@pytest.fixture
def state(monkeypatch: pytest.MonkeyPatch) -> _State:
    st = _State()
    _install(monkeypatch, st)

    def _no_smtp(*a: Any, **k: Any):
        raise AssertionError("gmail_draft must never touch SMTP")

    monkeypatch.setattr(gmail_module.smtplib, "SMTP", _no_smtp, raising=True)
    return st


def _tool(**kw: Any) -> GmailDraftTool:
    return GmailDraftTool(address=ME, app_password=SECRET, **kw)


def _parsed(st: _State, i: int = 0) -> email.message.EmailMessage:
    return email.message_from_bytes(st.appends[i]["raw"], policy=email.policy.default)


def _header_block(raw: bytes) -> str:
    return raw.split(b"\n\n", 1)[0].decode()


REPLY = {"reply_to_uid": "348552", "reply_to_label": ALL_MAIL, "body": "Thanks, Sam."}


class TestNewDraft:
    async def test_appends_to_drafts_with_draft_flag(self, state: _State) -> None:
        res = await _tool().call({"to": "bob@x.com", "subject": "Lunch?", "body": "Free Thursday?"})
        assert state.appends[0]["folder"] == DRAFTS
        assert state.appends[0]["flag_set"] == ["\\Draft"]
        msg = _parsed(state)
        assert msg["From"] == ME
        assert msg["To"] == "bob@x.com"
        assert msg["Subject"] == "Lunch?"
        assert msg["In-Reply-To"] is None
        assert msg.get_body(preferencelist=("plain",)).get_content().strip() == "Free Thursday?"
        assert msg["Message-ID"].endswith("@example.com>")
        out = res.output
        assert out["uid"] == "4242"
        assert out["folder"] == DRAFTS
        assert out["message_id"] == msg["Message-ID"]
        assert "not sent" in out["message"]
        assert "threaded" not in out

    async def test_display_name_in_from(self, state: _State) -> None:
        await _tool(from_name="Alice Example").call({"subject": "S", "body": "B"})
        assert _parsed(state)["From"] == f"Alice Example <{ME}>"

    async def test_cc_bcc_kept(self, state: _State) -> None:
        await _tool().call(
            {"to": "b@x.com", "subject": "S", "body": "B", "cc": "c@x.com", "bcc": "d@x.com"}
        )
        msg = _parsed(state)
        assert msg["Cc"] == "c@x.com"
        assert msg["Bcc"] == "d@x.com"

    async def test_html_body(self, state: _State) -> None:
        await _tool().call(
            {
                "to": "b@x.com",
                "subject": "S",
                "body": "<p>hi &amp; bye</p><ul><li>one</li></ul>",
                "html": True,
            }
        )
        msg = _parsed(state)
        assert msg.get_body(preferencelist=("html",)).get_content().startswith("<p>hi &amp; bye")
        assert msg.get_body(preferencelist=("plain",)).get_content() == "hi & bye\n- one\n"

    async def test_mail_client_friendly_body(self, state: _State) -> None:
        """Regression (2026-09-29): Spark dropped the body of a lone 8bit
        text/plain draft on "Edit draft". Drafts are multipart/alternative,
        quoted-printable, 7-bit clean, with QP lines within 76 chars."""
        body = (
            "Hi Sam,\n\nSince \u00a710.2 makes it "
            + "long " * 60
            + "\n<x> & y\n\nThanks,\nAlice\n"
        )
        await _tool().call({"to": "b@x.com", "subject": "S", "body": body})
        raw = state.appends[0]["raw"]
        assert all(b < 128 for b in raw)
        head, _, rest = raw.partition(b"\n\n")
        assert max(len(line) for line in rest.split(b"\n")) <= 78  # 76 + CRLF slack
        msg = _parsed(state)
        assert msg.get_content_type() == "multipart/alternative"
        plain = msg.get_body(preferencelist=("plain",))
        html = msg.get_body(preferencelist=("html",))
        assert plain["Content-Transfer-Encoding"] == "quoted-printable"
        assert html["Content-Transfer-Encoding"] == "quoted-printable"
        assert plain.get_content() == body
        assert "<p>Hi Sam,</p>" in html.get_content()
        assert "&lt;x&gt; &amp; y" in html.get_content()
        assert "<br>" in html.get_content()

    async def test_recipient_optional(self, state: _State) -> None:
        res = await _tool().call({"subject": "Notes", "body": "todo"})
        assert _parsed(state)["To"] is None
        assert " for " not in res.output["message"]

    async def test_subject_required_without_reply(self, state: _State) -> None:
        with pytest.raises(ToolError, match="subject is required"):
            await _tool().call({"to": "b@x.com", "body": "B"})
        assert state.appends == []

    async def test_localized_drafts_folder_found_by_flag(self, state: _State) -> None:
        state.folders[2] = _Folder("[Gmail]/Brouillons", ("\\Drafts",))
        await _tool().call({"subject": "S", "body": "B"})
        assert state.appends[0]["folder"] == "[Gmail]/Brouillons"

    async def test_fallback_folder_when_no_special_use_flag(self, state: _State) -> None:
        state.folders = [_Folder("INBOX", ())]
        await _tool().call({"subject": "S", "body": "B"})
        assert state.appends[0]["folder"] == DRAFTS

    async def test_uid_none_without_appenduid(self, state: _State) -> None:
        state.append_response = ("OK", [b"(Success)"])
        res = await _tool().call({"subject": "S", "body": "B"})
        assert res.output["uid"] is None
        assert res.output["url"] == "https://mail.google.com/mail/u/0/#drafts"

    async def test_login_does_not_select_a_folder(self, state: _State) -> None:
        await _tool().call({"subject": "S", "body": "B"})
        assert state.login_calls == [{"user": ME, "initial_folder": None}]

    async def test_raw_in_reply_to_is_not_encoded(self, state: _State) -> None:
        res = await _tool().call(
            {"to": "b@x.com", "subject": "Re: S", "body": "B", "in_reply_to": OUTLOOK_ID[1:-1]}
        )
        head = _header_block(state.appends[0]["raw"])
        assert f"In-Reply-To: {OUTLOOK_ID}" in head
        assert f"References: {OUTLOOK_ID}" in head
        assert "=?" not in head
        assert res.output["in_reply_to"] == OUTLOOK_ID


class TestReply:
    async def test_threading_headers_from_parent(self, state: _State) -> None:
        await _tool().call(REPLY)
        raw = state.appends[0]["raw"]
        head = _header_block(raw)
        assert "=?" not in head  # regression: Outlook ids were RFC 2047-encoded
        msg = _parsed(state)
        assert msg["In-Reply-To"] == OUTLOOK_ID
        assert msg["References"].split() == [
            "<11111111-2222-3333@acme-grounds.example>",
            "<44444444-5555@Spark>",
            OUTLOOK_ID,
        ]
        assert msg["Subject"] == "Re: Service Contract Renewal"
        assert state.boxes[ALL_MAIL]  # parent untouched

    async def test_reply_all_defaults(self, state: _State) -> None:
        state.boxes[ALL_MAIL]["348552"] = _Msg(
            "348552",
            to=(("Alice Example", ME), ("Sam", "info@acme-grounds.example")),
            cc=(("", "ben@acme-grounds.example"), ("", "ALICE@example.com")),
        )
        res = await _tool().call(REPLY)
        msg = _parsed(state)
        assert msg["To"] == "Sam <info@acme-grounds.example>"
        assert msg["Cc"] == "ben@acme-grounds.example"
        assert res.output["to"] == "Sam <info@acme-grounds.example>"
        assert res.output["cc"] == "ben@acme-grounds.example"

    async def test_reply_to_header_wins_over_sender(self, state: _State) -> None:
        state.boxes[ALL_MAIL]["348552"] = _Msg("348552", reply_to=(("Desk", "desk@x.com"),), cc=())
        await _tool().call(REPLY)
        assert _parsed(state)["To"] == "Desk <desk@x.com>"

    async def test_reply_to_own_message_keeps_audience(self, state: _State) -> None:
        state.boxes[ALL_MAIL]["348552"] = _Msg(
            "348552",
            sender=("Alice Example", ME),
            to=(("Alfie", "alfie@x.com"),),
            cc=(("", "caitlin@x.com"),),
        )
        await _tool().call(REPLY)
        msg = _parsed(state)
        assert msg["To"] == "Alfie <alfie@x.com>"
        assert msg["Cc"] == "caitlin@x.com"

    async def test_display_name_with_comma_is_quoted(self, state: _State) -> None:
        state.boxes[ALL_MAIL]["348552"] = _Msg("348552", sender=("Doe, John", "john@x.com"), cc=())
        await _tool().call(REPLY)
        assert _parsed(state)["To"].addresses[0].addr_spec == "john@x.com"

    async def test_explicit_recipients_and_subject_win(self, state: _State) -> None:
        await _tool().call({**REPLY, "to": "only@x.com", "subject": "Different"})
        msg = _parsed(state)
        assert msg["To"] == "only@x.com"
        assert msg["Cc"] is None
        assert msg["Subject"] == "Different"
        assert msg["In-Reply-To"] == OUTLOOK_ID

    async def test_explicit_cc_overrides_default_cc(self, state: _State) -> None:
        await _tool().call({**REPLY, "cc": "x@x.com"})
        msg = _parsed(state)
        assert msg["To"] == "Sam <info@acme-grounds.example>"
        assert msg["Cc"] == "x@x.com"

    async def test_no_double_re(self, state: _State) -> None:
        state.boxes[ALL_MAIL]["348552"] = _Msg("348552", subject="RE: Plumbing")
        await _tool().call(REPLY)
        assert _parsed(state)["Subject"] == "RE: Plumbing"

    async def test_parent_without_references(self, state: _State) -> None:
        state.boxes[ALL_MAIL]["348552"] = _Msg("348552", references=None)
        await _tool().call(REPLY)
        assert _parsed(state)["References"] == OUTLOOK_ID

    async def test_threaded_true_and_thread_link(self, state: _State) -> None:
        res = await _tool().call(REPLY)
        out = res.output
        assert out["threaded"] is True
        assert out["thread_id"] == PARENT_THRID
        assert out["url"] == f"https://mail.google.com/mail/u/0/#all/{int(PARENT_THRID):x}"
        assert "threaded into the conversation" in out["message"]
        assert out["reply_to"] == {
            "uid": "348552",
            "label": ALL_MAIL,
            "from": "Sam <info@acme-grounds.example>",
            "subject": "Service Contract Renewal",
            "message_id": OUTLOOK_ID,
        }

    async def test_threaded_false_is_flagged(self, state: _State) -> None:
        state.draft_thrid = "1822222222222222222"
        res = await _tool().call(REPLY)
        assert res.output["threaded"] is False
        assert "WARNING" in res.output["message"]

    async def test_threaded_unknown_when_readback_fails(self, state: _State) -> None:
        state.append_response = ("OK", [b"(Success)"])  # no APPENDUID → no read-back
        res = await _tool().call(REPLY)
        assert res.output["threaded"] is None
        assert len(state.appends) == 1

    async def test_label_required(self, state: _State) -> None:
        with pytest.raises(ToolError, match="reply_to_label is required"):
            await _tool().call({"reply_to_uid": "348552", "body": "B"})
        assert state.appends == []

    async def test_not_both_reply_forms(self, state: _State) -> None:
        with pytest.raises(ToolError, match="not both"):
            await _tool().call({**REPLY, "in_reply_to": OUTLOOK_ID})

    async def test_unknown_parent_names_the_label(self, state: _State) -> None:
        with pytest.raises(ToolError, match=r"not found in 'INBOX'"):
            await _tool().call({**REPLY, "reply_to_label": "INBOX"})
        assert state.appends == []

    async def test_parent_without_message_id(self, state: _State) -> None:
        state.boxes[ALL_MAIL]["348552"] = _Msg("348552", message_id=None)
        with pytest.raises(ToolError, match="no Message-ID"):
            await _tool().call(REPLY)
        assert state.appends == []


class TestReplaceDraft:
    async def test_old_draft_deleted_after_new_saved(self, state: _State) -> None:
        res = await _tool().call({**REPLY, "replace_draft_uid": "37527"})
        assert state.deleted == [(DRAFTS, ["37527"])]
        assert "37527" not in state.boxes[DRAFTS]
        assert "4242" in state.boxes[DRAFTS]
        assert res.output["replaced_draft_uid"] == "37527"

    async def test_missing_old_draft_is_a_note(self, state: _State) -> None:
        res = await _tool().call({"subject": "S", "body": "B", "replace_draft_uid": "999"})
        assert state.deleted == []
        assert res.output["replaced_draft_uid"] is None
        assert any("not found" in n for n in res.output["notes"])

    async def test_never_deletes_the_new_draft(self, state: _State) -> None:
        res = await _tool().call({"subject": "S", "body": "B", "replace_draft_uid": "4242"})
        assert state.deleted == []
        assert "4242" in state.boxes[DRAFTS]
        assert res.output["replaced_draft_uid"] is None

    async def test_delete_failure_is_a_note(self, state: _State) -> None:
        state.delete_raises = RuntimeError("NO")
        res = await _tool().call({"subject": "S", "body": "B", "replace_draft_uid": "37527"})
        assert res.output["uid"] == "4242"
        assert res.output["replaced_draft_uid"] is None
        assert any("could not delete" in n for n in res.output["notes"])

    async def test_no_delete_when_append_fails(self, state: _State) -> None:
        state.append_raises = RuntimeError("NO [OVERQUOTA]")
        with pytest.raises(ToolError):
            await _tool().call({"subject": "S", "body": "B", "replace_draft_uid": "37527"})
        assert state.deleted == []


class TestSendReply:
    """gmail_send shares the parent lookup and the non-encoding policy."""

    @pytest.fixture
    def sent(self, state: _State, monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
        out: list[bytes] = []

        class _SMTP:
            def __init__(self, *a: Any, **k: Any) -> None:
                pass

            def ehlo(self) -> None:
                pass

            def starttls(self) -> None:
                pass

            def login(self, *a: Any) -> None:
                pass

            def sendmail(self, frm: str, rcpts: list[str], raw: bytes) -> None:
                out.append(raw)

            def quit(self) -> None:
                pass

        monkeypatch.setattr(gmail_module.smtplib, "SMTP", _SMTP, raising=True)
        return out

    def _send(self) -> GmailSendTool:
        return GmailSendTool(address=ME, app_password=SECRET, from_name="Alice Example")

    async def test_reply_to_uid_threads_the_send(self, sent: list[bytes]) -> None:
        await self._send().call(
            {"to": "info@acme-grounds.example", "subject": "Re: S", "body": "B", **REPLY}
        )
        head = _header_block(sent[0])
        assert "=?" not in head
        msg = email.message_from_bytes(sent[0], policy=email.policy.default)
        assert msg["In-Reply-To"] == OUTLOOK_ID
        assert msg["References"].split()[-1] == OUTLOOK_ID
        assert msg["From"] == f"Alice Example <{ME}>"
        assert msg["To"] == "info@acme-grounds.example"  # recipients stay explicit

    async def test_raw_in_reply_to_not_encoded(self, sent: list[bytes]) -> None:
        await self._send().call(
            {"to": "a@x.com", "subject": "Re: S", "body": "B", "in_reply_to": OUTLOOK_ID}
        )
        assert f"In-Reply-To: {OUTLOOK_ID}" in _header_block(sent[0])

    def test_preview_names_the_parent(self) -> None:
        text = self._send().summarize_action(
            {"to": "a@x.com", "subject": "S", "body": "B", **REPLY}
        )
        assert f"reply to: uid 348552 in {ALL_MAIL}" in text


class TestMessageIdExposed:
    async def test_gmail_read_returns_message_id(self, state: _State) -> None:
        tool = GmailReadTool(address=ME, app_password=SECRET)
        res = await tool.call({"uid": "348552", "label": ALL_MAIL})
        assert res.output["message_id"] == OUTLOOK_ID


class TestErrors:
    async def test_login_refused_is_auth_error(self, state: _State) -> None:
        state.login_raises = Exception(f"AUTHENTICATIONFAILED for {SECRET}")
        with pytest.raises(AuthError) as ei:
            await _tool().call({"subject": "S", "body": "B"})
        assert SECRET not in str(ei.value)

    async def test_connect_failure_is_transport_error(self, state: _State) -> None:
        state.ctor_raises = socket.gaierror("no dns")
        with pytest.raises(TransportError):
            await _tool().call({"subject": "S", "body": "B"})

    async def test_append_socket_failure_is_transport_error(self, state: _State) -> None:
        state.append_raises = OSError("reset")
        with pytest.raises(TransportError):
            await _tool().call({"subject": "S", "body": "B"})

    async def test_append_rejected_is_tool_error(self, state: _State) -> None:
        state.append_raises = RuntimeError("NO [OVERQUOTA]")
        with pytest.raises(ToolError) as ei:
            await _tool().call({"subject": "S", "body": "B"})
        assert not isinstance(ei.value, (AuthError, TransportError))

    def test_password_not_in_repr(self) -> None:
        assert SECRET not in repr(_tool(from_name="J"))


class TestRegistry:
    def test_not_confirmation_gated(self) -> None:
        assert _tool().requires_confirmation is False

    @pytest.mark.parametrize(
        "args",
        [
            {"subject": "S"},
            {"subject": "S", "body": "B", "attachments": []},
        ],
    )
    async def test_invalid_args_never_reach_call(self, state: _State, args: dict) -> None:
        reg = Registry()
        reg.register(_tool())
        with pytest.raises(ToolValidationError):
            await reg.call_tool("gmail_draft", args)
        assert state.appends == []

    async def test_dispatch_through_registry_without_confirmation(self, state: _State) -> None:
        reg = Registry()
        reg.register(_tool())
        res = await reg.call_tool("gmail_draft", REPLY)
        assert res.output["threaded"] is True

    def test_every_gmail_tool_wraps_into_bridge_signature(self) -> None:
        async def _noop(args: dict[str, Any]) -> Any:
            return None

        common = {"address": ME, "app_password": SECRET}
        for tool in (
            GmailListTool(**common),
            GmailReadTool(**common),
            GmailSendTool(**common),
            GmailDraftTool(**common),
            GmailSearchTool(**common),
        ):
            wrapper = make_bridge_wrapper(
                name=tool.name,
                description=tool.description,
                input_schema=tool.input_schema,
                invoker=_noop,
            )
            assert set(wrapper.__signature__.parameters) == set(tool.input_schema["properties"])
