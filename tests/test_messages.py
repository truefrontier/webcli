"""webcli messages, offline: the guard (phone actions sorted by their plaintext type, the one-send scope), reading the
action out of a relay body, gmsg's rows, and every verb against a scripted Google Messages page. Nothing reaches
Google: the page is scripted, no Chrome starts, and every phone action the scripted page makes (its mark-as-read,
typing, the send button's SEND_MESSAGE) goes through the real policy via guard.check, as the page route would. A
blocked action never leaves.
"""

from __future__ import annotations

import asyncio
import base64
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from webcli.apps import webcli as webcli_app
from webcli.core import browser, daemon, guard
from webcli.core.errors import AuthRequired, NotFound, UsageError
from webcli.sites.messages import fetch, guard as mguard, parse
from webcli.sites.messages.endpoints import ACTIONS, APP_URL, AUTH_PATH, INPUTS, SEND_BUTTON
from webcli.sites.messages.guard import POLICY, SendScope, outgoing_action, request_allowed, send_policy

from conftest import make_profile
from google_fakes import ScriptedPage

RPC = "https://instantmessaging-pa.googleapis.com/$rpc/google.internal.communications.instantmessaging.v1."
RPC_GOOGLE = "https://instantmessaging-pa.clients6.google.com/$rpc/google.internal.communications.instantmessaging.v1."
SEND = RPC + "Messaging/SendMessage"
A = {name: number for number, name in ACTIONS.items()}
ROWS = [
    {"href": "/web/conversations/101", "name": "Alice Smith", "preview": "See you at 6", "timestamp": "2:14 PM",
     "unread": True},
    {"href": "/web/conversations/102", "name": "Sam Jones", "preview": "ok", "timestamp": "Mon", "unread": False},
    {"href": "/web/conversations/103", "name": "Sam Lee", "preview": "Gate code 4417?", "timestamp": "Sun",
     "unread": False},
    {"href": "/web/conversations/104", "name": "+1 (555) 010-2000", "preview": "Your order shipped",
     "timestamp": "Oct 1", "unread": False},
]
BUBBLES = {"101": [{"sent": False, "text": "Dinner?", "image": False, "aria": "", "time": "2:10 PM"},
                   {"sent": True, "text": "Sure", "image": False, "aria": "Sent on October 19, 2:12 PM. ", "time": ""},
                   {"sent": False, "text": "", "image": True, "aria": "", "time": "2:13 PM"},
                   {"sent": False, "text": "See you at 6", "image": False, "aria": "", "time": "2:14 PM"}]}


# --- Relay bodies, built the way Google Messages' web client (and libgm) builds them --------------------------------

def _varint(n: int) -> bytes:
    out = b""
    while True:
        byte, n = n & 0x7F, n >> 7
        out += bytes([byte | 0x80 if n else byte])
        if not n:
            return out


def _field(number: int, value) -> bytes:
    if isinstance(value, int):
        return _varint(number << 3) + _varint(value)
    return _varint(number << 3 | 2) + _varint(len(value)) + value


def rpc_data(*actions: int) -> bytes:
    """An OutgoingRPCData: requestID, the action(s), an encrypted request, sessionID."""
    return (_field(1, b"req-1") + b"".join(_field(2, a) for a in actions) + _field(5, b"\x8f\x13 ciphertext")
            + _field(6, b"session-1"))


def pblite(*actions: int, route: int = 19, data: bytes | None = None, tail: bool = False, both: bool = False) -> bytes:
    """An OutgoingRPCMessage as PBLite JSON: messageData at data[11] (field 12), or in a trailing sparse object."""
    message_data = base64.b64encode(data if data is not None else rpc_data(*actions)).decode()
    fields = ["req-1", route] + [None] * 9 + ([None] if tail else [message_data]) + [None] * 10 + [[[], 2]]
    if tail or both:
        fields.append({"12": message_data})
    return json.dumps([["mobile"], fields, ["req-1", None, None, None, None, "dG9rZW4=", [2026, 9, 2, 4, 6]], None,
                       86400, None, None, None, ["dest"]]).encode()


def binary(action: int, route: int = 19) -> bytes:
    return _field(1, b"mobile") + _field(2, _field(1, b"req-1") + _field(2, route) + _field(12, rpc_data(action)))


# --- The scripted app ---------------------------------------------------------------------------------------------

class Locator:
    def __init__(self, web: "MessagesWeb", kind: str | None) -> None:
        self.web, self.kind = web, kind

    @property
    def first(self) -> "Locator":
        return self

    def nth(self, _i: int) -> "Locator":
        return self

    async def count(self) -> int:
        if self.kind == "send" and self.web.on_button:
            self.web.on_button(self.web)  # something happens while the command waits for the button
        return int(self.kind is not None)

    async def is_visible(self) -> bool:
        return True

    async def is_enabled(self) -> bool:
        return True

    async def fill(self, text: str) -> None:
        self.web.fills.append(text)
        self.web.composed = text
        if text:
            self.web.relay("TYPING_UPDATES")  # the app tells the phone you're typing

    async def evaluate(self, _script: str):
        return self.web.read_back if self.web.read_back is not None else self.web.composed

    async def click(self, **_options) -> None:
        self.web.clicks += 1
        if self.web.relay(self.web.click_sends) and self.web.composed:
            thread = self.web.bubbles.setdefault(self.web.thread, [])
            thread.append({"sent": True, "text": self.web.composed, "image": False, "aria": "", "time": "now"})
            self.web.delivered.append((self.web.thread, self.web.composed))
            self.web.composed = ""


class MessagesWeb:
    """messages.google.com as the commands see it. paired: the profile is paired (else the app goes to its QR page).
    use_here: another session holds Messages until the command clicks "Use here". Every phone action the page makes
    goes through guard.check with the command's real policy; `wire` records (action, let out?) in order."""

    def __init__(self, *, paired: bool = True, use_here: bool = False, rows=None, title=None, loads: bool = True,
                 read_back: str | None = None, click_sends: str = "SEND_MESSAGE", queued: str | None = None,
                 on_button=None, dies_after_send: bool = False) -> None:
        self.paired, self.use_here, self.loads = paired, use_here, loads
        self.rows = ROWS if rows is None else rows
        self.bubbles = {k: list(v) for k, v in BUBBLES.items()}
        self.title, self.read_back, self.click_sends, self.queued = title, read_back, click_sends, queued
        self.on_button, self.dies_after_send = on_button, dies_after_send
        self.path, self.thread, self.composed = "/", None, ""
        self.fills: list[str] = []
        self.clicks = self.use_here_clicks = 0
        self.wire: list[tuple[str, bool]] = []
        self.delivered: list[tuple[str, str]] = []
        self.page = ScriptedPage(answers={
            "/* messages:state */": self.state, "/* messages:use-here */": self.click_use_here,
            "/* messages:conversations */": lambda _s: list(self.rows), "/* messages:open */": self.open,
            "/* messages:messages */": self.thread_bubbles,
            "/* messages:header */": lambda _sels: self.title(self.thread) if callable(self.title) else self.title})
        self.page.options = {}
        self.page.locator = self.locator
        goto = self.page.goto

        async def load(url, **options):
            await goto(url, **options)
            self.path = AUTH_PATH if not self.paired else "/web/conversations"
            self.page.url = "https://messages.google.com" + self.path
            if self.paired:
                self.relay("LIST_CONVERSATIONS")
                self.relay_raw("POST", "https://play.google.com/log?format=json", b"[]")  # telemetry

        self.page.goto = load

    def relay_raw(self, method: str, url: str, body: bytes | None) -> bool:
        return guard.check(SimpleNamespace(method=method, url=url, post_data_buffer=body), own_page=True)

    def relay(self, action: str) -> bool:
        ok = self.relay_raw("POST", SEND, pblite(A[action]))
        self.wire.append((action, ok))
        return ok

    def state(self, _s) -> dict:
        listing = self.paired and self.loads and not self.use_here
        return {"path": self.path, "qr": not self.paired, "list": listing, "items": len(self.rows) if listing else 0,
                "loading": False, "useHere": self.use_here, "messages": len(self.bubbles.get(self.thread, [])),
                "thread": self.thread is not None, "compose": self.thread is not None}

    def thread_bubbles(self, _s) -> list[dict]:
        if self.dies_after_send and self.delivered:
            raise RuntimeError("Target page, context or browser has been closed")
        return list(self.bubbles.get(self.thread, []))

    def click_use_here(self, _patterns) -> bool:
        self.use_here_clicks += 1
        self.use_here = False
        self.relay("GET_UPDATES")  # this tab becomes the active session
        return True

    def open(self, arg) -> bool:
        _s, wanted = arg
        if not any(parse.thread_id(r["href"]) == wanted for r in self.rows):
            return False
        self.thread, self.path = wanted, f"/web/conversations/{wanted}"
        self.page.url = "https://messages.google.com" + self.path
        self.relay("LIST_MESSAGES")
        self.relay("MESSAGE_READ")  # opening an unread conversation marks it read on the phone
        if self.queued:
            self.relay(self.queued)  # a message the app still had queued tries to go out
        return True

    def locator(self, selector: str) -> Locator:
        if selector == INPUTS[0]:
            return Locator(self, "compose")
        return Locator(self, "send" if selector == SEND_BUTTON else None)

    def sent(self) -> list[str]:
        return [action for action, ok in self.wire if ok and action in ("SEND_MESSAGE", "SEND_REPLY", "RESEND_MESSAGE")]


@pytest.fixture
def web(monkeypatch):
    """install(MessagesWeb(...)): browser.site_page yields its page with the command's policy and guard state, as
    page_at does."""

    def install(site: MessagesWeb) -> MessagesWeb:
        @asynccontextmanager
        async def site_page(policy, **options):
            site.page.policy, site.page.options = policy, options
            guard.state.begin(policy, send=options.get("send", False))
            try:
                yield site.page
            finally:
                guard.state.end()

        monkeypatch.setattr(browser, "site_page", site_page)
        return site

    return install


# --- Reading the action out of a relay body -----------------------------------------------------------------------

def test_the_action_is_read_from_pblite_and_binary_bodies():
    assert outgoing_action(pblite(A["LIST_CONVERSATIONS"])) == 1
    assert outgoing_action(pblite(A["SEND_MESSAGE"])) == 3
    assert outgoing_action(pblite(A["MESSAGE_READ"], tail=True)) == 10  # messageData in the sparse tail
    assert outgoing_action(binary(A["SEND_MESSAGE"])) == 3
    urlsafe = pblite(A["LIST_MESSAGES"]).replace(b"+", b"-").replace(b"/", b"_")
    assert outgoing_action(urlsafe) == 2


@pytest.mark.parametrize("body", [
    None, b"", b"[]", b"not json", b"{}", b"[null, null]",
    pblite(A["LIST_CONVERSATIONS"], A["SEND_MESSAGE"]),  # the action given twice: protobuf would take the last
    pblite(A["LIST_CONVERSATIONS"], both=True),  # messageData both dense and sparse
    pblite(A["LIST_CONVERSATIONS"], route=14),  # the pairing route, not a phone action
    pblite(data=b"\x12"),  # truncated
    pblite(data=_field(1, b"req") + _field(5, b"x")),  # no action
    pblite(data=_field(2, b"3")),  # an action that isn't a number
    b'[["m"], ["r", 19, null, null, null, null, null, null, null, null, null, "!!notbase64!!"]]',
    b'[["m"], {"2": ["r", 19], "2": ["r", 19]}]',  # a JSON key given twice
    binary(A["LIST_CONVERSATIONS"], route=7),
])
def test_a_body_that_isnt_exactly_one_data_action_reads_as_none(body):
    assert outgoing_action(body) is None


# --- The policy ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("method", "url", "body", "ok"), [
    ("GET", APP_URL, None, True),
    ("GET", "https://messages.google.com/web/config?c.p=1", None, True),
    ("GET", "https://www.gstatic.com/messages/main.js", None, True),
    ("GET", "https://fonts.googleapis.com/css2?family=Google+Sans", None, True),
    ("HEAD", "https://fonts.gstatic.com/s/x.woff2", None, True),
    ("GET", "http://messages.google.com/web/conversations", None, False),
    ("GET", "https://messages.google.com:8443/web/conversations", None, False),
    ("GET", "https://play.google.com/log", None, False),
    ("GET", SEND, None, False),
    ("POST", "https://play.google.com/log?format=json", b"[]", False),
    ("POST", "https://messages.google.com/web/conversations", b"[]", False),
    ("POST", RPC + "Messaging/ReceiveMessages", b"[]", True),
    ("POST", RPC_GOOGLE + "Messaging/ReceiveMessages", b"[]", True),
    ("POST", RPC + "Messaging/AckMessages", b"[]", True),
    ("POST", RPC_GOOGLE + "Registration/RegisterRefresh", b"[]", True),
    ("POST", RPC + "Pairing/RegisterPhoneRelay", b"[]", False),
    ("POST", RPC + "Pairing/RefreshPhoneRelay", b"[]", False),
    ("POST", RPC + "Pairing/RevokeRelayPairing", b"[]", False),
    ("POST", RPC_GOOGLE + "Registration/SignInGaia", b"[]", False),
    ("POST", "https://instantmessaging-pa.googleapis.com/upload", b"x", False),
    ("OPTIONS", SEND, None, True),
    ("OPTIONS", RPC + "Messaging/ReceiveMessages", None, True),
    ("OPTIONS", RPC + "Pairing/RevokeRelayPairing", None, False),
    ("PUT", SEND, pblite(1), False),
    ("POST", SEND.replace("https:", "http:"), pblite(1), False),
    ("POST", SEND, pblite(A["LIST_CONVERSATIONS"]), True),
    ("POST", SEND, pblite(A["LIST_MESSAGES"]), True),
    ("POST", SEND, pblite(A["GET_UPDATES"]), True),
    ("POST", SEND, pblite(A["ACK_BROWSER_PRESENCE"]), True),
    ("POST", SEND, pblite(A["GET_PARTICIPANTS_THUMBNAIL"]), True),
    ("POST", SEND, pblite(A["SEND_MESSAGE"]), False),
    ("POST", SEND, binary(A["SEND_MESSAGE"]), False),
    ("POST", SEND, pblite(A["MESSAGE_READ"]), False),
    ("POST", SEND, pblite(A["TYPING_UPDATES"]), False),
    ("POST", SEND, pblite(A["SEND_REACTION"]), False),
    ("POST", SEND, pblite(A["SEND_REPLY"]), False),
    ("POST", SEND, pblite(A["RESEND_MESSAGE"]), False),
    ("POST", SEND, pblite(A["DELETE_MESSAGE"]), False),
    ("POST", SEND, pblite(A["UPDATE_CONVERSATION"]), False),
    ("POST", SEND, pblite(A["GET_OR_CREATE_CONVERSATION"]), False),
    ("POST", SEND, pblite(A["SETTINGS_UPDATE"]), False),
    ("POST", SEND, pblite(A["DOWNLOAD_MESSAGE"]), False),
    ("POST", SEND, pblite(A["UNPAIR_GAIA_PAIRING"]), False),
    ("POST", SEND, pblite(99), False),
    ("POST", SEND, b"[]", False),
])
def test_the_read_policy(method, url, body, ok):
    assert request_allowed(method, url, body) is ok


def test_a_send_scope_passes_one_send_message_only_while_armed():
    scope = SendScope()
    assert not request_allowed("POST", SEND, pblite(3), scope=scope)  # not armed: no slot before the page checks
    scope.arm(10)
    assert not request_allowed("POST", SEND, pblite(A["SEND_REPLY"]), scope=scope)  # another action: still a write
    assert not request_allowed("POST", SEND, pblite(A["MESSAGE_READ"]), scope=scope)
    assert not request_allowed("POST", SEND, pblite(3, 3), scope=scope)  # unreadable
    assert not request_allowed("POST", RPC + "Messaging/AckMessages" + "x", pblite(3), scope=scope)
    assert request_allowed("POST", SEND, pblite(A["LIST_MESSAGES"]), scope=scope)  # reads pass, and spend nothing
    assert request_allowed("POST", SEND, pblite(3), scope=scope)
    assert (scope.sent, scope.armed_until) == (True, None)
    assert not request_allowed("POST", SEND, pblite(3), scope=scope)  # once
    scope.arm(10)
    assert not request_allowed("POST", SEND, pblite(3), scope=scope)  # spent for good, even re-armed


def test_the_slot_closes_after_its_window(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(mguard.time, "monotonic", lambda: now[0])
    scope = SendScope()
    scope.arm(10)
    now[0] += 10.5
    assert not request_allowed("POST", SEND, pblite(3), scope=scope) and not scope.sent


def test_a_scope_counts_only_on_a_page_opened_with_send():
    request = SimpleNamespace(method="POST", url=SEND, post_data_buffer=pblite(3))
    scope = SendScope()
    scope.arm(10)
    assert not send_policy(scope).allowed(request, guard.Guard(send=False))
    assert not POLICY.allowed(request, guard.Guard(send=True))
    assert send_policy(scope).allowed(request, guard.Guard(send=True))


def test_blocked_lines_name_the_rpc_and_the_action_never_a_value():
    def line(method, url, body=None):
        return POLICY.describe(SimpleNamespace(method=method, url=url, post_data_buffer=body))

    assert line("POST", SEND + "?key=AIzaFAKEkey0123456789abcdef", pblite(A["MESSAGE_READ"])) == (
        "POST instantmessaging-pa.googleapis.com Messaging/SendMessage action=MESSAGE_READ")
    assert line("POST", SEND, b"[]").endswith("action=<unreadable>")
    assert line("POST", SEND, pblite(99)).endswith("action=#99")
    assert line("POST", RPC + "Pairing/RevokeRelayPairing", b"[]") == (
        "POST instantmessaging-pa.googleapis.com Pairing/RevokeRelayPairing")
    assert line("POST", "https://play.google.com/log?format=json") == "POST play.google.com/log"


# --- gmsg's rows ----------------------------------------------------------------------------------------------------

def test_conversation_and_message_rows():
    rows = parse.conversations(ROWS + [{"href": None, "name": "  ", "preview": None}, "junk"])
    assert rows[0] == {"id": "101", "name": "Alice Smith", "preview": "See you at 6", "timestamp": "2:14 PM",
                       "unread": True}
    assert rows[-1] == {"id": None, "name": "Unknown", "preview": "", "timestamp": "", "unread": False}
    assert parse.messages(BUBBLES["101"] + [{"sent": True, "text": " ", "image": False}]) == [
        {"direction": "received", "text": "Dinner?", "timestamp": "2:10 PM"},
        {"direction": "sent", "text": "Sure", "timestamp": "October 19, 2:12 PM"},  # from the aria-label
        {"direction": "received", "text": "[image]", "timestamp": "2:13 PM"},
        {"direction": "received", "text": "See you at 6", "timestamp": "2:14 PM"}]


@pytest.mark.parametrize(("value", "found"), [
    ("/web/conversations/101", "101"), ("https://messages.google.com/web/conversations/7?x=1", "7"),
    ("conversations/9", "9"), ("/web/conversations/new", None), ("/web/conversations", None), (None, None),
])
def test_thread_ids(value, found):
    assert parse.thread_id(value) == found


def test_which_conversation_a_query_names():
    rows = parse.conversations(ROWS)
    assert parse.resolve(rows, "103")["name"] == "Sam Lee"  # an id
    assert parse.resolve(rows, "alice")["id"] == "101"  # a name, ignoring case
    assert parse.resolve(rows, "sam lee")["id"] == "103"  # the exact name wins over two that contain "sam"
    assert parse.resolve(rows, "555-010-2000")["id"] == "104"  # a number, formatting aside
    assert parse.resolve(rows, "+15550102000")["id"] == "104"
    assert parse.resolve(rows, "nobody") is None and parse.resolve(rows, "555-999-0000") is None
    with pytest.raises(UsageError, match="2 conversations match 'sam'.*Use its id"):
        parse.resolve(rows, "sam")


def test_search_matches_name_number_and_the_latest_message():
    rows = parse.conversations(ROWS)
    assert [(r["id"], r["matched"]) for r in parse.search(rows, "sam")] == [("102", ["name"]), ("103", ["name"])]
    assert [(r["id"], r["matched"]) for r in parse.search(rows, "4417")] == [("103", ["preview"])]
    assert [(r["id"], r["matched"]) for r in parse.search(rows, "010")] == [("104", ["name", "number"])]
    assert parse.search(rows, "zebra") == []


# --- The verbs, against the scripted app ------------------------------------------------------------------------

def test_status_is_paired_or_not(web):
    web(MessagesWeb())
    assert asyncio.run(fetch.status()) == {"signedIn": True, "paired": True, "status": "active"}
    web(MessagesWeb(paired=False))
    assert asyncio.run(fetch.status()) == {"signedIn": False, "paired": False, "status": "needs_pairing"}


def test_list_reads_the_rows_and_lets_only_reads_out(web):
    site = web(MessagesWeb())
    rows = asyncio.run(fetch.conversations())
    assert [r["id"] for r in rows] == ["101", "102", "103", "104"] and site.page.options == {}
    assert asyncio.run(fetch.conversations(2)) == rows[:2]
    assert site.wire == [("LIST_CONVERSATIONS", True)] * 2
    assert "POST play.google.com/log" in guard.state.blocked  # telemetry never leaves


def test_another_session_is_taken_over_with_use_here(web):
    site = web(MessagesWeb(use_here=True))
    assert len(asyncio.run(fetch.conversations())) == 4
    assert site.use_here_clicks == 1 and ("GET_UPDATES", True) in site.wire


def test_not_paired_is_auth_required(web):
    web(MessagesWeb(paired=False))
    with pytest.raises(AuthRequired, match="isn't paired"):
        asyncio.run(fetch.conversations())


def test_a_list_that_never_loads_is_a_site_error_naming_what_was_blocked(web):
    web(MessagesWeb(loads=False))
    with pytest.raises(RuntimeError, match=r"didn't show its conversation list.*The guard blocked: POST play\.google"):
        asyncio.run(fetch.conversations())


def test_read_opens_the_conversation_and_leaves_it_unread(web):
    site = web(MessagesWeb())
    found = asyncio.run(fetch.messages("alice"))
    assert found["conversation"]["id"] == "101" and site.thread == "101"
    assert [m["text"] for m in found["messages"]] == ["Dinner?", "Sure", "[image]", "See you at 6"]
    assert ("LIST_MESSAGES", True) in site.wire and ("MESSAGE_READ", False) in site.wire  # mark-as-read blocked
    assert [m["text"] for m in asyncio.run(fetch.messages("101", 2))["messages"]] == ["[image]", "See you at 6"]


def test_read_finds_only_conversations_in_the_list(web):
    site = web(MessagesWeb())
    with pytest.raises(NotFound, match="No conversation matching 'nobody'"):
        asyncio.run(fetch.messages("nobody"))
    with pytest.raises(UsageError, match="2 conversations match"):
        asyncio.run(fetch.messages("sam"))
    assert site.thread is None


def test_a_send_dry_run_resolves_and_types_and_clicks_nothing(web):
    site = web(MessagesWeb())
    out = asyncio.run(fetch.send("alice", "  On my way  "))
    assert out == {"dryRun": True, "to": "Alice Smith", "threadId": "101", "text": "On my way"}
    assert site.page.options["send"] is False and site.page.policy.site == "messages"
    assert (site.fills, site.clicks, site.thread, site.delivered, site.sent()) == ([], 0, None, [], [])


def test_a_confirmed_send_lets_exactly_one_send_out_to_that_conversation(web):
    site = web(MessagesWeb(title="Alice Smith"))
    out = asyncio.run(fetch.send("alice", "On my way", confirm=True))
    assert out == {"dryRun": False, "to": "Alice Smith", "threadId": "101", "text": "On my way", "sent": True,
                   "confirmed": True, "titleChecked": True}
    assert site.page.options["send"] is True
    assert site.delivered == [("101", "On my way")] and site.sent() == ["SEND_MESSAGE"] and site.clicks == 1
    # Everything else the page tried while sending stayed in: mark-as-read and typing.
    assert ("MESSAGE_READ", False) in site.wire and ("TYPING_UPDATES", False) in site.wire
    assert [ok for action, ok in site.wire if action == "SEND_MESSAGE"] == [True]
    replay = SimpleNamespace(method="POST", url=SEND, post_data_buffer=pblite(3))
    assert not site.page.policy.allowed(replay, guard.Guard(send=True))  # one send per --confirm


@pytest.mark.parametrize(("site", "why"), [
    (dict(read_back="On my wa"), "the compose box doesn't hold exactly the text"),
    (dict(title="Bob"), "the open conversation is titled 'Bob', not 'Alice Smith'"),
    (dict(queued="RESEND_MESSAGE"), r"the page tried a send of its own \(POST .* action=RESEND_MESSAGE\)"),
    (dict(queued="SEND_MESSAGE"), r"the page tried a send of its own"),
])
def test_a_confirmed_send_refuses_before_the_slot_opens(web, site, why):
    page = web(MessagesWeb(**site))
    with pytest.raises(RuntimeError, match=f"REFUSED: {why}.*Nothing was sent"):
        asyncio.run(fetch.send("alice", "On my way", confirm=True))
    assert (page.clicks, page.delivered, page.sent()) == (0, [], [])
    assert page.composed == ""  # whatever was typed is cleared


def switch_to_sam(web: MessagesWeb) -> None:
    web.thread, web.path = "102", "/web/conversations/102"


@pytest.mark.parametrize(("hook", "why"), [
    (switch_to_sam, "the page moved to conversation 102, not 101"),
    (lambda web: web.relay("SEND_MESSAGE"), r"the page tried a send of its own \(POST .* action=SEND_MESSAGE\)"),
])
def test_what_happens_while_waiting_for_the_button_is_checked_right_before_the_slot_opens(web, hook, why):
    page = web(MessagesWeb(on_button=hook))
    with pytest.raises(RuntimeError, match=f"REFUSED: {why}.*Nothing was sent"):
        asyncio.run(fetch.send("alice", "On my way", confirm=True))
    assert (page.clicks, page.delivered, page.sent()) == (0, [], [])
    assert page.fills == (["On my way"] if hook is switch_to_sam else ["On my way", ""])  # another's draft: untouched


def test_a_page_that_dies_after_the_send_still_reports_it_sent(web):
    site = web(MessagesWeb(dies_after_send=True))
    out = asyncio.run(fetch.send("alice", "On my way", confirm=True))
    assert (out["sent"], out["confirmed"]) == (True, False) and site.delivered == [("101", "On my way")]


def test_a_click_that_sends_some_other_action_is_blocked_and_nothing_is_sent(web):
    site = web(MessagesWeb(click_sends="SEND_REPLY"))
    with pytest.raises(RuntimeError, match=r"sent nothing.*Nothing was sent.*action=SEND_REPLY"):
        asyncio.run(fetch.send("alice", "On my way", confirm=True))
    assert site.clicks == 1 and site.delivered == [] and site.sent() == []


def test_a_send_goes_only_to_a_conversation_in_the_list(web):
    site = web(MessagesWeb())
    with pytest.raises(NotFound):
        asyncio.run(fetch.send("+1 555 999 0000", "hi", confirm=True))
    assert (site.fills, site.clicks, site.sent()) == ([], 0, [])


# --- The CLI --------------------------------------------------------------------------------------------------------

@pytest.fixture
def calls(monkeypatch):
    make_profile("messages:default")
    seen, answers = [], {}

    async def call(ident, site, op, *args, **kwargs):
        seen.append((ident.id, op, args, kwargs))
        value = answers[op]
        return value(*args, **kwargs) if callable(value) else value

    monkeypatch.setattr(daemon, "call", call)
    return seen, answers


def run(*args):
    return CliRunner().invoke(webcli_app.build(), ["messages", *args])


def send_answer(query, text, *, confirm, headed):
    out = {"dryRun": not confirm, "to": "Alice Smith", "threadId": "101", "text": text}
    return {**out, "sent": True, "confirmed": True, "titleChecked": True} if confirm else out


@pytest.mark.parametrize(("flags", "confirm"), [([], False), (["--dry-run"], False), (["--confirm", "--dry-run"], False),
                                                (["--confirm"], True), (["--yes"], True)])
def test_send_is_a_dry_run_unless_confirm_alone(calls, flags, confirm):
    seen, answers = calls
    answers["send"] = send_answer
    result = run("send", "--to", "Alice", "--text", " hi ", *flags, "--format", "json")
    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert out["dryRun"] is (not confirm) and out.get("sent", False) is confirm
    assert seen == [("messages:default", "send", ("Alice", "hi"), {"confirm": confirm, "headed": False})]


def test_an_unconfirmed_send_says_so(calls):
    _, answers = calls
    answers["send"] = lambda *a, confirm, headed: {**send_answer(*a, confirm=confirm, headed=headed),
                                                   "confirmed": False}
    result = run("send", "--to", "Alice", "--text", "hi", "--yes")
    assert result.exit_code == 0 and "WARNING: the send went out" in result.stderr
    assert "webcli messages read 101" in result.stderr


@pytest.mark.parametrize("args", [("send", "--to", "Alice", "--text", "   "), ("send", "--to", " ", "--text", "hi"),
                                  ("send", "--text", "hi"), ("read", "  "), ("search", "")])
def test_bad_input_is_a_usage_error_before_any_call(calls, args):
    seen, _ = calls
    assert run(*args).exit_code == 2 and seen == []


@pytest.mark.parametrize(("info", "code"), [({"signedIn": True, "paired": True, "status": "active"}, 0),
                                            ({"signedIn": False, "paired": False, "status": "needs_pairing"}, 4)])
def test_status_exits_0_paired_and_4_not(calls, info, code):
    _, answers = calls
    answers["status"] = info
    result = run("status", "--format", "json")
    assert result.exit_code == code and json.loads(result.stdout)["paired"] is info["paired"]
    if code:
        assert "webcli identity login messages:default" in result.stderr


def test_list_and_threads_are_one_command_in_every_format(calls):
    seen, answers = calls
    answers["conversations"] = lambda limit, headed: parse.conversations(ROWS)[:limit]
    group = webcli_app.build().commands["messages"]
    assert group.commands["threads"] is group.commands["list"]
    assert [r["id"] for r in json.loads(run("threads", "--limit", "2", "--format", "json").stdout)] == ["101", "102"]
    assert run("list", "--csv").stdout.splitlines()[0] == "id,name,preview,timestamp,unread"
    assert run("list", "--format", "md", "--compact").stdout.splitlines()[0] == "| id | name | timestamp | unread |"
    assert seen[0][2] == (2,)


def test_read_prints_the_messages(calls):
    _, answers = calls
    answers["messages"] = lambda query, limit, headed: {"conversation": parse.conversations(ROWS)[0],
                                                        "messages": parse.messages(BUBBLES["101"])[-limit:]}
    result = run("read", "Alice", "--limit", "1", "--format", "json")
    assert result.exit_code == 0
    assert json.loads(result.stdout) == [{"direction": "received", "text": "See you at 6", "timestamp": "2:14 PM"}]


def test_search_exits_3_on_no_match(calls):
    _, answers = calls
    answers["conversations"] = lambda limit, headed: parse.conversations(ROWS)
    hits = json.loads(run("search", "gate code", "--format", "json").stdout)
    assert [(r["id"], r["matched"]) for r in hits] == [("103", ["preview"])]
    assert run("search", "zebra").exit_code == 3


def test_ambiguity_exits_2_and_not_found_exits_3(calls):
    _, answers = calls

    def ambiguous(*_a, **_k):
        raise UsageError("2 conversations match 'sam'.")

    def missing(*_a, **_k):
        raise NotFound("No conversation matching 'x'.")

    answers["messages"] = ambiguous
    assert run("read", "sam").exit_code == 2
    answers["messages"] = missing
    assert run("read", "x").exit_code == 3
