"""Logging setup.

Plain stderr logging with the standard library. TLC-4 redaction lives
here as a logging filter that scrubs known secret values from log
records — a defense layer in case a secret gets stringified into a log
message.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterable


class _SecretRedactor(logging.Filter):
    """Replace any known secret value with `<redacted>` in log output."""

    REPLACEMENT = "<redacted>"

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        msg = record.getMessage()
        scrubbed = msg
        for secret in self._secrets:
            if secret in scrubbed:
                scrubbed = scrubbed.replace(secret, self.REPLACEMENT)
        if scrubbed != msg:
            record.msg = scrubbed
            record.args = ()
        return True


def configure_logging(level: str, secrets: Iterable[str] = ()) -> None:
    """Wire a stderr handler at `level`. Idempotent — safe to call
    repeatedly (e.g. from tests)."""
    root = logging.getLogger()
    root.setLevel(level.upper())

    # Replace any existing handlers so re-config takes effect.
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    handler.addFilter(_SecretRedactor(secrets))
    root.addHandler(handler)
