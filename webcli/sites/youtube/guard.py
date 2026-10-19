"""youtube.com's request policy for the core guard: endpoints.py's tables (ytcli's rules)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from webcli.core.guard import Guard, Policy, describe
from webcli.sites import google
from webcli.sites.youtube.endpoints import HOSTS, MYACTIVITY_BATCH, MYACTIVITY_HOST, READ_POSTS

if TYPE_CHECKING:
    from playwright.async_api import Request


def request_allowed(method: str, url: str) -> bool:
    """The policy. Fails closed: anything not listed in endpoints.py is aborted.

    - google.allowed: GET/HEAD on youtube.com, its image hosts, My Activity, and Google's sign-in and static hosts
      (no sign-out on either), and Google's cookie rotation.
    - POST the three youtubei reads (READ_POSTS) on www.youtube.com, and My Activity's batchexecute.
    Everything else is aborted: telemetry, the player, likes, subscriptions, comments, playlist edits, history
    pause/clear, and every unknown POST.

    Limits: My Activity's batchexecute is allowed for any rpcid, as in ytcli: its read rpcids aren't mapped yet,
    and webcli youtube only scrolls that page (a delete there needs clicks and a confirmation it never makes).
    Playwright routes only the first URL of a redirect chain, so a redirect Google's server chooses for an allowed
    request is followed unchecked.
    """
    method = method.upper()
    if google.allowed(method, url, HOSTS):
        return True
    parts = urlsplit(url)
    if method != "POST" or parts.scheme != "https" or parts.port not in (None, 443):
        return False
    if (parts.hostname, parts.path) in READ_POSTS:
        return True
    return parts.hostname == MYACTIVITY_HOST and bool(MYACTIVITY_BATCH.fullmatch(parts.path))


def _allowed(request: "Request", state: Guard) -> bool:
    return request_allowed(request.method, request.url)


def _describe(request: "Request") -> str:
    return describe(request.method, request.url)


POLICY = Policy("youtube", _allowed, _describe)
