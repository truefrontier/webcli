"""www.linkedin.com's request policy for the core guard. Fail-closed, read-only unless a write scope is open.

POLICY (every read): GET/HEAD over https on linkedin.com, its subdomains, and its CDN (*.licdn.com), never with a
Rest.li ?action= and never to sign-out. Every POST, PUT, PATCH, DELETE, and every other host is aborted, so nothing
a LinkedIn page's own scripts send (mark-as-read, typing, "seen", reactions, telemetry) gets out.

search_policy() is `posts search`'s: the reads, plus the search page's own "next results" request, a POST to
/flagship-web/rsc-action/actions/pagination that only reads (search_page_allowed: exactly the content-search pager
and screen, nothing else in the body). No other command allows it, and it writes nothing.

write_policy(scope) is the policy of `messages send --confirm` and `post create --confirm` alone, and only while
the command's page holds the core guard's send permission. On top of the reads it passes each of the scope's
writes once, bound to what that command means to write:
  - message: one POST createMessage whose conversationUrn, mailboxUrn (the sender), and text are the scope's, to an
    existing conversation (no recipientProfileUrns).
  - post: one POST register (IMAGE_SHARING, the scope's file size and name); one PUT (and its CORS preflight) to
    exactly the upload URL that register returned; one POST normShares whose text is the scope's and whose one image
    is the asset register returned.
No slot opens until fetch.py's author check (author.py) has passed and armed the scope: the guard can't see which
account is signed in. fetch.py then fills in what it learns (the conversation, the upload URL, the asset) before it
sends each write.

Limits: Playwright routes only the first URL of a redirect chain; the writes are sent with redirect: 'error'. The
upload PUT is bound to its URL, not its bytes, and the guard trusts the account the author check saw: both writes run
on robots.txt, where no page script runs, one command at a time on a locked profile.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlsplit

from webcli.core.guard import Guard, Policy, describe, host_matches
from webcli.sites.linkedin.endpoints import (
    ACTION_PARAM,
    APP_HOST,
    CREATE_PATH,
    HOSTS,
    PAGINATION_PATH,
    PAGINATION_QUERY_KEYS,
    REGISTER_PATH,
    SEARCH_PAGER,
    SEARCH_SCREEN,
    SEND_PATH,
    SIDE_EFFECT_PATHS,
)

if TYPE_CHECKING:
    from playwright.async_api import Request


@dataclass
class WriteScope:
    """The writes one confirmed command may send. kind "message" or "post"; text what it says; the rest is filled in
    as fetch.py learns it. used: the slots already spent (each write goes through once)."""

    kind: str
    text: str
    armed: bool = False  # the author check passed: until then no slot opens
    conversation: str | None = None  # message: the thread's conversationUrn
    mailbox: str | None = None  # message: the sender's profile urn (the signed-in author)
    size: int | None = None  # post: the image's bytes and file name, as register declares them
    filename: str | None = None
    upload_url: str | None = None  # post: what register returned
    asset: str | None = None
    used: set[str] = field(default_factory=set)


def _secure(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.port is None and "@" not in parts.netloc


def read_allowed(method: str, url: str) -> bool:
    """GET/HEAD on LinkedIn's hosts, without ?action= and not to sign-out."""
    parts = urlsplit(url)
    host, path = parts.hostname or "", parts.path
    if method.upper() not in ("GET", "HEAD") or not _secure(url):
        return False
    if not any(host_matches(pattern, host) for pattern in HOSTS):
        return False
    if any(k.lower() == ACTION_PARAM for k, _ in parse_qsl(parts.query, keep_blank_values=True)):
        return False
    return not any(path == p or path.startswith(p + "/") for p in SIDE_EFFECT_PATHS)


def _voyager(url: str, path: str, action: str | None) -> bool:
    """`url` is https://www.linkedin.com<path>, with exactly ?action=<action> (or no query when action is None)."""
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    return (_secure(url) and parts.hostname == APP_HOST and parts.path == path
            and query == ([(ACTION_PARAM, action)] if action else []))


def _json(body: str | None) -> dict:
    try:
        data = json.loads(body or "")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _get(data: Any, *keys: str) -> Any:
    for key in keys:
        data = data.get(key) if isinstance(data, dict) else None
    return data


def _slot(method: str, url: str, body: str | None, scope: WriteScope) -> str | None:
    """Which of the scope's writes this request is, or None."""
    method = method.upper()
    if scope.kind == "message":
        data = _json(body)
        ok = (method == "POST" and _voyager(url, SEND_PATH, "createMessage") and scope.conversation is not None
              and _get(data, "message", "conversationUrn") == scope.conversation
              and _get(data, "mailboxUrn") == scope.mailbox and _get(data, "message", "body", "text") == scope.text
              and "recipientProfileUrns" not in data)
        return "message" if ok else None
    if scope.kind != "post":
        return None
    if method == "PUT":
        return "upload" if scope.upload_url is not None and url == scope.upload_url and _secure(url) else None
    if method != "POST":
        return None
    data = _json(body)
    if _voyager(url, REGISTER_PATH, "upload"):
        ok = data == {"mediaUploadType": "IMAGE_SHARING", "fileSize": scope.size, "filename": scope.filename}
        return "register" if ok else None
    if _voyager(url, CREATE_PATH, None):
        media = data.get("media")
        ok = (scope.asset is not None and _get(data, "commentaryV2", "text") == scope.text
              and isinstance(media, list) and len(media) == 1 and _get(media[0], "mediaUrn") == scope.asset)
        return "create" if ok else None
    return None


def write_allowed(method: str, url: str, body: str | None, scope: WriteScope | None) -> bool:
    """Whether this request is one of the scope's writes not yet sent. Allowing it spends that slot. The upload's
    CORS preflight (OPTIONS, no body) to exactly the upload URL passes too, before the upload, without spending it."""
    if scope is None or not scope.armed:
        return False
    if method.upper() == "OPTIONS":
        return scope.kind == "post" and scope.upload_url == url and "upload" not in scope.used and _secure(url)
    slot = _slot(method, url, body, scope)
    if slot is None or slot in scope.used:
        return False
    scope.used.add(slot)
    return True


def request_allowed(method: str, url: str, body: str | None = None, *, scope: WriteScope | None = None) -> bool:
    """The policy (see the module doc). Fails closed."""
    return read_allowed(method, url) or write_allowed(method, url, body, scope)


def _body(request: "Request") -> str | None:
    if request.method.upper() != "POST":
        return None
    try:
        return request.post_data
    except Exception:  # binary: no write slot takes one
        return None


def _describe(request: "Request") -> str:
    """METHOD host/path, plus action=... for a Rest.li action (an action name, never a value)."""
    text = describe(request.method, request.url)
    actions = sorted({v for k, v in parse_qsl(urlsplit(request.url).query) if k.lower() == ACTION_PARAM})
    return text + (f" action={','.join(a if a.isidentifier() else '<redacted>' for a in actions)}" if actions else "")


def write_policy(scope: WriteScope | None) -> Policy:
    """The policy for one command. A scope counts only while the page holds send (state.send): only a confirmed
    `messages send` or `post create` opens its page that way."""

    def allowed(request: "Request", state: Guard) -> bool:
        return request_allowed(request.method, request.url, _body(request), scope=scope if state.send else None)

    return Policy("linkedin", allowed, _describe)


def search_page_allowed(method: str, url: str, body: str | None) -> bool:
    """The content-search page's own pagination: POST https://www.linkedin.com<PAGINATION_PATH> (query keys only
    parentSpanId/sduiid) whose JSON body is exactly {pagerId, clientArguments, paginationRequest} for the
    content-search pager on the search-results screen. A read: it asks for the next results."""
    parts = urlsplit(url)
    if method.upper() != "POST" or not _secure(url) or parts.hostname != APP_HOST or parts.path != PAGINATION_PATH:
        return False
    if not {k for k, _ in parse_qsl(parts.query, keep_blank_values=True)} <= PAGINATION_QUERY_KEYS:
        return False
    data = _json(body)
    return (set(data) == {"pagerId", "clientArguments", "paginationRequest"} and data.get("pagerId") == SEARCH_PAGER
            and _get(data, "paginationRequest", "pagerId") == SEARCH_PAGER
            and _get(data, "clientArguments", "screenId") == SEARCH_SCREEN)


def search_policy(passed: list[str] | None = None) -> Policy:
    """`posts search`: the reads plus search_page_allowed. Never a write: send stays off, and no scope opens.
    `passed` collects a line for each pagination request let through (for the guard log)."""

    def allowed(request: "Request", state: Guard) -> bool:
        if read_allowed(request.method, request.url):
            return True
        ok = search_page_allowed(request.method, request.url, _body(request))
        if ok and passed is not None:
            passed.append(_describe(request))
        return ok

    return Policy("linkedin", allowed, _describe)


POLICY = write_policy(None)
