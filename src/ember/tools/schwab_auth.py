"""One-shot Schwab auth bootstrap.

Run from the host (NOT via the agent) the first time. After that, the
``/schwab-reauth`` Discord command (BR-FC-11) is the canonical refresh
path — no terminal access required. The browser-based OAuth flow lands
the access + refresh tokens in ``<data_dir>/schwab/.schwab_token.json``;
afterwards every Ember tool can use the cached token silently until the
refresh window closes.

Headless-safe: the script stubs ``webbrowser.get`` to a no-op controller
(no local browser is required — the URL is printed) and uses
``client_from_login_flow(interactive=False)`` so it does not pause on a
``Press ENTER`` prompt before binding the callback listener.

Usage::

    cd /srv/ember
    uv run --with schwab-py --with python-dotenv \\
        python -m ember.tools.schwab_auth

The script reads ``SCHWAB_APP_KEY`` / ``SCHWAB_APP_SECRET`` /
``SCHWAB_CALLBACK_URL`` (defaults to ``https://127.0.0.1:8182``) from
``.env``. It writes to ``<DATA_DIR>/schwab/.schwab_token.json`` with
mode 0600.

This module exists only as a `python -m` entrypoint; it is NOT imported
by the agent at runtime.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _main() -> int:
    try:
        from dotenv import load_dotenv  # type: ignore
    except ImportError:
        # python-dotenv may be absent if invoked without `--with`. Fail
        # softly — env vars from the current shell still work.
        load_dotenv = None

    if load_dotenv is not None:
        load_dotenv()

    app_key = os.environ.get("SCHWAB_APP_KEY")
    app_secret = os.environ.get("SCHWAB_APP_SECRET")
    callback_url = os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182")
    data_dir_env = os.environ.get("DATA_DIR", "/data")

    if not app_key or not app_secret:
        print(
            "error: SCHWAB_APP_KEY and SCHWAB_APP_SECRET must be set in .env",
            file=sys.stderr,
        )
        return 1

    data_dir = Path(data_dir_env) / "schwab"
    data_dir.mkdir(mode=0o700, exist_ok=True)
    try:
        os.chmod(data_dir, 0o700)
    except OSError:
        pass
    token_path = data_dir / ".schwab_token.json"

    try:
        from schwab.auth import client_from_login_flow  # type: ignore
    except ImportError as e:
        print(f"error: schwab-py not installed: {e}", file=sys.stderr)
        print(
            "rerun with `uv run --with schwab-py --with python-dotenv "
            "python -m ember.tools.schwab_auth`",
            file=sys.stderr,
        )
        return 1

    # schwab-py uses `webbrowser.get(requested_browser).open(url)` to launch
    # a browser. On a headless host this raises `webbrowser.Error: could not
    # locate runnable browser` from `.get()`. We don't need a local browser
    # here — the URL is printed and the operator opens it elsewhere. Stub
    # `webbrowser.get` to return a no-op controller so the SDK proceeds to
    # bind its callback listener and wait for the redirect.
    import webbrowser

    class _NoopBrowser:
        def open(self, *a, **kw):  # noqa: D401
            return True

    webbrowser.get = lambda *a, **kw: _NoopBrowser()  # type: ignore[assignment]

    print(f"writing token to {token_path}")
    print(f"callback URL: {callback_url}")
    print(
        "open the auth URL printed below in any browser; the OAuth redirect "
        "must reach this host on port 8182 (use ssh -L 8182:127.0.0.1:8182 if "
        "browsing from a different machine)"
    )

    # `client_from_login_flow(interactive=False)` skips easy_client's
    # `input("Press ENTER...")` prompt — webbrowser.open() is a no-op
    # via our stub, and the SDK still binds its local server on 8182
    # and blocks on the redirect. That's exactly what we want for an
    # automated/SSH-tunneled bootstrap.
    try:
        client_from_login_flow(
            api_key=app_key,
            app_secret=app_secret,
            callback_url=callback_url,
            token_path=str(token_path),
            interactive=False,
            enforce_enums=True,
        )
    except Exception as e:
        print(f"auth failed: {e}", file=sys.stderr)
        return 1

    # Tighten mode after the SDK creates the file.
    try:
        os.chmod(token_path, 0o600)
    except OSError as e:
        print(f"warning: could not chmod token file: {e}", file=sys.stderr)

    print(f"\nOK — token written to {token_path}")
    print("the agent will now be able to authenticate silently.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
