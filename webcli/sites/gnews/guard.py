"""news.google.com's request policy: google.allowed's base and nothing more. GET/HEAD on news.google.com and
Google's sign-in/static hosts (minus sign-out); POST only Google's cookie rotation. So every batchexecute RPC (follow,
hide, "fewer stories like this", save, and the page's own feed calls) and all logging are aborted: webcli reads what
the page rendered with GETs alone."""

from __future__ import annotations

from typing import TYPE_CHECKING

from webcli.core.guard import Guard, Policy, describe
from webcli.sites import google
from webcli.sites.gnews.endpoints import HOSTS

if TYPE_CHECKING:
    from playwright.async_api import Request


def request_allowed(method: str, url: str) -> bool:
    return google.allowed(method.upper(), url, HOSTS)


def _allowed(request: "Request", _state: Guard) -> bool:
    return request_allowed(request.method, request.url)


POLICY = Policy("gnews", _allowed, lambda request: describe(request.method, request.url))
