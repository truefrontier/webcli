"""keep.google.com's request policy for the core guard: endpoints.py's tables."""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from webcli.core.guard import Guard, Policy, describe
from webcli.sites import google
from webcli.sites.keep.endpoints import API_HOST, CHANGES_PATH, HOSTS, PROXY_PATH
from webcli.sites.keep.parse import read_only_sync

if TYPE_CHECKING:
    from playwright.async_api import Request


def _api_path(url: str) -> str | None:
    """The path of an https API_HOST URL on the default port; None for anything else."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or port is not None or "@" in parts.netloc or parts.hostname != API_HOST:
        return None
    return parts.path


def is_changes(url: str) -> bool:
    return _api_path(url) == CHANGES_PATH


def request_allowed(method: str, url: str, body: str | None = None) -> bool:
    """The policy. Fails closed: anything not listed in endpoints.py is aborted.

    - google.allowed: GET/HEAD on keep.google.com and Google's sign-in and static hosts (no sign-out), and Google's
      cookie rotation.
    - GET gapi's proxy iframe on the API host (the page's API calls go out from it, each still checked here).
    - The notes/v1/changes sync: its CORS preflight (OPTIONS, no body), and a POST only when read_only_sync(body):
      no nodes (note edits) and no userInfo (label changes) to upload.
    Everything else is aborted: telemetry, other Keep APIs, reminders, sharing, every unknown POST.

    Limits: Playwright routes only the first URL of a redirect chain, so a redirect Google's server chooses for an
    allowed request is followed unchecked (a 307/308 keeps the same read-only body).
    """
    method = method.upper()
    if google.allowed(method, url, HOSTS):
        return True
    if method == "GET" and _api_path(url) == PROXY_PATH:
        return True
    if not is_changes(url):
        return False
    return method == "OPTIONS" or (method == "POST" and read_only_sync(body))


def _allowed(request: "Request", state: Guard) -> bool:
    body = request.post_data if request.method == "POST" and is_changes(request.url) else None
    return request_allowed(request.method, request.url, body)


def _describe(request: "Request") -> str:
    return describe(request.method, request.url)


POLICY = Policy("keep", _allowed, _describe)
