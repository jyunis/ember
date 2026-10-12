"""Shared base for iCloud CalDAV tools (v16).

Reminders and Calendar share a single CalDAV endpoint
(`caldav.icloud.com`); collections are distinguished by supported
component type (VTODO for Reminders lists, VEVENT for calendars). One
Apple-ID app-specific password covers both surfaces.

Secret hygiene (TLC-4):
- The app password is held on a private slot and intentionally elided
  from `__repr__` / `__str__`.
- All surfaced error text is filtered through `_safe_error_text` which
  defensively drops any verbatim occurrence of the password.

Library exception → Ember error mapping (see `ember.errors`):
- `caldav.lib.error.AuthorizationError` → `AuthError`
- `socket.gaierror` / `OSError` / `ConnectionError` /
  `requests.exceptions.ConnectionError` → `TransportError`
- everything else (UID-not-found, ambiguous calendar, validation) →
  `ToolError`
"""

from __future__ import annotations

import socket
from typing import Any


def _safe_error_text(exc: BaseException, password: str) -> str:
    """Render an exception for logs/messages without leaking the secret."""
    msg = f"{type(exc).__name__}"
    detail = str(exc) or ""
    if password and password in detail:
        detail = detail.replace(password, "***")
    if detail:
        msg = f"{msg}: {detail}"
    return msg


def _is_transport_exc(exc: BaseException) -> bool:
    """Best-effort: classify an exception as transport-level.

    `caldav` wraps `requests` under the hood; `requests.exceptions.
    ConnectionError` / `Timeout` and the stdlib `OSError` family both
    indicate transport failure rather than auth or app-level errors.
    Imported lazily so this module is import-safe even when `requests`
    isn't on the path (the usual case before `caldav` is installed).
    """
    if isinstance(exc, (OSError, socket.gaierror, ConnectionError, TimeoutError)):
        return True
    try:
        import requests.exceptions as _rexc  # type: ignore[import-not-found]

        if isinstance(exc, (_rexc.ConnectionError, _rexc.Timeout)):
            return True
    except Exception:
        pass
    return False


class _iCloudBase:
    """Holds creds + endpoint; provides secret-eliding repr.

    The principal/calendar lookup is deliberately deferred to per-call
    helpers so failures translate cleanly per tool (TLC-4 / FC-3) rather
    than at construction time.
    """

    def __init__(
        self,
        *,
        apple_id: str,
        app_password: str,
        caldav_url: str = "https://caldav.icloud.com",
    ) -> None:
        self._apple_id = apple_id
        self._app_password = app_password
        self._caldav_url = caldav_url

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(apple_id={self._apple_id!r}, caldav_url={self._caldav_url!r})"
        )

    __str__ = __repr__

    def _client(self) -> Any:
        """Construct a `caldav.DAVClient` bound to the configured creds.

        Imported lazily so modules that import this base remain
        importable even when `caldav` is not installed (e.g. dev box
        before `uv sync`).
        """
        import caldav  # type: ignore[import-not-found]

        return caldav.DAVClient(
            url=self._caldav_url,
            username=self._apple_id,
            password=self._app_password,
        )
