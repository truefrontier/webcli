"""www.amazon.com's request policy for the core guard. HARD read-only, and fail-closed.

- GET/HEAD: https on www.amazon.com / amazon.com under endpoints.READ_PATHS only (order history, order details, the
  printable invoice, ship-track, Your Payments transactions, wish lists), and never a path naming a cart, buy,
  checkout, return, cancel, subscribe, delete, edit, or sign-out; plus any GET of Amazon's static image/CSS/JS hosts.
- POST: only the transactions page's pager, a form-encoded body whose every widget event is a next/previous page
  navigation. No other POST, PUT, PATCH, or DELETE goes out: no purchase, cart change, return, cancellation,
  subscription, wish list edit, or account edit, and no telemetry.
- Everything else, every other host included, is aborted and named in the error.

No command clicks anything but the transactions pager's own Next button; the guard is what makes that a promise.

Limits: Playwright routes only the first URL of a redirect chain, so a redirect Amazon's server chooses for an allowed
request (to sign-in, say) is followed unchecked.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlsplit

from webcli.core.guard import Guard, Policy, describe, host_matches
from webcli.sites.amazon.endpoints import (
    DENY_WORDS,
    PAGER_EVENTS,
    PAGER_PATH_RE,
    PAGER_PATHS,
    READ_PATHS,
    SITE_HOSTS,
    STATIC_HOSTS,
)

if TYPE_CHECKING:
    from playwright.async_api import Request

_PAGER = re.compile(PAGER_PATH_RE)
EVENT_PREFIX = "ppw-widgetEvent:"


def read_path(path: str) -> bool:
    """A path that is one of READ_PATHS or below it (Amazon appends /ref=... segments), naming no write."""
    low = path.lower()
    if any(word in low for word in DENY_WORDS):
        return False
    return any(low == prefix or low.startswith(prefix + "/") for prefix in READ_PATHS)


def pager_body(body: str | None) -> bool:
    """A form-encoded body with at least one widget event, every one a page navigation, and only ppw- fields."""
    if not body or len(body) > 200_000:
        return False
    try:
        fields = parse_qsl(body, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return False
    events = []
    for key, _value in fields:
        if not key.startswith("ppw-"):
            return False
        if key.startswith(EVENT_PREFIX):
            events.append(key[len(EVENT_PREFIX):].split(":", 1)[0])
    return bool(events) and all(name in PAGER_EVENTS for name in events)


def request_allowed(method: str, url: str, body: str | None = None) -> bool:
    """The policy (see the module doc). Fails closed."""
    method = method.upper()
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    host, path = (parts.hostname or "").lower(), parts.path or "/"
    if parts.scheme != "https" or port not in (None, 443) or "@" in parts.netloc:
        return False
    if method in ("GET", "HEAD"):
        if any(host_matches(pattern, host) for pattern in STATIC_HOSTS):
            return True
        return host in SITE_HOSTS and read_path(path)
    if method == "POST" and host in SITE_HOSTS:
        if path in PAGER_PATHS or _PAGER.fullmatch(path):
            return pager_body(body)
    return False


def _body(request: "Request") -> str | None:
    if request.method.upper() != "POST":
        return None
    try:
        return request.post_data
    except Exception:  # binary: not the pager's form, so refused
        return None


def _allowed(request: "Request", state: Guard) -> bool:
    return request_allowed(request.method, request.url, _body(request))


def _describe(request: "Request") -> str:
    """METHOD host/path (never a query string or a body)."""
    return describe(request.method, request.url)


POLICY = Policy("amazon", _allowed, _describe)
