"""The chromecli wire protocol (v1): op names, request validation, the error-code -> exit-code table, and Chrome's
native-messaging framing (uint32 little-endian length + UTF-8 JSON).

CLI <-> host: one JSON line each way per connection, then close (like core/daemon).
Host <-> extension: native framing; host -> extension at most 1 MiB per message (Chrome's limit), extension -> host
at most 64 MiB.

Request (CLI -> host):   {"v": 1, "op": "tabs.read", "args": {...}, "client": {"version", "pid", "timeoutMs"}}
Host -> extension:       {"v": 1, "id": "<hex>", "op", "args", "confirm": false}
Reply:                   {"v": 1, "id", "ok": true, "result": {...}}
                         {"v": 1, "id", "ok": false, "error": {"code", "message", "hint"?, ...}}
Events (extension -> host): {"event": "hello"|"share_changed"|"paused_changed"|"policy_ack", ...}
Events (host -> extension): {"event": "policy", "policy": {...}}
"""

from __future__ import annotations

import json
import struct
from typing import Any, BinaryIO

VERSION = 1
HOST_TO_EXT_MAX = 1 * 2**20
EXT_TO_HOST_MAX = 64 * 2**20
CLI_REQUEST_MAX = 1 * 2**20

READ_OPS = ("status", "tabs.list", "tabs.show", "tabs.read", "tabs.screenshot", "history.search", "page.wait")
TAB_OPS = ("tab.open", "tab.focus", "tab.close", "tab.navigate")
PAGE_OPS = ("page.click", "page.type", "page.fill", "page.select", "page.press", "page.eval")
OPS = READ_OPS + TAB_OPS + PAGE_OPS
# Ops that change a tab or page: sent only with --confirm (confirm: true); the extension refuses them otherwise.
MUTATING_OPS = TAB_OPS + PAGE_OPS
# P4 (Jev) adds these; until then they are bad_request.
FUTURE_OPS = ("page.find", "page.elements")
EVENTS_FROM_EXT = ("hello", "share_changed", "paused_changed", "policy_ack", "error")

# Spec section 4: one table, every error code the host or extension can answer with.
ERROR_EXIT: dict[str, int] = {
    # 2: the request itself is wrong
    "bad_request": 2, "too_large": 2, "bad_target": 2, "missing_expect": 2, "confirm_conflict": 2,
    # 3: not found
    "tab_not_found": 3, "element_not_found": 3, "ambiguous_target": 3, "no_history": 3,
    # 4: a person must act
    "not_shared": 4, "not_act_shared": 4, "origin_blocked": 4, "paused": 4, "declined": 4, "confirm_timeout": 4,
    "not_visible": 4, "permission_needed": 4, "policy_pending": 4,
    # 5: bridge / page
    "bridge_down": 5, "host_busy": 5, "timeout": 5, "url_mismatch": 5, "script_error": 5, "jev_error": 5,
    # 7: config
    "config_error": 7, "host_not_installed": 7, "jev_not_configured": 7, "history_disabled": 7, "eval_disabled": 7,
    # 8: dispatched, not verified
    "sent_unverified": 8,
}
ERROR_HINTS: dict[str, str] = {
    "not_shared": "Kevin: click the chromecli icon on that tab and choose Share-read.",
    "not_act_shared": "Kevin: click the chromecli icon on that tab and choose Share-act.",
    "paused": "Agents are paused in the chromecli popup; Kevin un-pauses there.",
    "origin_blocked": "That origin is on the deny list; it can't be shared.",
    "policy_pending": "A [chromecli] config change widens access; Kevin accepts or rejects it in the chromecli popup.",
    "not_visible": "Screenshots need the tab to be the active tab of a focused window; Kevin brings it to the front.",
    "permission_needed": "Kevin: click the chromecli icon on that tab (re-share it) so Chrome grants access, then retry.",
    "bridge_down": "Chrome isn't running, or the chromecli extension isn't loaded/connected. See `chromecli status`.",
    "host_busy": "Another chromecli-host is already serving (two Chrome profiles with the extension?).",
    "history_disabled": "history = false in [chromecli] of ~/.webcli/config.toml.",
    "config_error": "Fix [chromecli] in ~/.webcli/config.toml.",
    "declined": "Kevin clicked Deny in the browser. Don't retry the same action without asking him.",
    "confirm_timeout": "Nobody answered the in-browser confirm in time. Ask Kevin before retrying.",
    "missing_expect": "Add --expect-origin ORIGIN or --expect-url PREFIX (the page you think the tab is on).",
    "url_mismatch": "The tab isn't where --expect-* says. Re-check it with `chromecli show TAB` before acting.",
    "sent_unverified": "The action went out but its check failed. Don't repeat it; read the page first.",
    "eval_disabled": "Chrome's MV3 isolated-world CSP forbids running code strings; use click/type/fill/select/press.",
    "ambiguous_target": "Narrow the target: a more specific --selector, a --ref from `read --elements`, or --role.",
    "element_not_found": "Refs die on navigation; run `chromecli read TAB --elements` again.",
}


class ProtocolError(Exception):
    """A request or frame that breaks the protocol; `code` is an ERROR_EXIT key."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def exit_code(code: str | None) -> int:
    """Exit code for an error code; an unknown code is a bridge/site error (5)."""
    return ERROR_EXIT.get(code or "", 5)


def error(code: str, message: str, **extra: Any) -> dict:
    out = {"code": code, "message": message, **extra}
    if code in ERROR_HINTS and "hint" not in out:
        out["hint"] = ERROR_HINTS[code]
    return out


def reply_error(req_id: str | None, code: str, message: str, **extra: Any) -> dict:
    return {"v": VERSION, "id": req_id, "ok": False, "error": error(code, message, **extra)}


def validate_request(req: Any) -> dict:
    """A CLI request, checked: {"v": 1, "op": one of OPS, "args": dict, ...}. Raises ProtocolError(bad_request)."""
    if not isinstance(req, dict):
        raise ProtocolError("bad_request", "A request is a JSON object.")
    if req.get("v") != VERSION:
        raise ProtocolError("bad_request", f"Protocol version {req.get('v')!r} isn't {VERSION}.")
    op = req.get("op")
    if op not in OPS:
        later = " (not in this version)" if op in FUTURE_OPS else ""
        raise ProtocolError("bad_request", f"Unknown op {op!r}{later}.")
    args = req.get("args", {})
    if not isinstance(args, dict):
        raise ProtocolError("bad_request", "args must be an object.")
    if "confirm" in req and not isinstance(req["confirm"], bool):
        raise ProtocolError("bad_request", "confirm must be true or false.")
    return {"v": VERSION, "op": op, "args": args, "confirm": bool(req.get("confirm", False)),
            "client": req.get("client") if isinstance(req.get("client"), dict) else {}}


def encode_frame(message: Any, limit: int = HOST_TO_EXT_MAX) -> bytes:
    """One native-messaging frame. Raises ProtocolError(too_large) past `limit` bytes of JSON."""
    body = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(body) > limit:
        raise ProtocolError("too_large", f"Message is {len(body)} bytes; the limit is {limit}.")
    return struct.pack("<I", len(body)) + body


def _read_exact(stream: BinaryIO, n: int) -> bytes | None:
    chunks, got = [], 0
    while got < n:
        chunk = stream.read(n - got)
        if not chunk:
            return None
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def read_frame(stream: BinaryIO, limit: int = EXT_TO_HOST_MAX) -> Any:
    """The next frame's JSON, or None at EOF. Raises ProtocolError(too_large / bad_request)."""
    header = _read_exact(stream, 4)
    if header is None:
        return None
    (length,) = struct.unpack("<I", header)
    if length > limit:
        raise ProtocolError("too_large", f"Frame of {length} bytes is over the {limit}-byte limit.")
    body = _read_exact(stream, length)
    if body is None:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("bad_request", f"Frame is not UTF-8 JSON: {exc}") from None
