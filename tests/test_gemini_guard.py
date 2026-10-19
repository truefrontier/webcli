"""Offline tests (from gweb) for gemini's request policy and the core guard's fail-closed check."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import stat
import threading
import time
from urllib.parse import urlencode

import pytest

from webcli.core import guard
from webcli.sites.gemini import endpoints
from webcli.sites.gemini import guard as browser

BATCH = "https://gemini.google.com/_/BardChatUi/data/batchexecute"
GENERATE = "https://gemini.google.com/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate"
CHANNEL = "https://geminiweb-pa.clients6.google.com/v1/processSession"


def batch(*rpcids: str, url_ids: str | None = None) -> tuple[str, str]:
    """A batchexecute (url, body) pair the way the web app sends it."""
    url = BATCH + "?" + urlencode({"rpcids": ",".join(rpcids) if url_ids is None else url_ids,
                                   "source-path": "/app", "hl": "en", "_reqid": "100001", "rt": "c"})
    freq = [[[r, json.dumps([]), None, "generic"] for r in rpcids]]
    return url, urlencode({"f.req": json.dumps(freq), "at": "AJvLN6M-fake:1"})


def allowed(method, url, body=None, *, send=False):
    return browser.request_allowed(method, url, body, send=send)


@pytest.mark.parametrize(
    ("method", "url", "send", "ok"),
    [
        ("GET", "https://gemini.google.com/app", False, True),
        ("HEAD", "https://gemini.google.com/app", False, True),
        ("GET", "https://www.gstatic.com/lamda/images/gemini.svg", False, True),
        ("GET", "https://accounts.google.com/Logout?continue=x", False, False),
        ("GET", "https://accounts.google.com/Logout?continue=x", True, False),
        ("POST", "https://accounts.google.com/RotateCookies", False, True),
        ("POST", GENERATE + "?bl=x&_reqid=1&rt=c", False, False),  # read-only commands never send
        ("POST", GENERATE + "?bl=x&_reqid=1&rt=c", True, True),
        ("POST", "http://gemini.google.com/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate",
         True, False),
        ("POST", "https://gemini.google.com/_/BardChatUi/data/assistant.lamda.BardFrontendService/ProcessFile",
         False, False),
        ("POST", "https://gemini.google.com/_/BardChatUi/data/assistant.lamda.BardFrontendService/ProcessFile",
         True, True),
        ("POST", "https://gemini.google.com/_/BardChatUi/data/assistant.lamda.BardFrontendService/Other",
         True, False),
        ("POST", "https://content-push.googleapis.com/upload/", False, False),
        ("POST", "https://content-push.googleapis.com/upload/", True, True),
        ("PUT", "https://content-push.googleapis.com/upload/?upload_id=x", True, True),
        ("POST", "https://push.clients6.google.com/upload/", False, False),  # where --file bytes go now
        ("POST", "https://push.clients6.google.com/upload/", True, True),
        ("PUT", "https://push.clients6.google.com/upload/?upload_id=x", True, True),
        ("GET", "https://push.clients6.google.com/upload/", False, False),
        ("POST", "https://push.clients6.google.com/other", True, False),
        ("POST", "https://play.google.com/log?format=json", True, False),
        ("POST", "https://gemini.google.com/_/BardChatUi/jserror", True, False),
        ("DELETE", "https://gemini.google.com/app/1a2b3c4d5e6f7a8b", True, False),
        ("PUT", "https://gemini.google.com/app", True, False),
        ("OPTIONS", BATCH, True, False),
        ("GET", BATCH + "?rpcids=GzXR5e&f.req=%5B%5D", False, False),  # RPC paths are POST-only
        ("HEAD", GENERATE, True, False),
        ("GET", "https://gemini.google.com/_/BardChatUi/data/anything", True, False),
        # The prompt's WebChannel: only `ask` (send=True), only GET and POST, only this path.
        ("POST", CHANNEL + "?VER=8&RID=81906&CVER=22&zx=x&t=1", True, True),
        ("GET", CHANNEL + "?VER=8&RID=rpc&SID=x&CI=0&AID=3&TYPE=xmlhttp&zx=x&t=1", True, True),
        ("POST", CHANNEL + "?VER=8&SID=x&TYPE=terminate&zx=x", True, True),
        ("POST", CHANNEL + "?VER=8&RID=81906&CVER=22", False, False),  # read commands never send
        ("GET", CHANNEL + "?VER=8&RID=rpc&SID=x&TYPE=xmlhttp", False, False),  # not even the back channel
        ("HEAD", CHANNEL, True, False),
        ("PUT", CHANNEL, True, False),
        ("OPTIONS", CHANNEL, True, False),
        ("POST", "http://geminiweb-pa.clients6.google.com/v1/processSession", True, False),
        ("GET", "http://geminiweb-pa.clients6.google.com/v1/processSession", True, False),
        ("POST", "https://geminiweb-pa.clients6.google.com/v1/processSessionX", True, False),
        ("POST", "https://geminiweb-pa.clients6.google.com/v1/otherThing", True, False),
        ("POST", "https://geminiweb-pa.clients6.google.com/v1/otherThing", False, False),
        ("POST", "https://evil.example.com/v1/processSession", True, False),
        ("GET", "https://content-push.googleapis.com/upload/", False, False),  # send-only, whatever the method
    ],
)
def test_request_allowed(method, url, send, ok):
    assert allowed(method, url, send=send) is ok


@pytest.mark.parametrize("send", [False, True])
def test_read_rpcs_pass_and_every_known_mutation_is_blocked(send):
    for rpcid in endpoints.READ_RPCIDS:
        assert allowed("POST", *batch(rpcid), send=send), rpcid
    for rpcid in endpoints.MUTATING_RPCIDS:
        assert not allowed("POST", *batch(rpcid), send=send), rpcid
    assert not allowed("POST", *batch("zzUnknown"), send=send)  # fails closed


def test_batch_needs_url_and_body_to_agree():
    list_chats, read_chat = endpoints.RPC["list_chats"], endpoints.RPC["read_chat"]
    assert allowed("POST", *batch(list_chats, read_chat))
    assert not allowed("POST", *batch(list_chats, "GzXR5e"))  # one bad id sinks the whole batch
    assert not allowed("POST", *batch("GzXR5e", url_ids=list_chats))  # URL says read, body deletes
    assert not allowed("POST", *batch(list_chats, url_ids=f"{list_chats},GzXR5e"))
    url, body = batch(list_chats)
    _, delete = batch("GzXR5e")
    assert not allowed("POST", url, delete + "&" + body)  # two f.req fields: which one runs?
    assert not allowed("POST", url + "&rpcids=GzXR5e", body)  # two rpcids labels
    assert not allowed("POST", url, None)
    assert not allowed("POST", url, "f.req=not-json")
    assert not allowed("POST", url.replace("gemini.google.com", "evil.example.com"), batch(list_chats)[1])


def test_read_and_mutating_sets_do_not_overlap():
    assert not set(endpoints.READ_RPCIDS) & set(endpoints.MUTATING_RPCIDS)
    assert not set(endpoints.DOWNLOAD_RPCIDS) & (set(endpoints.MUTATING_RPCIDS) | set(endpoints.READ_RPCIDS))
    assert set(endpoints.RPC.values()) <= set(endpoints.READ_RPCIDS) | set(endpoints.DOWNLOAD_RPCIDS)


def test_the_full_size_image_rpc_needs_download_mode():
    full_size = endpoints.RPC["full_size_image"]
    assert full_size == "c8o8Fe"
    for send in (False, True):
        assert not browser.request_allowed("POST", *batch(full_size), send=send)  # not even plain `ask`
    assert browser.request_allowed("POST", *batch(full_size), send=True, download=True)
    assert browser.request_allowed("POST", *batch(full_size, endpoints.RPC["read_chat"]), download=True)
    for rpcid in endpoints.MUTATING_RPCIDS:  # download mode lets no write through, alone or riding along
        assert not browser.request_allowed("POST", *batch(rpcid), send=True, download=True)
        assert not browser.request_allowed("POST", *batch(full_size, rpcid), send=True, download=True)
    assert not browser.request_allowed("POST", *batch(full_size, url_ids="MaZiqc"), download=True)  # URL and body disagree


@pytest.mark.parametrize(("url", "ok"), [
    ("https://lh3.googleusercontent.com/gg-dl/AbC123=d-I-rp?alr=yes", True),  # the full-size link
    ("https://work.fife.usercontent.google.com/rd-gg-dl/AbC123=s0-d-I-rp?alr=yes", True),
    ("https://lh3.googleusercontent.com/rd-gg-dl/AbC123=s0-d-I-rp", True),
    ("https://lh3.google.com/rd-gg/AbC123=s0", True),  # the preview's hops
    ("https://lh5.googleusercontent.com/gg/AbC123", True),
    ("https://LH3.googleusercontent.com/gg/AbC123", True),  # hostnames are case-insensitive
    ("http://lh3.googleusercontent.com/gg/AbC123", False),
    ("https://lh3.googleusercontent.com:8443/gg/AbC123", False),
    ("https://user:pw@lh3.googleusercontent.com/gg/AbC123", False),
    ("https://lh3.googleusercontent.com.evil.example/gg/AbC123", False),
    ("https://evil.example/lh3.googleusercontent.com/gg/AbC123", False),
    ("https://googleusercontent.com/image_generation_content/0", False),
    ("https://lh.googleusercontent.com/gg/AbC123", False),
    ("https://fife.usercontent.google.com/x", False),
    ("https://gemini.google.com/_/BardChatUi/data/batchexecute", False),
    ("https://accounts.google.com/Logout", False),
    ("https://www.gstatic.com/lamda/images/gemini.svg", False),
    ("https://contribution.usercontent.google.com/download?c=x", False),
    ("data:image/png;base64,AAAA", False),
    ("javascript:alert(1)", False),
    ("https://lh3.googleusercontent.com:bad/gg/x", False),
    ("", False),
])
def test_image_downloads_only_reach_gemini_image_hosts(url, ok):
    assert browser.image_url_allowed(url) is ok


def test_sign_in_mode_frees_google_sign_in_but_not_gemini():
    def ok(method, url, body=None):
        return browser.request_allowed(method, url, body, sign_in=True)

    assert ok("POST", "https://accounts.google.com/v3/signin/_/AccountsSignInUi/data/batchexecute?rpcids=x")
    assert ok("POST", "https://accounts.google.com/Logout")  # a person is at the window
    assert ok("POST", *batch(endpoints.RPC["list_chats"]))
    assert not ok("POST", *batch("L5adhe"))  # Gemini settings stay off-limits during login
    assert not ok("POST", GENERATE)
    assert not ok("POST", CHANNEL + "?VER=8&RID=1")  # nor can a prompt typed in the login window go out
    assert not ok("GET", CHANNEL + "?TYPE=xmlhttp")
    assert not ok("POST", "https://content-push.googleapis.com/upload/")


def _allowed(request, send):
    """The core guard's check for a request on the command's own page, under the gemini policy."""
    guard.state.begin(browser.POLICY, send=send)
    return guard.check(request, own_page=True)


def test_the_guard_records_what_it_blocks_without_query_strings_or_bodies():

    class Request:
        method = "POST"
        url = BATCH + "?rpcids=GzXR5e&f.sid=-4271880281862545069"
        post_data = batch("GzXR5e")[1]

    assert _allowed(Request(), send=True) is False
    assert guard.state.blocked == ["POST gemini.google.com/_/BardChatUi/data/batchexecute rpcids=GzXR5e"]

    class Socket:
        url = "wss://gemini.google.com/ws?token=secret"
        closed = False

        async def close(self):
            self.closed = True

    assert browser.describe("GET", "https://cdn.example.com/u/4c81102d95782c63a49ab00b9621a6e9ec723e3e/x?sig=1") == (
        "GET cdn.example.com/u/<redacted>/x")
    odd = urlencode({"f.req": json.dumps([[["AJvLN6Mx4nqzQ0aTestTokenOnlyAAAA:1760450400123", "[]"]]])})
    assert browser.describe("POST", BATCH, odd).endswith("rpcids=<redacted>")

    socket = Socket()
    asyncio.run(guard.websocket(socket))
    assert socket.closed and guard.state.blocked[-1] == "WS gemini.google.com/ws"
    guard.state.blocked.clear()


def test_guard_fails_closed_when_the_body_cannot_be_read():
    class Request:
        method, url = "POST", batch(endpoints.RPC["list_chats"])[0]

        @property
        def post_data(self):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "binary")

    class Upload:
        method, url = "POST", "https://content-push.googleapis.com/upload/"

        @property
        def post_data(self):
            raise AssertionError("upload bodies are binary and must not be read")

    class Channel:
        method, url = "POST", CHANNEL + "?VER=8&RID=81906&CVER=22"

        @property
        def post_data(self):
            raise AssertionError("WebChannel bodies are protobuf and must not be read")

    assert _allowed(Request(), send=True) is False
    assert _allowed(Upload(), send=True) is True
    assert _allowed(Channel(), send=True) is True
    assert _allowed(Channel(), send=False) is False
    assert guard.state.blocked[-1] == "POST geminiweb-pa.clients6.google.com/v1/processSession"
    guard.state.blocked.clear()
