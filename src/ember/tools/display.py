"""Display tool — push content to the inkframe e-ink picture frame.

One tool wraps the display-manager service's `POST /push`:

- `display_push` — queue a content item (message/alert/news/quote/countdown).

The display renders by priority (alert < message < news < quote <
countdown < weather; lowest wins) and prunes expired items each render
cycle (~5 min), falling back to weather. `ttl_minutes=0` means no
expiry. The service is LAN-internal and unauthenticated.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from ember.errors import ToolError, ToolRetryable
from ember.tools.base import ToolResult

_log = logging.getLogger("ember.tools.display")

_DEFAULT_TIMEOUT = 15.0

# Types whose payload is text-based; countdown uses label + target_date.
_TEXT_TYPES = ("message", "alert", "news", "quote")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class DisplayPushTool:
    name: str = "display_push"
    description: str = (
        "Push content to the family's e-ink picture frame (inkframe). A "
        "'message' preempts the normal weather display until its TTL "
        "expires, after which the frame reverts to weather automatically. "
        "Set ttl_minutes whenever the content is time-boxed (e.g. 120 for "
        "something relevant 7-9am); ttl_minutes=0 means it stays until "
        "explicitly cleared. Reserve 'alert' (top priority, never expires) "
        "for genuinely urgent things. 'news' is a lower-priority ticker "
        "item; 'quote' and 'countdown' are ambient content shown when "
        "nothing more important is queued."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "enum": ["message", "alert", "news", "quote", "countdown"],
                "default": "message",
                "description": "Content type. Priority: alert > message > news > quote > countdown; the frame shows the highest-priority unexpired item.",
            },
            "text": {
                "type": "string",
                "description": "Body text. Required for message/alert/news/quote.",
            },
            "sender": {
                "type": "string",
                "default": "ember",
                "description": "Shown as the message sender (message type only).",
            },
            "ttl_minutes": {
                "type": "integer",
                "minimum": 0,
                "default": 0,
                "description": "Minutes until the item expires and the frame reverts to weather. 0 = no expiry. Always set this when content is time-boxed (e.g. 120 for a 7-9am slot).",
            },
            "author": {
                "type": "string",
                "description": "Quote attribution (quote type only).",
            },
            "label": {
                "type": "string",
                "description": "Countdown label, e.g. 'Days until vacation'. Required for countdown.",
            },
            "target_date": {
                "type": "string",
                "description": "Countdown target date, YYYY-MM-DD. Required for countdown.",
            },
        },
        "additionalProperties": False,
    }
    requires_confirmation: bool = False

    def __init__(self, *, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")

    @staticmethod
    def _build_payload(args: dict[str, Any]) -> dict[str, Any]:
        kind = args.get("type", "message")
        text = args.get("text")

        if kind in _TEXT_TYPES and not text:
            raise ToolError(f"display_push: 'text' is required for type '{kind}'")

        if kind == "message":
            return {
                "type": "message",
                "text": text,
                "sender": args.get("sender", "ember"),
                "ttl_minutes": int(args.get("ttl_minutes", 0)),
            }
        if kind == "alert":
            return {"type": "alert", "text": text}
        if kind == "news":
            return {
                "type": "news",
                "text": text,
                "ttl_minutes": int(args.get("ttl_minutes", 0)),
            }
        if kind == "quote":
            payload: dict[str, Any] = {"type": "quote", "text": text}
            if args.get("author"):
                payload["author"] = args["author"]
            return payload
        # countdown
        label = args.get("label")
        target_date = args.get("target_date")
        if not label or not target_date:
            raise ToolError(
                "display_push: 'label' and 'target_date' are required for type 'countdown'"
            )
        if not _DATE_RE.match(target_date):
            raise ToolError(f"display_push: target_date must be YYYY-MM-DD, got {target_date!r}")
        return {"type": "countdown", "label": label, "target_date": target_date}

    async def call(self, args: dict[str, Any]) -> ToolResult:
        payload = self._build_payload(args)
        url = f"{self._base_url}/push"
        try:
            async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
                resp = await client.request("POST", url, json=payload)
        except httpx.ConnectError as e:
            raise ToolRetryable(f"display-manager unreachable: {e}") from e
        except httpx.HTTPError as e:
            raise ToolError(f"display-manager HTTP error: {e}") from e

        if resp.status_code >= 500:
            raise ToolError(f"display_push: server error (HTTP {resp.status_code})")
        if resp.status_code >= 400:
            raise ToolError(f"display_push: HTTP {resp.status_code}: {resp.text[:200]}")

        return ToolResult(output=resp.json())
