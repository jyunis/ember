"""Vault tools (FC-9, TLC-4, UoW-13).

Wraps the memory-MCP secrets broker over HTTP with a bearer token.
Only path in the agent that touches the broker. Two tools:
- `vault_list` — list available credential names.
- `vault_get` — fetch a single credential, redacted by default. Revealing
  one in cleartext (`show_full=true`) is confirmation-gated.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ember.errors import ToolError, ToolRetryable
from ember.tools.base import ToolResult

_log = logging.getLogger("ember.tools.vault")

# The broker shells out to the Bitwarden CLI: ~1s of node startup per
# command, and a cache miss triggers `bw sync` + a retry (10s+ observed
# 2026-09-27). 10s was cutting that off and surfacing an empty error.
_BROKER_TIMEOUT = 30.0


def _request_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


class VaultListTool:
    name: str = "vault_list"
    description: str = (
        "List all credential names available in the user's vault. "
        "Returns names only, never values. Use vault_get to fetch a specific value."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    requires_confirmation: bool = False

    def __init__(self, *, broker_url: str, token: str) -> None:
        self._broker_url = broker_url.rstrip("/")
        self._token = token

    async def call(self, args: dict[str, Any]) -> ToolResult:
        try:
            async with httpx.AsyncClient(timeout=_BROKER_TIMEOUT) as client:
                resp = await client.get(
                    f"{self._broker_url}/secrets",
                    headers=_request_headers(self._token),
                )
        except httpx.ConnectError as e:
            raise ToolRetryable(f"vault broker unreachable: {e}") from e
        except httpx.HTTPError as e:
            raise ToolError(f"vault broker error: {e}") from e

        if resp.status_code in (401, 403):
            raise ToolError("vault: forbidden")
        if resp.status_code >= 400:
            raise ToolError(f"vault list failed: HTTP {resp.status_code}")
        return ToolResult(output=resp.json())


class VaultGetTool:
    name: str = "vault_get"
    description: str = (
        "Fetch a credential field from the user's vault. By default the "
        "value is elided as `****<last4>`; set `show_full=true` ONLY when "
        "the user explicitly asks to see the full credential. `item` must be "
        "an exact name from vault_list — call it first if unsure. `field` is "
        "`password`, `username`, `notes`, or the name of a custom field on the "
        "item; an error for an empty field lists the fields that do have a "
        "value. A show_full read needs this conversation unlocked (one "
        "authenticator code opens a window; the host prompts for it and then "
        "asks you to continue). Use the value in your work — NEVER repeat a "
        "secret in chat unless the user explicitly asked to see it."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "item": {
                "type": "string",
                "description": "Credential name (case-insensitive substring match).",
            },
            "field": {
                "type": "string",
                "default": "password",
                "description": (
                    "password (default), username, or a custom field name on "
                    "the item (case-insensitive)."
                ),
            },
            "show_full": {
                "type": "boolean",
                "default": False,
                "description": "If true, return the full value; otherwise elide.",
            },
        },
        "required": ["item"],
        "additionalProperties": False,
    }
    # Conditional gate (TLC-3), same shape as `gmail_send`: the elided read
    # is the everyday path and stays frictionless, while returning a
    # credential in cleartext requires human approval — and, when
    # `vault_get` is listed in CONFIRM_TOTP_TOOLS, a code from a second
    # device. Without this, a single tool call silently exfiltrates every
    # secret in the vault.
    requires_confirmation: bool = True

    def __init__(self, *, broker_url: str, token: str) -> None:
        self._broker_url = broker_url.rstrip("/")
        self._token = token

    def needs_confirmation(self, args: dict[str, Any]) -> bool:
        """Gate only cleartext reads. A redacted `****<last4>` result
        discloses nothing worth a prompt."""
        return bool(args.get("show_full", False))

    def summarize_action(self, args: dict[str, Any]) -> str:
        """The 🔒 preview. Names the credential being revealed, never any
        part of its value."""
        item = args.get("item") or "(unnamed)"
        field = args.get("field", "password")
        return f"vault_get: reveal {field} for {item!r} IN CLEARTEXT"

    async def preflight(self, args: dict[str, Any]) -> None:
        """Registry hook, run before the 🔒 prompt for a cleartext reveal:
        do the redacted read now so a wrong item name or empty field
        fails back to the model immediately (with the broker's
        available-fields hint) instead of after the user's TOTP code."""
        await self._fetch(args["item"], args.get("field", "password"))

    async def call(self, args: dict[str, Any]) -> ToolResult:
        item = args["item"]
        field = args.get("field", "password")
        show_full = bool(args.get("show_full", False))
        value = await self._fetch(item, field)

        # `message` is what the confirmation-redeem path shows the user
        # (see Agent._format_tool_confirmation_reply): without it a
        # cleartext reveal came back as a bare "vault_get done." and the
        # value the user just authenticated for was never displayed.
        out: dict[str, Any] = {"item": item, "field": field}
        if show_full:
            # No `message` on the cleartext path: the value is for the
            # model to *use*; nothing here should render it to the screen.
            return ToolResult(output={**out, "value": value, "redacted": False})
        elided = self._elide(value)
        return ToolResult(
            output={
                **out,
                "value": elided,
                "redacted": True,
                "message": f"{field} for {item!r}: {elided} (redacted)",
            }
        )

    async def _fetch(self, item: str, field: str) -> str:
        """GET the field from the broker; raise ToolError with the
        broker's own detail on any failure. Returns the cleartext value —
        callers decide whether to elide."""
        try:
            async with httpx.AsyncClient(timeout=_BROKER_TIMEOUT) as client:
                resp = await client.get(
                    f"{self._broker_url}/secrets/{item}",
                    params={"field": field},
                    headers=_request_headers(self._token),
                )
        except httpx.ConnectError as e:
            raise ToolRetryable(f"vault broker unreachable: {e}") from e
        except httpx.TimeoutException as e:
            raise ToolError(
                f"vault broker timed out after {_BROKER_TIMEOUT:.0f}s for {item}"
            ) from e
        except httpx.HTTPError as e:
            # str(e) is empty for several httpx errors; name the type.
            raise ToolError(f"vault broker error: {type(e).__name__}: {e}") from e

        if resp.status_code == 404:
            # The broker distinguishes "no such item" from "item exists but
            # this field is empty" — surface that, or the agent (and the
            # human reading its error) will hunt for an item that is there.
            detail = self._broker_detail(resp)
            if detail:
                raise ToolError(f"vault: not found: {item} ({detail})")
            raise ToolError(f"vault: not found: {item}")
        if resp.status_code in (401, 403):
            raise ToolError(f"vault: forbidden for {item}")
        if resp.status_code >= 400:
            detail = self._broker_detail(resp)
            suffix = f": {detail}" if detail else ""
            raise ToolError(f"vault get failed: HTTP {resp.status_code}{suffix}")

        # The memory-MCP broker answers `{"secret": <str>}`; accept the
        # older `{"value": <str>}` shape too. Anything else is refused
        # outright: passing an unrecognised body through would return a
        # cleartext credential without the show_full gate (TLC-4).
        data = resp.json()
        value: Any = None
        if isinstance(data, dict):
            value = data.get("secret")
            if value is None:
                value = data.get("value")
        if not isinstance(value, str):
            raise ToolError(f"vault: unexpected broker response for {item}")
        return value

    @staticmethod
    def _broker_detail(resp: httpx.Response) -> str | None:
        """The broker's `{"error": "..."}` message, if the body is one."""
        try:
            body = resp.json()
        except ValueError:
            return None
        if isinstance(body, dict) and isinstance(body.get("error"), str):
            return body["error"]
        return None

    @staticmethod
    def _elide(value: str) -> str:
        """Return `****<last4>` for values >= 8 chars; `****` otherwise."""
        if not isinstance(value, str):
            return "****"
        if len(value) >= 8:
            return f"****{value[-4:]}"
        return "****"
