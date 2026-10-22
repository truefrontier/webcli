"""web.whatsapp.com's request policy for the core guard. Fail-closed.

WhatsApp Web carries everything (the chat list, messages, read receipts, sends) inside one Noise-encrypted
WebSocket, so this guard can't tell a read from a write the way the Google Messages guard can (endpoints.py). What it
does:
  - GET/HEAD over https to web.whatsapp.com and static.whatsapp.net (the app and its static files), from the
    command's own page only.
  - The one WebSocket wss://web.whatsapp.com/ws/..., from the command's own page only, closed when the command ends.
  - Everything else is aborted: every POST/PUT/PATCH/DELETE (crash logs, telemetry), media downloads
    (mmg.whatsapp.net: `read` shows [image] instead), profile pictures, and every other host.
The guarded Chrome also blocks service workers, so every request the page makes passes through here.

Reads stay reads because commands type and click nothing that writes: `chats` scrolls the list, `read` clicks the
chat's row and scrolls up, `search` types into the search box (searched on this computer). Only a confirmed `send`
types into a chat's compose box and clicks Send, after the checks in fetch.send.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from webcli.core.guard import Guard, Policy, describe, host_matches
from webcli.sites.whatsapp.endpoints import HOSTS, SOCKET_HOST, SOCKET_PATH_PREFIX

if TYPE_CHECKING:
    from playwright.async_api import Request


def _clean(url: str, scheme: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == scheme and parts.port is None and "@" not in parts.netloc


def request_allowed(method: str, url: str) -> bool:
    """See the module doc. Fails closed."""
    if method.upper() not in ("GET", "HEAD") or not _clean(url, "https"):
        return False
    host = urlsplit(url).hostname or ""
    return any(host_matches(pattern, host) for pattern in HOSTS)


def socket_allowed(url: str) -> bool:
    """Only wss://web.whatsapp.com/ws..."""
    parts = urlsplit(url)
    return (_clean(url, "wss") and parts.hostname == SOCKET_HOST
            and (parts.path == SOCKET_PATH_PREFIX or parts.path.startswith(SOCKET_PATH_PREFIX + "/")))


def _allowed(request: "Request", _state: Guard) -> bool:
    return request_allowed(request.method, request.url)


def _describe(request: "Request") -> str:
    return describe(request.method, request.url)


POLICY = Policy("whatsapp", _allowed, _describe, sockets=socket_allowed)
