"""www.perplexity.ai's request policy for the core guard: read-only and fail-closed.

Allowed: GET/HEAD, https on the default port, to www.perplexity.ai at exactly the paths in endpoints.READ_PATHS
(the /discover document, the Discover feed and topic list, the session check). Everything else is aborted: every
script, style, font and image (the page never runs, so it sends no query, like, follow, bookmark or beacon), every
POST/PUT/PATCH/DELETE, every other /rest/* or /api/* path, and every other host (analytics, Cloudflare's beacon).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from webcli.core.guard import Guard, Policy, describe
from webcli.sites.perplexity.endpoints import APP_HOST, READ_PATHS

if TYPE_CHECKING:
    from playwright.async_api import Request


def request_allowed(method: str, url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.port not in (None, 443) or "@" in parts.netloc:
        return False
    return method.upper() in ("GET", "HEAD") and parts.hostname == APP_HOST and parts.path in READ_PATHS


def _allowed(request: "Request", _state: Guard) -> bool:
    return request_allowed(request.method, request.url)


def _describe(request: "Request") -> str:
    return describe(request.method, request.url)


POLICY = Policy("perplexity", _allowed, _describe)
