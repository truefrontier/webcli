"""youtube.com's request policy for the core guard: endpoints.py's tables (ytcli's rules), plus the one write scope.

POLICY is read-only. write_policy(scope) is `playlist create --confirm`'s alone: on top of the reads it passes ONE
POST /youtubei/v1/playlist/create whose JSON body is exactly the scope's title, privacy, and video list, only while
the command's page holds the core guard's send permission and fetch.create_playlist has armed the scope around its
own call. The first such write uses the scope up, so a second is refused.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from webcli.core.guard import Guard, Policy, describe
from webcli.sites import google
from webcli.sites.youtube.endpoints import (
    CREATE_KEYS,
    HOSTS,
    MYACTIVITY_BATCH,
    MYACTIVITY_HOST,
    PLAYLIST_CREATE,
    READ_POSTS,
)

if TYPE_CHECKING:
    from playwright.async_api import Request


@dataclass
class WriteScope:
    """The one playlist a `playlist create --confirm` run may create: exactly this title, privacy (PRIVATE, UNLISTED,
    PUBLIC), and video list. armed: while fetch.create_playlist's own call is in flight. used: the guard let it
    through (no second write)."""

    title: str
    privacy: str
    video_ids: tuple[str, ...]
    armed: bool = False
    used: bool = False


def _unique_keys(pairs: list) -> dict:
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key")
    return dict(pairs)


def _no_constant(name: str):
    raise ValueError(f"non-JSON constant {name}")


def create_body(body: str | None) -> dict | None:
    """A playlist/create body: a JSON object with exactly CREATE_KEYS, no key given twice at any depth, no NaN or
    Infinity. None for anything else."""
    try:
        data = json.loads(body or "", object_pairs_hook=_unique_keys, parse_constant=_no_constant)
    except (ValueError, TypeError, RecursionError):
        return None
    return data if isinstance(data, dict) and set(data) == CREATE_KEYS else None


def write_allowed(method: str, url: str, body: str | None, scope: WriteScope | None) -> bool:
    """Whether this request is the scope's one write: an armed, unused scope, and a POST to exactly
    https://www.youtube.com/youtubei/v1/playlist/create whose body carries exactly its title, privacy, and videos
    (same order). Allowing it uses the scope up."""
    if scope is None or scope.used or not scope.armed or method.upper() != "POST":
        return False
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.port is not None or "@" in parts.netloc
            or (parts.hostname, parts.path) != PLAYLIST_CREATE):
        return False
    data = create_body(body)
    if (data is None or data["title"] != scope.title or data["privacyStatus"] != scope.privacy
            or data["videoIds"] != list(scope.video_ids)):
        return False
    scope.used, scope.armed = True, False
    return True


def request_allowed(method: str, url: str, body: str | None = None, *, scope: WriteScope | None = None) -> bool:
    """The policy. Fails closed: anything not listed in endpoints.py is aborted.

    - google.allowed: GET/HEAD on youtube.com, its image hosts, My Activity, and Google's sign-in and static hosts
      (no sign-out on either), and Google's cookie rotation.
    - POST the three youtubei reads (READ_POSTS) on www.youtube.com, and My Activity's batchexecute.
    - With a scope (`playlist create --confirm` only), the one playlist/create write_allowed() accepts.
    Everything else is aborted: telemetry, the player, likes, subscriptions, comments, other playlist edits, history
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
    if method == "POST" and parts.scheme == "https" and parts.port in (None, 443):
        if (parts.hostname, parts.path) in READ_POSTS:
            return True
        if parts.hostname == MYACTIVITY_HOST and MYACTIVITY_BATCH.fullmatch(parts.path):
            return True
    return write_allowed(method, url, body, scope)


def _body(request: "Request") -> str | None:
    parts = urlsplit(request.url)
    if request.method.upper() != "POST" or (parts.hostname, parts.path) != PLAYLIST_CREATE:
        return None
    try:
        return request.post_data
    except Exception:  # binary: unreadable, so it can't match the scope
        return None


def _describe(request: "Request") -> str:
    return describe(request.method, request.url)


def write_policy(scope: WriteScope | None) -> Policy:
    """The policy for one command. A scope counts only while the page holds send (state.send): only `playlist create
    --confirm` opens its page that way."""

    def allowed(request: "Request", state: Guard) -> bool:
        return request_allowed(request.method, request.url, _body(request), scope=scope if state.send else None)

    return Policy("youtube", allowed, _describe)


POLICY = write_policy(None)
