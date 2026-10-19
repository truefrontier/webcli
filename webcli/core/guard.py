"""The fail-closed request guard. Every request a guarded Chrome makes goes through check(); a site plugs in its
endpoints table as a Policy, and anything the policy doesn't allow is aborted.

`state` holds the running command's policy and permissions. It is set when the command's page opens and reset when
it closes (browser.page_at), so permissions belong to that page alone:
  - no command running (Chrome starting, the daemon idle): no policy, every request is aborted;
  - only the command's own page (and its frames) gets the policy, through a page route bound to that command's
    lease; every other page falls through to the context route, which aborts everything (sign-in excepted: it may
    finish in a new tab), and a page left over from an earlier command (a tab whose close failed) holds a stale
    lease, so it never inherits a later command's send or download permission;
  - send (the prompt and its uploads) only for `ask` / `-p`; download (gemini's full-size image rpc) only for
    `--image-out`; sign_in only for `identity login`.
WebSockets: closed, unless the policy names the ones its site needs (`sockets`: muse's VM gateway). Those connect
only from the command's own page while its lease runs, and are closed when the command ends (close_sockets).
A policy that raises counts as "no": a guard that raises would leave the request hanging.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable
from urllib.parse import urlsplit

from webcli.core.redact import redact_url

if TYPE_CHECKING:
    from playwright.async_api import Request, Route, WebSocketRoute


@dataclass(frozen=True)
class Policy:
    """A site's allowlist: allowed(request, state) and how a blocked request is described (no secrets)."""

    site: str
    allowed: Callable[["Request", "Guard"], bool]
    describe: Callable[["Request"], str]
    # The WebSocket URLs the site's page may open (muse's wss://*.metaaivm.com); None: none, every socket closes.
    sockets: Callable[[str], bool] | None = None


@dataclass
class Guard:
    policy: Policy | None = None
    send: bool = False
    download: bool = False
    sign_in: bool = False
    expect_models: frozenset[str] | None = None  # chatgpt -m: the model slugs a prompt may name
    blocked: list[str] = field(default_factory=list)  # what was aborted during this command, as describe() lines
    lease: object | None = None  # the running command's; its page's route carries the same object
    opened: list = field(default_factory=list)  # WebSockets this command's page connected; closed when it ends

    def begin(self, policy: Policy, *, send: bool = False, download: bool = False, sign_in: bool = False) -> object:
        """Start a command's permissions; returns its lease for the page's route."""
        self.policy, self.send, self.download, self.sign_in = policy, send, download, sign_in
        self.expect_models = None
        self.blocked.clear()
        self.lease = object()
        return self.lease

    def end(self) -> None:
        """Back to deny-all. `blocked` stays readable until the next command begins."""
        self.policy, self.send, self.download, self.sign_in = None, False, False, False
        self.expect_models, self.lease = None, None


state = Guard()


def describe(method: str, url: str) -> str:
    """METHOD host/path for error messages. No query string, and token-looking path segments are redacted."""
    parts = urlsplit(url)
    return f"{method} {redact_url(f'{parts.scheme}://{parts.netloc}{parts.path}').split('://', 1)[-1]}"


def host_matches(pattern: str, host: str) -> bool:
    return host.endswith(pattern[1:]) if pattern.startswith("*.") else host == pattern


def path_matches(prefix: str, path: str) -> bool:
    return path.startswith(prefix) if prefix.endswith("/") else path == prefix


def check(request: "Request", *, own_page: bool) -> bool:
    """Whether `request` may go out. own_page: it came through the command page's route."""
    policy = state.policy
    try:
        if policy is not None and (own_page or state.sign_in) and policy.allowed(request, state):
            return True
    except Exception:
        pass  # fail closed
    try:
        state.blocked.append(policy.describe(request) if policy else describe(request.method, request.url))
    except Exception:
        state.blocked.append(f"{getattr(request, 'method', '?')} <unreadable request>")
    return False


def page_route(lease: object) -> Callable[["Route"], Awaitable[None]]:
    """The route for a command's page: the policy decides while `lease` is the running command's."""

    async def handler(route: "Route") -> None:
        if check(route.request, own_page=lease is state.lease):
            await route.continue_()
        else:
            await route.abort("blockedbyclient")

    return handler


async def context_route(route: "Route") -> None:
    """Every other page (page routes take precedence): aborted, unless signing in."""
    if check(route.request, own_page=False):
        await route.continue_()
    else:
        await route.abort("blockedbyclient")


async def websocket(ws: "WebSocketRoute") -> None:
    """Every socket a page_websocket route doesn't take (gemini and chatgpt read replies from records or the page,
    and context.route never sees sockets): close it."""
    state.blocked.append(describe("WS", ws.url))
    await ws.close()


def socket_allowed(url: str, *, own_page: bool) -> bool:
    """Whether a WebSocket to `url` may connect: only from the command's own page, and only when its policy lists
    it."""
    policy = state.policy
    try:
        return bool(policy is not None and own_page and policy.sockets is not None and policy.sockets(url))
    except Exception:
        return False  # fail closed


def page_websocket(lease: object) -> Callable[["WebSocketRoute"], Awaitable[None]]:
    """The socket route for a command's page whose policy lists sockets (page routes take precedence over the
    context's `websocket`)."""

    async def handler(ws: "WebSocketRoute") -> None:
        if socket_allowed(ws.url, own_page=lease is state.lease):
            ws.connect_to_server()
            state.opened.append(ws)
        else:
            await websocket(ws)

    return handler


async def close_sockets() -> None:
    """Close the sockets the ending command connected (closing the page side closes the server side too), so a
    tab whose close fails keeps none."""
    while state.opened:
        ws = state.opened.pop()
        try:
            await asyncio.wait_for(ws.close(), timeout=5)
        except Exception:
            pass
