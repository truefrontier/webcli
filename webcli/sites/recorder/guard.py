"""recorder.google.com's request policy for the core guard: endpoints.py's tables."""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import unquote, urlsplit

from webcli.core.guard import Guard, Policy, describe
from webcli.sites import google
from webcli.sites.recorder.endpoints import AUDIO_HOSTS, HOSTS, READ_METHODS, RPC_HOST, RPC_PREFIX

if TYPE_CHECKING:
    from playwright.async_api import Request


def rpc_method(url: str) -> str | None:
    """The PlaybackService method an https pixelrecorder-pa URL calls; None for anything else."""
    parts = urlsplit(url)
    path = unquote(parts.path)
    if parts.scheme != "https" or parts.hostname != RPC_HOST or parts.port is not None:
        return None
    if not path.startswith(RPC_PREFIX):
        return None
    method = path[len(RPC_PREFIX):]
    return method if method and "/" not in method else None


def request_allowed(method: str, url: str) -> bool:
    """The policy. Fails closed: anything not listed in endpoints.py is aborted.

    - google.allowed: GET/HEAD on recorder.google.com, its audio host, and Google's sign-in and static hosts (no
      sign-out), and Google's cookie rotation.
    - POST (and its CORS preflight, OPTIONS) to exactly the PlaybackService methods in READ_METHODS.
    Everything else is aborted: renames, deletes, sharing, edits, every other method and service, telemetry.

    Limits: Playwright routes only the first URL of a redirect chain, so a redirect Google's server chooses for an
    allowed request is followed unchecked.
    """
    method = method.upper()
    if google.allowed(method, url, HOSTS):
        return True
    return method in ("POST", "OPTIONS") and rpc_method(url) in READ_METHODS


def audio_url_allowed(url: str) -> bool:
    """Whether audio may be fetched from `url` with page.request (which the route guard never sees): https on an
    AUDIO_HOSTS host, default port, no credentials in the URL. Checked before every hop."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    return (parts.scheme == "https" and port is None and "@" not in parts.netloc
            and bool(AUDIO_HOSTS.fullmatch(parts.hostname or "")))


def _allowed(request: "Request", state: Guard) -> bool:
    return request_allowed(request.method, request.url)


def _describe(request: "Request") -> str:
    return describe(request.method, request.url)


POLICY = Policy("recorder", _allowed, _describe)
