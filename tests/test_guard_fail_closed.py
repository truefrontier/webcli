"""Required (4): the core guard fails closed. Unlisted rpcids, hosts, and POSTs are aborted; listed reads pass;
sends only in send mode; nothing at all with no command running or from another page."""

from __future__ import annotations

import asyncio
import json
from urllib.parse import urlencode

import pytest

from webcli.core import guard
from webcli.sites.chatgpt.guard import POLICY as CHATGPT
from webcli.sites.gemini.guard import POLICY as GEMINI

BATCH = "https://gemini.google.com/_/BardChatUi/data/batchexecute"
CHANNEL = "https://geminiweb-pa.clients6.google.com/v1/processSession"


class Request:
    def __init__(self, method: str, url: str, post_data: str | None = None) -> None:
        self.method, self.url, self.post_data = method, url, post_data


def batch(rpcid: str) -> Request:
    body = urlencode({"f.req": json.dumps([[[rpcid, "[]", None, "generic"]]]), "at": "AJvLN6M-fake:1"})
    return Request("POST", f"{BATCH}?{urlencode({'rpcids': rpcid, 'rt': 'c'})}", body)


def allowed(policy, request, *, send=False, download=False, own_page=True) -> bool:
    guard.state.begin(policy, send=send, download=download)
    return guard.check(request, own_page=own_page)


def test_an_unlisted_rpcid_is_blocked_and_a_listed_read_passes():
    assert allowed(GEMINI, batch("MaZiqc"))  # list chats: a listed read
    assert not allowed(GEMINI, batch("GzXR5e"), send=True)  # delete chat: unlisted
    assert not allowed(GEMINI, batch("zzNever"), send=True)
    assert guard.state.blocked == ["POST gemini.google.com/_/BardChatUi/data/batchexecute rpcids=zzNever"]


@pytest.mark.parametrize(("policy", "request_"), [
    (CHATGPT, Request("GET", "https://www.google-analytics.com/collect")),
    (CHATGPT, Request("POST", "https://evil.example/backend-api/f/conversation")),
    (CHATGPT, Request("POST", "https://chatgpt.com/backend-api/memories")),  # an unlisted POST on the app host
    (CHATGPT, Request("PATCH", "https://chatgpt.com/backend-api/conversation/68f3c1a2-1234-8000-9abc-0123456789ab")),
    (GEMINI, Request("POST", "https://play.google.com/log?format=json")),
    (GEMINI, Request("POST", "https://gemini.google.com/_/BardChatUi/jserror")),
    (GEMINI, Request("PUT", "https://gemini.google.com/app")),
])
def test_unlisted_hosts_and_posts_are_blocked_even_in_send_mode(policy, request_):
    assert not allowed(policy, request_, send=True)


def test_listed_reads_pass():
    assert allowed(CHATGPT, Request("GET", "https://chatgpt.com/backend-api/models?iim=false"))
    assert allowed(CHATGPT, Request("POST", "https://ab.chatgpt.com/v1/initialize"))
    assert allowed(GEMINI, Request("GET", "https://gemini.google.com/app"))
    assert allowed(GEMINI, Request("POST", "https://accounts.google.com/RotateCookies"))


@pytest.mark.parametrize(("policy", "request_"), [
    (CHATGPT, Request("POST", "https://chatgpt.com/backend-api/f/conversation", '{"model": "gpt-6"}')),
    (CHATGPT, Request("POST", "https://chatgpt.com/backend-api/files")),
    (GEMINI, Request("POST", CHANNEL + "?VER=8&RID=1")),
    (GEMINI, Request("POST", "https://push.clients6.google.com/upload/")),
])
def test_sends_only_in_send_mode(policy, request_):
    assert not allowed(policy, request_, send=False)
    assert allowed(policy, request_, send=True)


def test_the_full_size_image_rpc_only_with_download():
    assert not allowed(GEMINI, batch("c8o8Fe"), send=True)
    assert allowed(GEMINI, batch("c8o8Fe"), send=True, download=True)


def test_no_command_running_means_nothing_passes():
    guard.state.end()
    assert not guard.check(Request("GET", "https://chatgpt.com/"), own_page=True)
    assert not guard.check(batch("MaZiqc"), own_page=True)
    assert guard.state.blocked[-1] == "POST gemini.google.com/_/BardChatUi/data/batchexecute"


def test_another_page_gets_nothing_unless_signing_in():
    assert not allowed(CHATGPT, Request("GET", "https://chatgpt.com/"), own_page=False)
    guard.state.begin(GEMINI, sign_in=True)
    assert guard.check(Request("GET", "https://accounts.google.com/signin"), own_page=False)  # a sign-in tab
    assert not guard.check(Request("POST", CHANNEL), own_page=False)  # still no prompt


def test_a_policy_that_raises_fails_closed():
    class Unreadable:
        method, url = "POST", batch("MaZiqc").url

        @property
        def post_data(self):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "binary")

    assert not allowed(GEMINI, Unreadable(), send=True)
    broken = guard.Policy("broken", lambda request, state: 1 / 0, lambda request: "x")
    assert not allowed(broken, Request("GET", "https://chatgpt.com/"))
    assert guard.state.blocked == ["x"]


def test_routes_continue_or_abort_and_websockets_close():
    class Route:
        def __init__(self, request):
            self.request, self.outcome = request, None

        async def continue_(self):
            self.outcome = "continue"

        async def abort(self, reason):
            self.outcome = reason

    class Socket:
        url, closed = "wss://chatgpt.com/ws?token=secret", False

        async def close(self):
            self.closed = True

    route = guard.page_route(guard.state.begin(CHATGPT))
    ok, bad, other = (Route(Request("GET", "https://chatgpt.com/")), Route(Request("GET", "https://evil.example/")),
                      Route(Request("GET", "https://chatgpt.com/")))
    asyncio.run(route(ok))
    asyncio.run(route(bad))
    asyncio.run(guard.context_route(other))
    socket = Socket()
    asyncio.run(guard.websocket(socket))
    assert (ok.outcome, bad.outcome, other.outcome, socket.closed) == ("continue", "blockedbyclient",
                                                                       "blockedbyclient", True)
    assert guard.state.blocked[-1] == "WS chatgpt.com/ws"


def test_a_page_left_over_from_an_earlier_command_inherits_nothing():
    """A tab whose close failed keeps its route; a later command's send permission must not reach it."""
    class Route:
        def __init__(self, request):
            self.request, self.outcome = request, None

        async def continue_(self):
            self.outcome = "continue"

        async def abort(self, reason):
            self.outcome = reason

    stale = guard.page_route(guard.state.begin(GEMINI))  # an earlier command's page
    guard.state.end()
    fresh = guard.page_route(guard.state.begin(GEMINI, send=True))  # now `ask` runs
    prompt = Request("POST", CHANNEL + "?VER=8&RID=1")
    old, new = Route(prompt), Route(prompt)
    asyncio.run(stale(old))
    asyncio.run(fresh(new))
    assert (old.outcome, new.outcome) == ("blockedbyclient", "continue")
