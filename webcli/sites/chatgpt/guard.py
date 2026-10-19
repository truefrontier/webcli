"""chatgpt.com's request policy for the core guard: endpoints.py's tables, verbatim from cgpt's browser.py."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from webcli.core.guard import Guard, Policy, describe, host_matches, path_matches
from webcli.sites.chatgpt.endpoints import (
    APP_HOST,
    CONVERSATION_PATHS,
    READ_HOSTS,
    READ_POSTS,
    SEND_REQUESTS,
    SIDE_EFFECT_GETS,
    UPLOADED,
)

if TYPE_CHECKING:
    from playwright.async_api import Request


def body_model(body: str | None) -> str | None:
    """The "model" a prompt request's JSON body asks for, else None."""
    try:
        data = json.loads(body or "")
    except ValueError:
        return None
    return data.get("model") if isinstance(data, dict) and isinstance(data.get("model"), str) else None


def request_allowed(method: str, url: str, body: str | None = None, *, send: bool = False, sign_in: bool = False,
                    expect_models: frozenset[str] | None = None) -> bool:
    """The policy. Fails closed: anything endpoints.py doesn't allow is aborted.

    - SEND_REQUESTS (the prompt, its sentinel check, --file uploads) and POST /backend-api/files/<id>/uploaded:
      only with send=True (only `ask` / `-p`), over https, with a listed method. Checked first, so no other
      rule (sign-in, GET) lets anything else reach them. With expect_models (-m), a prompt whose JSON body names
      another model is aborted too: the picker didn't take, so nothing goes out.
    - sign_in=True: any request to a host other than chatgpt.com, and chatgpt.com's own /api/auth/ and /auth/ pages
      (minus sign-out), so a person can sign in. (`identity login chatgpt:*` opens plain, unguarded Chrome instead.)
    - GET/HEAD on READ_HOSTS (chatgpt.com, its CDNs, Cloudflare's check), minus SIDE_EFFECT_GETS (sign-out).
    - POST to READ_POSTS (feature flags, Cloudflare's check).
    Everything else is aborted: PATCH/DELETE on chats (rename, archive, delete), share, memory, custom
    instructions, settings, title generation, feedback, analytics, telemetry, and every third-party host.

    Limits: Playwright routes only the first URL of a redirect chain, so a redirect the server chooses for an
    allowed request is followed unchecked. Checking a prompt's body means holding it in memory for this call;
    it is never stored, logged, or printed.
    """
    method = method.upper()
    parts = urlsplit(url)
    host, path = (parts.hostname or "").lower(), parts.path
    # A send path on chatgpt.com is claimed whatever the method (so only its listed methods pass, and only with
    # send); on a file host only the upload methods are, so reading images there stays a plain GET.
    send_only = [methods for methods, h, prefix in SEND_REQUESTS
                 if host_matches(h, host) and path_matches(prefix, path) and (h == APP_HOST or method in methods)]
    if host == APP_HOST and UPLOADED.match(path):
        send_only.append(("POST",))
    if send_only:
        if not (send and parts.scheme == "https" and any(method in methods for methods in send_only)):
            return False
        if expect_models and method == "POST" and host == APP_HOST and path in CONVERSATION_PATHS:
            return body_model(body) in expect_models  # fail closed: no model, no send
        return True
    if sign_in and host != APP_HOST:
        return True
    if sign_in and (path.startswith("/api/auth/") or path.startswith("/auth/")) \
            and not any(host == h and path.startswith(p) for h, p in SIDE_EFFECT_GETS):
        return True
    if any(host == h and path.startswith(p) for h, p in SIDE_EFFECT_GETS):
        return False
    if not READ_HOSTS.fullmatch(host):
        return False
    if method in ("GET", "HEAD"):
        return True
    if parts.scheme != "https" or method != "POST":
        return False
    return any(host == h and path.startswith(p) for h, p in READ_POSTS)


def _allowed(request: "Request", state: Guard) -> bool:
    # Only the prompt's body is read (for its model): upload bodies are binary and post_data would raise.
    parts = urlsplit(request.url)
    body = request.post_data if parts.hostname == APP_HOST and parts.path in CONVERSATION_PATHS else None
    return request_allowed(request.method, request.url, body, send=state.send, sign_in=state.sign_in,
                           expect_models=state.expect_models)


POLICY = Policy("chatgpt", _allowed, lambda request: describe(request.method, request.url))
