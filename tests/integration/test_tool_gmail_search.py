"""gmail_search — Gmail's own search (X-GM-RAW) over the whole mailbox.

Properties under test (single-author, 2026-09-03; doc #58 § 3 slice-2 decision):

- The IMAP search criteria is `X-GM-RAW "<query>"` with IMAP quoting
  (backslash-escaped `"` and `\\`), sent with charset UTF-8, in the
  `[Gmail]/All Mail` folder (cross-label, all years).
- Only the newest `limit` UIDs (highest UIDs) are fetched, newest first,
  headers-only, never marking seen; `total` reports the full match count.
- Each hit carries `uid` + `label` (so `gmail_read(uid, label)` works),
  subject/from/to/date, Gmail labels, thread id, and a Gmail deep link
  built from the hex X-GM-MSGID.
- Zero matches → empty `messages` and no fetch; empty query → ToolError;
  login refusal → AuthError; socket failure → TransportError.
- Registry (TLC-6): missing query / limit out of 1..25 never reach `call`.
- `requires_confirmation` is False; a 25-hit result serializes under the
  16 KB payload-envelope threshold (TLC-26).

Documented choices: the fake exposes `.client.uid(...)` returning the raw
imaplib shape `('OK', [(b'... (X-GM-... UID n)',), ...])`; the tool is
tolerant of missing Gmail attributes (labels → [], url → None).
"""

from __future__ import annotations

import datetime
import json
import socket
from typing import Any

import pytest

from ember.errors import AuthError, ToolError, TransportError
from ember.tools import gmail as gmail_module
from ember.tools.gmail import GmailSearchTool
from ember.tools.registry import Registry, ToolValidationError

ALL_MAIL = "[Gmail]/All Mail"


class _Msg:
    def __init__(self, uid: str, subject: str, from_: str, day: int) -> None:
        self.uid = uid
        self.subject = subject
        self.from_ = from_
        self.to = ("alice@example.com",)
        self.date = datetime.datetime(2026, 6, day, 12, 0, tzinfo=datetime.UTC)
        self.text = "BODY SHOULD NOT BE READ"
        self.html = ""
        self.flags = ()
        self.attachments = []
        self.headers = {"message-id": (f"<m{uid}@x.com>",)}


class _State:
    def __init__(self) -> None:
        self.msgs: dict[str, _Msg] = {}
        self.gm: dict[str, tuple[str, str, str]] = {}  # uid -> (msgid, thrid, labels-raw)
        self.uids_calls: list[dict[str, Any]] = []
        self.fetch_calls: list[dict[str, Any]] = []
        self.raw_calls: list[tuple[str, ...]] = []
        self.login_calls: list[dict[str, Any]] = []
        self.login_raises: BaseException | None = None
        self.ctor_raises: BaseException | None = None
        self.uids_raises: BaseException | None = None
        self.match: list[str] = []

    def add(
        self, uid: str, subject: str, from_: str, day: int, msgid: str, thrid: str, labels: str
    ):
        self.msgs[uid] = _Msg(uid, subject, from_, day)
        self.gm[uid] = (msgid, thrid, labels)


def _install(monkeypatch: pytest.MonkeyPatch, st: _State) -> None:
    class _Client:
        def uid(self, cmd: str, *args: str):
            st.raw_calls.append((cmd, *args))
            assert cmd == "FETCH"
            out = []
            for uid in args[0].split(","):
                if uid in st.gm:
                    msgid, thrid, labels = st.gm[uid]
                    out.append(
                        (
                            f"1 (X-GM-THRID {thrid} X-GM-MSGID {msgid} X-GM-LABELS ({labels}) "
                            f"UID {uid})".encode(),
                        )
                    )
            return ("OK", out)

    class _FakeMailBox:
        def __init__(self, host: str, *a: Any, **k: Any) -> None:
            if st.ctor_raises is not None:
                raise st.ctor_raises
            self.client = _Client()

        def login(self, user: str, password: str, initial_folder: str = "INBOX"):
            st.login_calls.append({"user": user, "initial_folder": initial_folder})
            if st.login_raises is not None:
                raise st.login_raises
            return self

        def __enter__(self):
            return self

        def __exit__(self, *exc: Any) -> bool:
            return False

        def uids(self, criteria: Any, charset: str = "US-ASCII", **kw: Any) -> list[str]:
            st.uids_calls.append({"criteria": str(criteria), "charset": charset})
            if st.uids_raises is not None:
                raise st.uids_raises
            return list(st.match)

        def fetch(self, criteria: Any, **kwargs: Any):
            st.fetch_calls.append({"criteria": str(criteria), "kwargs": kwargs})
            wanted = str(criteria)
            # imap_tools AND(uid=[...]) renders as "(UID a,b,c)"
            ids = wanted.strip("()").split(" ", 1)[1].split(",")
            return iter([st.msgs[u] for u in ids if u in st.msgs])

    monkeypatch.setattr(gmail_module, "MailBox", _FakeMailBox, raising=True)


def _tool() -> GmailSearchTool:
    return GmailSearchTool(address="alice@example.com", app_password="pw-secret")


@pytest.fixture
def state(monkeypatch: pytest.MonkeyPatch) -> _State:
    st = _State()
    st.add(
        "10",
        "Old invoice",
        "old@x.com",
        1,
        "1800000000000000010",
        "1800000000000000001",
        "Financial",
    )
    st.add(
        "42",
        "Sump pump invoice",
        "plumber@x.com",
        20,
        "1800000000000000042",
        "1800000000000000001",
        '"\\\\Important" Financial "Real Estate"',
    )
    st.add(
        "43", "Receipt", "shop@x.com", 21, "1800000000000000043", "1800000000000000002", "Purchases"
    )
    st.match = ["10", "43", "42"]
    _install(monkeypatch, st)
    return st


class TestCriteria:
    async def test_x_gm_raw_with_imap_quoting_and_utf8(self, state: _State) -> None:
        await _tool().call({"query": 'from:plumber "sump pump" \\ after:2026/06/01'})
        call = state.uids_calls[0]
        assert call["criteria"] == 'X-GM-RAW "from:plumber \\"sump pump\\" \\\\ after:2026/06/01"'
        assert call["charset"] == "UTF-8"

    async def test_searches_all_mail_folder(self, state: _State) -> None:
        await _tool().call({"query": "invoice"})
        assert state.login_calls[0]["initial_folder"] == ALL_MAIL

    async def test_empty_query_rejected_before_imap(self, state: _State) -> None:
        with pytest.raises(ToolError):
            await _tool().call({"query": "   "})
        assert state.login_calls == []


class TestResults:
    async def test_newest_first_limited_headers_only(self, state: _State) -> None:
        res = await _tool().call({"query": "invoice", "limit": 2})
        out = res.output
        assert out["total"] == 3
        assert [m["uid"] for m in out["messages"]] == ["43", "42"]
        kw = state.fetch_calls[0]["kwargs"]
        assert kw.get("headers_only") is True
        assert kw.get("mark_seen") is False
        assert "10" not in state.fetch_calls[0]["criteria"]

    async def test_hit_shape_labels_thread_url(self, state: _State) -> None:
        res = await _tool().call({"query": "sump"})
        hit = next(m for m in res.output["messages"] if m["uid"] == "42")
        assert hit["label"] == ALL_MAIL
        assert hit["subject"] == "Sump pump invoice"
        assert hit["from"] == "plumber@x.com"
        assert hit["date_iso"].startswith("2026-06-20")
        assert hit["labels"] == ["\\Important", "Financial", "Real Estate"]
        assert hit["thread_id"] == "1800000000000000001"
        assert hit["url"] == f"https://mail.google.com/mail/u/0/#all/{0x18FAE27693B4002A:x}"
        assert hit["message_id"] == f"<m{hit['uid']}@x.com>"
        assert "BODY SHOULD NOT BE READ" not in json.dumps(res.output)

    async def test_zero_matches_no_fetch(self, state: _State) -> None:
        state.match = []
        res = await _tool().call({"query": "nothing"})
        assert res.output == {"query": "nothing", "total": 0, "messages": []}
        assert state.fetch_calls == [] and state.raw_calls == []

    async def test_missing_gm_attrs_tolerated(self, state: _State) -> None:
        state.gm.pop("43")
        res = await _tool().call({"query": "receipt", "limit": 1})
        hit = res.output["messages"][0]
        assert hit["uid"] == "43" and hit["labels"] == [] and hit["url"] is None

    async def test_size_budget_25_hits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        st = _State()
        for i in range(100, 125):
            st.add(
                str(i),
                "Subject " * 12,
                "someone.with.a.long.address@example-domain.com",
                1,
                str(1800000000000000000 + i),
                "1800000000000000001",
                '"Real Estate" Financial Purchases',
            )
        st.match = [str(i) for i in range(100, 125)]
        _install(monkeypatch, st)
        res = await _tool().call({"query": "x", "limit": 25})
        assert len(res.output["messages"]) == 25
        assert len(json.dumps(res.output, ensure_ascii=False).encode()) < 16384


class TestErrors:
    async def test_login_refused_is_auth_error(self, state: _State) -> None:
        state.login_raises = Exception("AUTHENTICATIONFAILED")
        with pytest.raises(AuthError):
            await _tool().call({"query": "x"})

    async def test_socket_failure_is_transport_error(self, state: _State) -> None:
        state.ctor_raises = socket.gaierror("dns")
        with pytest.raises(TransportError):
            await _tool().call({"query": "x"})

    async def test_search_failure_is_tool_error_without_secret(self, state: _State) -> None:
        state.uids_raises = RuntimeError("bad criteria pw-secret")
        with pytest.raises(ToolError) as ei:
            await _tool().call({"query": "x"})
        assert "pw-secret" not in str(ei.value)


class TestRegistry:
    def test_requires_confirmation_false(self) -> None:
        assert _tool().requires_confirmation is False

    @pytest.mark.parametrize(
        "args", [{}, {"query": "x", "limit": 0}, {"query": "x", "limit": 26}, {"query": 5}]
    )
    async def test_schema_validation_blocks_bad_args(self, state: _State, args: dict) -> None:
        reg = Registry()
        reg.register(_tool())
        with pytest.raises(ToolValidationError):
            await reg.call_tool("gmail_search", args)
        assert state.login_calls == []
