"""muse.ai's request policy for the core guard: endpoints.py's tables, verbatim from musecli's browser.py."""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from webcli.core.guard import Guard, Policy, describe
from webcli.sites.muse.endpoints import READ_POSTS, SEND_POSTS, SESSION_POSTS, SIDE_EFFECT_GETS, SOCKET_HOST_SUFFIXES
from webcli.sites.muse.parse import post_op

if TYPE_CHECKING:
    from playwright.async_api import Request


def request_allowed(method: str, url: str, headers: dict[str, str] | None = None, body: str | None = None,
                    *, send: bool = False) -> bool:
    """The HTTP policy. Fails closed: anything not listed in endpoints.py is aborted.

    - GET/HEAD anywhere, minus SIDE_EFFECT_GETS (sign-out). musecli's rule, verified live on 2026-10-15: the
      sign-in check cycles through auth.muse.ai, facebook.com, and instagram.com, and Muse's CDN hosts are
      unmapped. A GET writes nothing here; every write is a POST or goes through the socket.
    - POST only when (host, path, op) is in SESSION_POSTS or READ_POSTS, or SEND_POSTS when send=True (only `ask`
      / `-p`). op comes from parse.post_op; a body whose labels disagree is aborted. Telemetry, sharing, settings,
      deletes, and every unknown POST are aborted.
    - PUT/PATCH/DELETE/OPTIONS and plain-http POSTs: never.

    Playwright routes only the first URL of a redirect chain. A redirect keeps GET/HEAD, and 307/308 keep a POST's
    method and body, so a hop can't turn an allowed call into another one.
    """
    method = method.upper()
    parts = urlsplit(url)
    host, path = parts.hostname or "", parts.path
    if method in ("GET", "HEAD"):
        return not any(host == h and path.startswith(prefix) for h, prefix in SIDE_EFFECT_GETS)
    if method != "POST" or parts.scheme != "https":
        return False
    try:
        op = post_op(headers or {}, body)
    except ValueError:
        return False
    allowed = SESSION_POSTS | READ_POSTS | (SEND_POSTS if send else set())
    return (host, path, op) in allowed


def socket_allowed(url: str) -> bool:
    """The WebSocket policy: only the Muse VM gateway (wss://*.metaaivm.com).

    That socket is end-to-end encrypted (Noise) and carries reads and writes alike, so the guard cannot filter
    inside it (send=False doesn't reach it). llmcli muse keeps it read-only by never clicking or typing anything but
    what each command documents.
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    return parts.scheme == "wss" and any(host.endswith(suffix) for suffix in SOCKET_HOST_SUFFIXES)


def _op(request: "Request") -> str | None:
    """The POST's operation for the policy. request.headers is sync and keeps the app's own x-fb-friendly-name and
    next-action (it leaves out only cookie-like headers)."""
    return post_op(request.headers, request.post_data) if request.method == "POST" else None


def _allowed(request: "Request", state: Guard) -> bool:
    post = request.method == "POST"
    return request_allowed(request.method, request.url, request.headers if post else None,
                           request.post_data if post else None, send=state.send)


def _describe(request: "Request") -> str:
    """'POST muse.ai/api/x op=Name': host and path, token-looking segments redacted, and the operation."""
    try:
        op = _op(request)
    except Exception:
        op = "<labels disagree>"
    return describe(request.method, request.url) + (f" op={op}" if op else "")


POLICY = Policy("muse", _allowed, _describe, sockets=socket_allowed)
