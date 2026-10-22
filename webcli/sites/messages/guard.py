"""messages.google.com's request policy for the core guard. Fail-closed, read-only unless a send scope is armed.

Every phone action goes through one relay call, Messaging/SendMessage, whose action type is in the clear and whose
request is end-to-end encrypted (endpoints.py). So this guard sorts phone actions by type; it never sees which
conversation or what text.

POLICY (every read):
  - GET/HEAD over https on messages.google.com, Google's static hosts, and fonts.googleapis.com.
  - The relay (RELAY_READS): the long poll the phone's answers arrive on, its acks, the session's token refresh; the
    app's sign-in and connection check (SESSION_RPCS, on clients6 only); and Messaging/SendMessage only when its
    body reads as exactly one DataEvent action in READ_ACTIONS (listing, thumbnails, the session's presence and
    active-session calls). CORS preflights (OPTIONS) to those paths.
  - Everything else is aborted: every other action (MESSAGE_READ, so reading never marks a conversation read;
    TYPING_UPDATES, SEND_MESSAGE, RESEND_MESSAGE, SEND_REACTION, SEND_REPLY, DELETE_MESSAGE, UPDATE_CONVERSATION,
    GET_OR_CREATE_CONVERSATION, settings, group edits), pairing and unpairing, attachment uploads, a body that doesn't
    read as exactly one action (a field given twice counts as unreadable), telemetry, and every other host.

Downloads (`read --image-out` alone): while the page holds the core guard's download permission, also the GET (only
GET, https, no query) to MEDIA_HOST + MEDIA_PATH, the app's encrypted-attachment download. POSTs there (uploads) stay
blocked with or without it, and download adds nothing else.

send_policy(scope) is `send --confirm`'s alone, and counts only while the command's page holds the core guard's send
permission. On top of the reads it passes ONE SEND_MESSAGE, and only while the scope is armed. fetch.send arms it
after checking in the page that the open conversation is the resolved one, that the compose box holds exactly the
text, and that the page has tried no send of its own. It then clicks send. The slot closes after that one send or
SEND_SECONDS, whichever comes first.

Limits: the guard holds the send to one SEND_MESSAGE, opened only for the seconds after the click. It can't check the
conversation or the text, which are encrypted: fetch.send checks those in the page before the slot opens, and checks
the thread for the sent text afterwards. Playwright routes only the first URL of a redirect chain.
"""

from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from webcli.core.guard import Guard, Policy, describe, host_matches
from webcli.sites.messages.endpoints import (
    ACTIONS,
    DATA_EVENT,
    HOSTS,
    MEDIA_HOST,
    MEDIA_PATH,
    READ_ACTIONS,
    RELAY_HOSTS,
    RELAY_READS,
    RPC_NAMES,
    RPC_PREFIX,
    SEND_MESSAGE,
    SEND_RPC,
    SESSION_HOST,
    SESSION_RPCS,
)

if TYPE_CHECKING:
    from playwright.async_api import Request

_B64 = re.compile(r"[A-Za-z0-9+/_-]*={0,2}")


@dataclass
class SendScope:
    """The one send a confirmed `send` may let out. armed_until: a time.monotonic() deadline while armed, else None.
    sent: the one SEND_MESSAGE went out."""

    armed_until: float | None = None
    sent: bool = False

    def arm(self, seconds: float) -> None:
        self.armed_until = time.monotonic() + seconds

    def disarm(self) -> None:
        self.armed_until = None


def _secure(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.port is None and "@" not in parts.netloc


def rpc(url: str) -> str | None:
    """"Service/Method" (Messaging/SendMessage) for an https relay RPC URL, else None."""
    parts = urlsplit(url)
    if not _secure(url) or parts.hostname not in RELAY_HOSTS or not parts.path.startswith(RPC_PREFIX):
        return None
    return parts.path[len(RPC_PREFIX):]


# --- Reading the action out of a Messaging/SendMessage body --------------------------------------------------------

def _varint(buf: bytes, i: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        if i >= len(buf) or shift > 63:
            raise ValueError("truncated varint")
        byte = buf[i]
        i += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, i
        shift += 7


def _fields(buf: bytes) -> dict[int, list]:
    """A protobuf message's fields: number -> its values in order (int for varints, bytes for length-delimited)."""
    out: dict[int, list] = {}
    i = 0
    while i < len(buf):
        key, i = _varint(buf, i)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _varint(buf, i)
        elif wire == 2:
            size, i = _varint(buf, i)
            if i + size > len(buf):
                raise ValueError("truncated field")
            value, i = buf[i:i + size], i + size
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
            if i + size > len(buf):
                raise ValueError("truncated field")
            value, i = buf[i:i + size], i + size
        else:
            raise ValueError(f"wire type {wire}")
        if number == 0:
            raise ValueError("field 0")
        out.setdefault(number, []).append(value)
    return out


def _one(fields: dict[int, list], number: int):
    """The single value of a singular field; ValueError when it is given twice (protobuf would take the last)."""
    values = fields.get(number, [])
    if len(values) > 1:
        raise ValueError(f"field {number} given twice")
    return values[0] if values else None


def _pblite(msg, number: int):
    """Field `number` of a PBLite message (a JSON array; a trailing object holds sparse fields). ValueError when it
    is given both ways."""
    if not isinstance(msg, list):
        raise ValueError("not a PBLite message")
    tail = msg[-1] if msg and isinstance(msg[-1], dict) else None
    dense = msg[:-1] if tail is not None else msg
    value = dense[number - 1] if number <= len(dense) else None
    extra = tail.get(str(number)) if tail else None
    if value is not None and extra is not None:
        raise ValueError(f"field {number} given twice")
    return value if value is not None else extra


def _b64(value) -> bytes:
    if not isinstance(value, str) or not _B64.fullmatch(value):
        raise ValueError("not base64")
    value = value.replace("-", "+").replace("_", "/")
    return base64.b64decode(value + "=" * (-len(value) % 4), validate=True)


def _unique_keys(pairs: list) -> dict:
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key")
    return dict(pairs)


def outgoing_action(body: bytes | None) -> int | None:
    """The action of one Messaging/SendMessage body: OutgoingRPCMessage.data (2) must take the DataEvent route (2) and
    carry messageData (12), an OutgoingRPCData holding exactly one action (2). PBLite JSON, or binary protobuf. None
    for anything else: no body, another route, no action, a field given twice, or anything that doesn't parse."""
    try:
        if not body:
            return None
        if body.lstrip()[:1] == b"[":
            data = _pblite(json.loads(body, object_pairs_hook=_unique_keys), 2)
            route, raw = _pblite(data, 2), _b64(_pblite(data, 12))
        else:
            data = _fields(_one(_fields(body), 2))
            route, raw = _one(data, 2), _one(data, 12)
        actions = _fields(raw).get(2, [])
        if type(route) is not int or route != DATA_EVENT or len(actions) != 1 or type(actions[0]) is not int:
            return None
        return actions[0]
    except (ValueError, TypeError, IndexError, AttributeError, RecursionError):
        return None


# --- The policy ---------------------------------------------------------------------------------------------------

def read_allowed(method: str, url: str, body: bytes | None = None) -> bool:
    """See the module doc. Fails closed."""
    method = method.upper()
    if not _secure(url):
        return False
    if method in ("GET", "HEAD"):
        return any(host_matches(pattern, urlsplit(url).hostname or "") for pattern in HOSTS)
    name = rpc(url)
    session = name in SESSION_RPCS and urlsplit(url).hostname == SESSION_HOST
    if method == "OPTIONS":
        return name in RELAY_READS or name == SEND_RPC or session
    if method != "POST":
        return False
    return name in RELAY_READS or session or (name == SEND_RPC and outgoing_action(body) in READ_ACTIONS)


def send_allowed(method: str, url: str, body: bytes | None, scope: SendScope | None) -> bool:
    """The scope's one SEND_MESSAGE, while it is armed. Allowing it spends the scope and disarms it."""
    if scope is None or scope.sent or scope.armed_until is None or time.monotonic() > scope.armed_until:
        return False
    if method.upper() != "POST" or rpc(url) != SEND_RPC or outgoing_action(body) != SEND_MESSAGE:
        return False
    scope.sent, scope.armed_until = True, None
    return True


def media_allowed(method: str, url: str) -> bool:
    """The app's attachment download: a GET to exactly https://MEDIA_HOST/MEDIA_PATH, with no query."""
    parts = urlsplit(url)
    return (method.upper() == "GET" and _secure(url) and parts.hostname == MEDIA_HOST and parts.path == MEDIA_PATH
            and not parts.query)


def request_allowed(method: str, url: str, body: bytes | None = None, *, scope: SendScope | None = None,
                    download: bool = False) -> bool:
    return (read_allowed(method, url, body) or (download and media_allowed(method, url))
            or send_allowed(method, url, body, scope))


def _body(request: "Request") -> bytes | None:
    if request.method.upper() != "POST":
        return None
    try:
        return request.post_data_buffer
    except Exception:
        return None


def _describe(request: "Request") -> str:
    """METHOD host/path; for a relay call, METHOD host Service/Method, plus the action's name for SendMessage (a
    name from the enum, never a value)."""
    name = rpc(request.url)
    if name not in RPC_NAMES:
        return describe(request.method, request.url)
    text = f"{request.method.upper()} {urlsplit(request.url).hostname} {name}"
    if name == SEND_RPC and request.method.upper() == "POST":
        action = outgoing_action(_body(request))
        text += f" action={ACTIONS.get(action, '<unreadable>' if action is None else f'#{action}')}"
    return text


def send_policy(scope: SendScope | None) -> Policy:
    """The policy for one command. A scope counts only while the page holds send (state.send): only a confirmed
    `send` opens its page that way. The media download counts only while it holds download (state.download): only
    `read --image-out` opens its page that way."""

    def allowed(request: "Request", state: Guard) -> bool:
        return request_allowed(request.method, request.url, _body(request), scope=scope if state.send else None,
                               download=state.download)

    return Policy("messages", allowed, _describe)


POLICY = send_policy(None)
