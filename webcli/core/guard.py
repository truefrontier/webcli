"""The fail-closed request guard. Every request a guarded Chrome makes goes through check(); a site plugs in its
endpoints table as a Policy, and anything the policy doesn't allow is aborted.

`state` holds the running command's policy and permissions. It is set when the command's page opens and reset when
it closes (browser.page_at), so permissions belong to that page alone:
  - no command running (Chrome starting, the daemon idle): no policy, every request is aborted;
  - only the command's own page (and its frames) gets the policy, through a page route; every other page falls
    through to the context route, which aborts everything (sign-in excepted: it may finish in a new tab);
  - send (the prompt and its uploads) only for `ask` / `-p`; download (gemini's full-size image rpc) only for
    `--image-out`; sign_in only for `identity login`.
A policy that raises counts as "no": a guard that raises would leave the request hanging.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable
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


@dataclass
class Guard:
    policy: Policy | None = None
    send: bool = False
    download: bool = False
    sign_in: bool = False
    expect_models: frozenset[str] | None = None  # chatgpt -m: the model slugs a prompt may name
    blocked: list[str] = field(default_factory=list)  # what was aborted during this command, as describe() lines

    def begin(self, policy: Policy, *, send: bool = False, download: bool = False, sign_in: bool = False) -> None:
        self.policy, self.send, self.download, self.sign_in = policy, send, download, sign_in
        self.expect_models = None
        self.blocked.clear()

    def end(self) -> None:
        """Back to deny-all. `blocked` stays readable until the next command begins."""
        self.policy, self.send, self.download, self.sign_in = None, False, False, False
        self.expect_models = None


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


async def page_route(route: "Route") -> None:
    """On the command's page: the policy decides."""
    if check(route.request, own_page=True):
        await route.continue_()
    else:
        await route.abort("blockedbyclient")


async def context_route(route: "Route") -> None:
    """Every other page (page routes take precedence): aborted, unless signing in."""
    if check(route.request, own_page=False):
        await route.continue_()
    else:
        await route.abort("blockedbyclient")


async def websocket(ws: "WebSocketRoute") -> None:
    """No site needs WebSockets (replies are read from records or the page), and context.route never sees them:
    close every one."""
    state.blocked.append(describe("WS", ws.url))
    await ws.close()
