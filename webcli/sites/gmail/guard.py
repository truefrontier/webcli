"""mail.google.com's request policy for the core guard: endpoints.py's tables, plus the one write scope.

POLICY is read-only, and strict: GET/HEAD on mail.google.com and Google's sign-in and static hosts, none carrying a
legacy action (act / at / logout), and Google's cookie rotation. Every POST is aborted: Gmail's sync, telemetry,
attestation, and every action.

write_policy(scope) is the policy of `filters create|delete --confirm` alone. It also passes ONE POST to
/mail/u/0/ whose act is that command's (endpoints.WRITE_ACTS) and whose query or form carries every scope needle as a
whole parameter value (the filter id to delete; each typed criterion to create), and only while the command's page
holds the core guard's send permission. The first such write uses the scope up, so a second is refused.

Limit: a create is bound to its act and its typed criteria, not to its actions or "has attachment": their wire
names are unverified. Only webcli's own clicks pick them (fetch.create_filter), and the page's own scripts don't
create filters.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlsplit

from webcli.core.guard import Guard, Policy, describe
from webcli.sites import google
from webcli.sites.gmail.endpoints import ACTION_PARAMS, ACTION_PATH, APP_HOST, HOSTS, WRITE_ACTS

if TYPE_CHECKING:
    from playwright.async_api import Request

_ACT = re.compile(r"[A-Za-z_]{1,16}")


@dataclass
class WriteScope:
    """The one write a `filters create|delete --confirm` run may send: `action` (create|delete), a request carrying
    every needle as a whole parameter value. used: the guard let it through (no second write)."""

    action: str
    needles: tuple[str, ...] = ()
    used: bool = False


def _params(url: str, body: str | None) -> list[tuple[str, str]]:
    return (parse_qsl(urlsplit(url).query, keep_blank_values=True)
            + (parse_qsl(body, keep_blank_values=True) if body else []))


def is_action(url: str) -> bool:
    parts = urlsplit(url)
    return parts.hostname == APP_HOST and parts.path == ACTION_PATH


def write_allowed(method: str, url: str, body: str | None, scope: WriteScope | None) -> bool:
    """Whether this request is the scope's one write. Allowing it uses the scope up."""
    if scope is None or scope.used or method != "POST" or scope.action not in WRITE_ACTS:
        return False
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.port is not None or "@" in parts.netloc or not is_action(url):
        return False
    params = _params(url, body)
    acts = {v for k, v in params if k == "act"}
    if len(acts) != 1 or not acts <= WRITE_ACTS[scope.action]:
        return False
    values = {v for _, v in params}
    if not all(needle in values for needle in scope.needles):  # whole values: 123456 never matches 91234567
        return False
    scope.used = True
    return True


def request_allowed(method: str, url: str, body: str | None = None, *, scope: WriteScope | None = None) -> bool:
    """The policy. Fails closed: anything not listed here or in endpoints.py is aborted.

    - GET/HEAD: google.allowed (mail.google.com and Google's sign-in and static hosts, no sign-out), and never with
      an act, at, or logout parameter (legacy Gmail actions ride GETs too).
    - POST: Google's cookie rotation; and, with a scope (`filters create|delete --confirm` only), the one write
      write_allowed() accepts.
    Everything else is aborted.

    Limits: Playwright routes only the first URL of a redirect chain, so a redirect Google's server chooses for an
    allowed request is followed unchecked. A POST body the guard can't read (not form data) can't carry the needles,
    so the write is refused.
    """
    method = method.upper()
    if method in ("GET", "HEAD"):
        keys = {k for k, _ in parse_qsl(urlsplit(url).query, keep_blank_values=True)}
        return not keys & ACTION_PARAMS and google.allowed(method, url, HOSTS)
    if google.allowed(method, url, HOSTS):
        return True
    return write_allowed(method, url, body, scope)


def _body(request: "Request") -> str | None:
    if request.method.upper() != "POST" or not is_action(request.url):
        return None
    try:
        return request.post_data
    except Exception:  # binary: unreadable, so no needle can match
        return None


def _describe(request: "Request") -> str:
    """METHOD host/path, plus act=... for a legacy Gmail action (an action name, never a token)."""
    text = describe(request.method, request.url)
    acts = sorted({v for k, v in parse_qsl(urlsplit(request.url).query) if k == "act"})
    return text + (f" act={','.join(a if _ACT.fullmatch(a) else '<redacted>' for a in acts)}" if acts else "")


def write_policy(scope: WriteScope | None) -> Policy:
    """The policy for one command. A scope counts only while the page holds send (state.send): only `filters
    create|delete --confirm` opens its page that way."""

    def allowed(request: "Request", state: Guard) -> bool:
        return request_allowed(request.method, request.url, _body(request), scope=scope if state.send else None)

    return Policy("gmail", allowed, _describe)


POLICY = write_policy(None)
