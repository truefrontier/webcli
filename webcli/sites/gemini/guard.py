"""gemini.google.com's request policy for the core guard: endpoints.py's tables, verbatim from gweb's browser.py."""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlsplit

from webcli.core.guard import Guard, Policy
from webcli.core.redact import REDACTED, redact_url, tokenish
from webcli.sites.gemini.endpoints import (
    APP_HOST,
    BATCH_PATH,
    DOWNLOAD_RPCIDS,
    IMAGE_HOSTS,
    READ_RPCIDS,
    RPC_PREFIX,
    SEND_REQUESTS,
    SESSION_POSTS,
    SIDE_EFFECT_GETS,
)
from webcli.sites.gemini.parse import freq_rpcids

if TYPE_CHECKING:
    from playwright.async_api import Request


def rpcids_of(url: str, body: str | None) -> list[str] | None:
    """The rpcids a batchexecute POST runs, or None when the URL and the f.req body disagree.

    The server runs what f.req says; the URL's `rpcids=` is only a label. Both are checked so
    neither can smuggle in an id the other leaves out.
    """
    labels = [v for k, v in parse_qsl(urlsplit(url).query, keep_blank_values=True) if k == "rpcids"]
    in_url = [r for r in (labels[0] if len(labels) == 1 else "").split(",") if r]
    in_body = freq_rpcids(body)
    if not in_url or sorted(set(in_url)) != sorted(set(in_body)):
        return None
    return in_body


def describe(method: str, url: str, body: str | None = None) -> str:
    """METHOD host/path [rpcids=...] for error messages. No query string (it carries the session id),
    and token-looking path segments or ids are redacted."""
    parts = urlsplit(url)
    text = f"{method} {parts.hostname}{redact_url(parts.path)}"
    if parts.path == BATCH_PATH:
        ids = sorted({REDACTED if tokenish(r) else r for r in freq_rpcids(body)})
        text += f" rpcids={','.join(ids)}"
    return text


def request_allowed(method: str, url: str, body: str | None = None, *, send: bool = False,
                    sign_in: bool = False, download: bool = False) -> bool:
    """The policy. Fails closed: anything not listed in endpoints.py is aborted.

    - SEND_REQUESTS (the prompt's WebChannel processSession, StreamGenerate, the file upload):
      only with send=True (only `ask` / `-p`), over https, with a listed method. Checked first, so
      no other rule (sign-in, GET) lets anything else reach them.
    - sign_in=True (only `identity login google:*`): any request to a host other than gemini.google.com, so
      a person can sign in to Google. Gemini itself stays under the rules below.
    - GET/HEAD anywhere, minus SIDE_EFFECT_GETS (sign-out) and Gemini's RPC paths.
    - POST batchexecute on gemini.google.com only when every rpcid is in READ_RPCIDS (or, with
      download=True, only `ask --image-out`, in DOWNLOAD_RPCIDS).
    - POST RotateCookies (keeps the session cookies fresh; no user data).
    Everything else is aborted: delete/rename/pin/share RPCs, settings, Gems, telemetry.

    Limits: Playwright routes only the first URL of a redirect chain, so a redirect Google's
    server chooses for an allowed request is followed unchecked (nothing here follows links, and
    a 307/308 hop carries the same read-only f.req). Checking a batchexecute body means holding
    it, `at` token included, in memory for this call; it is never stored, logged, or printed.
    """
    method = method.upper()
    parts = urlsplit(url)
    host, path = parts.hostname, parts.path
    send_only = [methods for methods, h, root in SEND_REQUESTS
                 if host == h and (path == root or path.startswith(root.rstrip("/") + "/"))]
    if send_only:
        return send and parts.scheme == "https" and any(method in methods for methods in send_only)
    if sign_in and host != APP_HOST:
        return True
    if method in ("GET", "HEAD"):
        if host == APP_HOST and path.startswith(RPC_PREFIX):
            return False  # RPCs are POSTs; a GET there is nothing the app needs
        return not any(host == h and path.startswith(prefix) for h, prefix in SIDE_EFFECT_GETS)
    if parts.scheme != "https":
        return False
    if method != "POST":
        return False
    if (host, path) in SESSION_POSTS:
        return True
    if host != APP_HOST or path != BATCH_PATH:
        return False
    rpcids = rpcids_of(url, body)
    allowed = READ_RPCIDS | DOWNLOAD_RPCIDS if download else READ_RPCIDS
    return bool(rpcids) and all(r in allowed for r in rpcids)


def image_url_allowed(url: str) -> bool:
    """Whether a generated image may be fetched from this URL with page.request: https on a listed image host
    (endpoints.IMAGE_HOSTS), default port, no credentials in the URL. page.request bypasses the route guard, so
    the download code calls this before every hop (and never lets page.request follow a redirect itself)."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    return (parts.scheme == "https" and port is None and "@" not in parts.netloc
            and bool(IMAGE_HOSTS.fullmatch(parts.hostname or "")))


def _body(request: "Request") -> str | None:
    # Only batchexecute bodies are read: upload bodies are binary and post_data would raise.
    return request.post_data if urlsplit(request.url).path == BATCH_PATH else None


def _allowed(request: "Request", state: Guard) -> bool:
    return request_allowed(request.method, request.url, _body(request), send=state.send, sign_in=state.sign_in,
                           download=state.download)


def _describe(request: "Request") -> str:
    try:
        body = _body(request)
    except Exception:
        body = None
    return describe(request.method, request.url, body)


POLICY = Policy("gemini", _allowed, _describe)
