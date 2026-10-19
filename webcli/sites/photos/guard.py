"""photos.google.com's request policy for the core guard: endpoints.py's tables."""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from webcli.core.guard import Guard, Policy, describe
from webcli.core.redact import REDACTED, tokenish
from webcli.sites import google
from webcli.sites.gemini.guard import rpcids_of  # the same batchexecute check: URL labels and f.req must agree
from webcli.sites.photos.endpoints import APP_HOST, BATCH_PATH, DOWNLOAD_HOSTS, HOSTS, READ_RPCIDS

if TYPE_CHECKING:
    from playwright.async_api import Request


def is_batch(url: str) -> bool:
    parts = urlsplit(url)
    return parts.hostname == APP_HOST and bool(BATCH_PATH.fullmatch(parts.path))


def request_allowed(method: str, url: str, body: str | None = None) -> bool:
    """The policy. Fails closed: anything not listed in endpoints.py is aborted.

    - google.allowed: GET/HEAD on photos.google.com, its media host, and Google's sign-in and static hosts (no
      sign-out), and Google's cookie rotation.
    - POST batchexecute on photos.google.com only when every rpcid (the URL's label and the f.req body agree) is in
      READ_RPCIDS. Trash, archive, favorite, album edits, sharing, and every unknown rpcid are aborted.
    Everything else is aborted: telemetry, uploads, every other POST.

    Limits: Playwright routes only the first URL of a redirect chain, so a redirect Google's server chooses for an
    allowed request is followed unchecked. Checking a batchexecute body means holding it, `at` token included, in
    memory for this call; it is never stored, logged, or printed.
    """
    method = method.upper()
    if google.allowed(method, url, HOSTS):
        return True
    if method != "POST" or urlsplit(url).scheme != "https" or not is_batch(url):
        return False
    rpcids = rpcids_of(url, body)
    return bool(rpcids) and all(r in READ_RPCIDS for r in rpcids)


def download_url_allowed(url: str) -> bool:
    """Whether an original may be fetched from `url` with page.request (which the route guard never sees): https on
    a DOWNLOAD_HOSTS host, default port, no credentials in the URL. Checked before every hop."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    return (parts.scheme == "https" and port is None and "@" not in parts.netloc
            and bool(DOWNLOAD_HOSTS.fullmatch(parts.hostname or "")))


def _body(request: "Request") -> str | None:
    # Only batchexecute bodies are read: upload bodies are binary and post_data would raise.
    return request.post_data if request.method == "POST" and is_batch(request.url) else None


def _allowed(request: "Request", state: Guard) -> bool:
    return request_allowed(request.method, request.url, _body(request))


def _describe(request: "Request") -> str:
    """METHOD host/path, plus rpcids=... for a batchexecute (token-looking ones redacted)."""
    text = describe(request.method, request.url)
    try:
        ids = rpcids_of(request.url, _body(request)) if is_batch(request.url) else None
    except Exception:
        ids = None
    if ids:
        text += f" rpcids={','.join(sorted({REDACTED if tokenish(r) else r for r in ids}))}"
    return text


POLICY = Policy("photos", _allowed, _describe)
