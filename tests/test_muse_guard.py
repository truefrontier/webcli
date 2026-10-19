"""Offline tests (from musecli) for muse's request policy: the HTTP allowlist, POSTs matched on their operation, sends
only in send mode, and the WebSocket guard (only wss://*.metaaivm.com, only from the running command's page, closed
when the command ends)."""

from __future__ import annotations

import asyncio

import pytest

from conftest import FIXTURES, FakeCDP
from webcli.core import guard
from webcli.sites.chatgpt.guard import POLICY as CHATGPT
from webcli.sites.muse import guard as muse_guard
from webcli.sites.muse.guard import POLICY, request_allowed, socket_allowed

FORM = (FIXTURES / "muse" / "graphql_form.txt").read_text()
GATEWAY = "wss://hatch.metaaivm.com/v1/noise?vm_id=12345&auth_token=secret"


@pytest.mark.parametrize(("method", "url", "allowed"), [
    ("GET", "https://muse.ai/", True),
    ("HEAD", "https://muse.ai/", True),
    ("GET", "https://muse.ai/thread/7c9e6679", True),
    ("GET", "https://muse.ai/api/session", True),
    ("GET", "https://auth.muse.ai/aymh/", True),  # the Meta session cycle
    ("GET", "https://www.facebook.com/aymh/redirect-cycle/", True),
    ("GET", "https://static.xx.fbcdn.net/rsrc.php/v4/x.js", True),
    ("GET", "https://muse.ai/api/logout?sessionBindingId=abc", False),
    ("POST", "https://muse.ai/api/auth/check", True),
    ("POST", "https://muse.ai/api/hatch/token", True),
    ("POST", "https://muse.ai/api/hatch/vm/wake", True),
    ("POST", "http://muse.ai/api/auth/check", False),
    ("POST", "https://evil.example/api/auth/check", False),
    ("POST", "https://muse.ai/api/auth/check/../x", False),
    ("POST", "https://muse.ai/api/logout", False),
    ("POST", "https://muse.ai/api/falco/v2", False),
    ("POST", "https://muse.ai/api/analytics", False),
    ("POST", "https://muse.ai/api/hatch/artifact-share", False),
    ("POST", "https://muse.ai/api/support/help-ticket", False),
    ("POST", "https://muse.ai/api/consent/record", False),
    ("POST", "https://o4509963614355457.ingest.us.sentry.io/api/1/envelope/", False),
    ("POST", "https://muse.ai/api/graphql", False),
    ("DELETE", "https://muse.ai/api/hatch/token", False),
    ("PUT", "https://muse.ai/api/auth/check", False),
    ("PATCH", "https://muse.ai/api/auth/check", False),
    ("OPTIONS", "https://muse.ai/api/auth/check", False),
])
def test_request_allowed(method, url, allowed):
    assert request_allowed(method, url) is allowed
    assert request_allowed(method, url, send=True) is allowed  # SEND_POSTS is empty: sending adds no HTTP request


def test_posts_match_on_their_operation(monkeypatch):
    monkeypatch.setattr(muse_guard, "READ_POSTS", {("muse.ai", "/api/graphql", "useHatchConversationsQuery")})
    url = "https://muse.ai/api/graphql"
    assert request_allowed("POST", url, {}, FORM) is True
    assert request_allowed("POST", url, {"x-fb-friendly-name": "useHatchConversationsQuery"}, FORM) is True
    # A mismatched label, a second name, or another op on the same path are all aborted.
    assert request_allowed("POST", url, {"x-fb-friendly-name": "useHatchDeleteSessionMutation"}, FORM) is False
    assert request_allowed("POST", url, {}, FORM + "&fb_api_req_friendly_name=useHatchDeleteSessionMutation") is False
    assert request_allowed("POST", url, {}, FORM.replace("Conversations", "DeleteSession")) is False
    assert request_allowed("POST", url, {}, None) is False
    assert request_allowed("POST", url, {}, "{broken") is False


def test_send_posts_only_when_sending(monkeypatch):
    monkeypatch.setattr(muse_guard, "SEND_POSTS", {("muse.ai", "/api/chat/send", None)})
    assert request_allowed("POST", "https://muse.ai/api/chat/send") is False
    assert request_allowed("POST", "https://muse.ai/api/chat/send", send=True) is True
    assert request_allowed("POST", "https://muse.ai/api/falco", send=True) is False


@pytest.mark.parametrize(("url", "allowed"), [
    (GATEWAY, True),
    ("wss://vm-1234.metaaivm.com/v1/noise", True),
    ("ws://hatch.metaaivm.com/v1/noise", False),
    ("wss://metaaivm.com.evil.example/v1/noise", False),
    ("wss://evilmetaaivm.com/v1/noise", False),
    ("wss://gateway.meta.ai/ws/clippy", False),
    ("wss://edge-chat.meta.ai/chat", False),
    ("wss://localhost:7700/", False),
])
def test_socket_allowed(url, allowed):
    assert socket_allowed(url) is allowed


class Request:
    def __init__(self, method, url, headers=None, body=None):
        self.method, self.url, self.headers, self.post_data = method, url, headers or {}, body


class Route:
    def __init__(self, request):
        self.request, self.outcome = request, None

    async def continue_(self):
        self.outcome = "continued"

    async def abort(self, reason):
        self.outcome = reason


class Socket:
    def __init__(self, url):
        self.url, self.outcome = url, None

    def connect_to_server(self):
        self.outcome = "connected"

    async def close(self):
        self.outcome = "closed"


def test_the_core_guard_runs_the_policy_and_records_blocks_without_secrets():
    route = guard.page_route(guard.state.begin(POLICY))
    routes = [Route(Request("GET", "https://muse.ai/")),
              Route(Request("POST", "https://muse.ai/api/auth/check")),
              Route(Request("POST", "https://muse.ai/api/graphql?x=secret", {}, FORM)),
              Route(Request("DELETE", "https://muse.ai/api/session/abc")),
              Route(Request("POST", "https://muse.ai/api/x/eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.c2ln"))]

    async def go():
        for r in routes:
            await route(r)

    asyncio.run(go())
    assert [r.outcome for r in routes] == ["continued", "continued"] + ["blockedbyclient"] * 3
    assert guard.state.blocked == ["POST muse.ai/api/graphql op=useHatchConversationsQuery",
                                   "DELETE muse.ai/api/session/abc", "POST muse.ai/api/x/<redacted>"]
    assert "secret" not in " ".join(guard.state.blocked)


def test_a_request_the_policy_cannot_read_fails_closed():
    class Unreadable(Request):
        @property
        def post_data(self):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "binary")

        @post_data.setter
        def post_data(self, _value):
            pass

    guard.state.begin(POLICY, send=True)
    assert not guard.check(Unreadable("POST", "https://muse.ai/api/auth/check"), own_page=True)
    assert guard.state.blocked == ["POST muse.ai/api/auth/check op=<labels disagree>"]


def test_only_the_gateway_socket_connects_and_only_from_the_commands_page():
    handler = guard.page_websocket(guard.state.begin(POLICY))
    sockets = [Socket(GATEWAY), Socket("wss://gateway.meta.ai/ws/clippy?token=secret"),
               Socket("ws://hatch.metaaivm.com/v1/noise")]

    async def go():
        for ws in sockets:
            await handler(ws)

    asyncio.run(go())
    assert [ws.outcome for ws in sockets] == ["connected", "closed", "closed"]
    assert guard.state.opened == [sockets[0]]
    assert guard.state.blocked == ["WS gateway.meta.ai/ws/clippy", "WS hatch.metaaivm.com/v1/noise"]
    assert "secret" not in " ".join(guard.state.blocked)


def test_other_pages_stale_pages_and_no_command_get_no_socket():
    stale = guard.page_websocket(guard.state.begin(POLICY))  # an earlier command's tab whose close failed
    guard.state.end()
    fresh = guard.page_websocket(guard.state.begin(POLICY))
    old, other, new = Socket(GATEWAY), Socket(GATEWAY), Socket(GATEWAY)
    asyncio.run(stale(old))
    asyncio.run(guard.websocket(other))  # every other page: the context's route
    asyncio.run(fresh(new))
    assert (old.outcome, other.outcome, new.outcome) == ("closed", "closed", "connected")
    guard.state.end()
    idle = Socket(GATEWAY)
    asyncio.run(fresh(idle))  # the daemon idle between commands
    assert idle.outcome == "closed"
    assert not guard.socket_allowed(GATEWAY, own_page=True)


def test_sites_without_sockets_still_close_every_one():
    handler = guard.page_websocket(guard.state.begin(CHATGPT))
    ws = Socket(GATEWAY)
    asyncio.run(handler(ws))
    assert ws.outcome == "closed" and CHATGPT.sockets is None


def test_a_policy_whose_socket_check_raises_fails_closed():
    broken = guard.Policy("broken", lambda request, state: True, lambda request: "x", sockets=lambda url: 1 / 0)
    ws = Socket(GATEWAY)
    asyncio.run(guard.page_websocket(guard.state.begin(broken))(ws))
    assert ws.outcome == "closed"


def test_the_commands_sockets_close_when_it_ends():
    handler = guard.page_websocket(guard.state.begin(POLICY))
    first, second = Socket(GATEWAY), Socket("wss://vm-1.metaaivm.com/v1/noise")

    class Stuck(Socket):
        async def close(self):
            raise RuntimeError("target closed")

    stuck = Stuck(GATEWAY)
    for ws in (first, stuck, second):
        asyncio.run(handler(ws))
    asyncio.run(guard.close_sockets())
    assert (first.outcome, second.outcome) == ("closed", "closed") and guard.state.opened == []


@pytest.mark.parametrize(("policy", "routes"), [(POLICY, ["route", "route_web_socket"]), (CHATGPT, ["route"])])
def test_page_at_gives_sockets_only_to_a_policy_that_lists_them_and_closes_them(policy, routes):
    from webcli.core import browser

    class Page:
        def __init__(self):
            self.calls, self.socket_handler = [], None

        async def route(self, _pattern, _handler):
            self.calls.append("route")

        async def route_web_socket(self, _pattern, handler):
            self.calls.append("route_web_socket")
            self.socket_handler = handler

        async def close(self):
            self.calls.append("close")

    class Context:
        def __init__(self):
            self.pages, self.page = [], Page()

        async def new_page(self):
            return self.page

        async def new_cdp_session(self, _page):
            return FakeCDP([])

    context, ws = Context(), Socket(GATEWAY)

    async def main():
        async with browser.page_at(context, policy, new=True) as page:
            if page.socket_handler:
                await page.socket_handler(ws)
                assert ws.outcome == "connected"

    asyncio.run(main())
    assert context.page.calls == [*routes, "close"]
    assert ws.outcome == ("closed" if policy is POLICY else None) and guard.state.opened == []


def test_page_at_revokes_the_lease_before_closing_sockets_even_if_cancelled():
    from webcli.core import browser

    class Hanging(Socket):
        async def close(self):
            raise asyncio.CancelledError  # the command was killed mid-cleanup

    class Page:
        closed = False

        async def route(self, *_):
            pass

        async def route_web_socket(self, _pattern, handler):
            self.handler = handler

        async def close(self):
            Page.closed = True

    class Context:
        pages: list = []

        async def new_page(self):
            return Page()

        async def new_cdp_session(self, _page):
            return FakeCDP([])

    async def main():
        async with browser.page_at(Context(), POLICY, new=True) as page:
            await page.handler(Hanging(GATEWAY))

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(main())
    assert guard.state.policy is None and guard.state.lease is None and Page.closed
