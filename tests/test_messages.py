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
from webcli.core.errors import AuthRequired, NotFound, ProfileBusy, UsageError
from webcli.sites.messages import fetch, guard as mguard, parse
from webcli.sites.messages.endpoints import (
    ACTIONS,
    APP_URL,
    AUTH_PATH,
    INPUTS,
    MEDIA_HOST,
    MEDIA_PATH,
    SELECTORS,
    SEND_BUTTON,
)
from webcli.sites.messages.guard import POLICY, SendScope, outgoing_action, request_allowed, send_policy

from conftest import make_profile
from google_fakes import ScriptedPage

RPC = "https://instantmessaging-pa.googleapis.com/$rpc/google.internal.communications.instantmessaging.v1."
RPC_GOOGLE = "https://instantmessaging-pa.clients6.google.com/$rpc/google.internal.communications.instantmessaging.v1."
SEND = RPC + "Messaging/SendMessage"
SEND_GOOGLE = RPC_GOOGLE + "Messaging/SendMessage"
MEDIA = f"https://{MEDIA_HOST}{MEDIA_PATH}"
JPEG = b"\xff\xd8\xff\xe0 jpeg "
JMS = "https://instantmessaging-pa-jms-us.clients6.google.com/$rpc/google.internal.communications.instantmessaging.v1."
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

def saved_files(where) -> list:
    """The files a command saved in `where` (the temp WEBCLI_HOME lives there too)."""
    return [p for p in where.iterdir() if p.name != "home"]


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
        if self.kind == "contact":  # the Start chat picker's input: it filters contacts, nothing reaches a chat
            if not text and self.web.typed and self.web.group_lag:
                self.web.group_hidden = 1
            self.web.typed = text
            self.web.typed_log.append(text)
            return
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
            thread.append({"sent": True, "text": self.web.bubble_text(self.web.composed), "image": False, "aria": "",
                           "time": "now"})
            self.web.delivered.append((self.web.thread, self.web.composed))
            self.web.composed = ""


class MessagesWeb:
    """messages.google.com as the commands see it. paired: the profile is paired (else the app goes to its QR page).
    use_here: another session holds Messages until the command clicks "Use here". Every phone action the page makes
    goes through guard.check with the command's real policy; `wire` records (action, let out?) in order."""

    def __init__(self, *, paired: bool = True, use_here: bool = False, rows=None, title=None, loads: bool = True,
                 read_back: str | None = None, click_sends: str = "SEND_MESSAGE", queued: str | None = None,
                 on_button=None, dies_after_send: bool = False, full_size: bool = True, sticky: bool = False,
                 older=None, contacts=None, groups=None, page_size: int = 2, new_title=None, details=None,
                 live_match: bool = False, group_lag: bool = False, sticky_details: bool = False,
                 bubble_text=None) -> None:
        self.paired, self.use_here, self.loads, self.full_size = paired, use_here, loads, full_size
        self.sticky = sticky  # the lightbox ignores Escape
        self.rows = ROWS if rows is None else rows
        self.bubbles = {k: list(v) for k, v in BUBBLES.items()}
        self.title, self.read_back, self.click_sends, self.queued = title, read_back, click_sends, queued
        self.on_button, self.dies_after_send = on_button, dies_after_send
        self.path, self.thread, self.composed, self.online = "/", None, "", False
        self.media = False  # the app's attachment download got out: thumbnails and the lightbox can load
        self.viewing: tuple[int, int] | None = None  # (bubble, part) the lightbox shows
        self.fills: list[str] = []
        self.clicks = self.use_here_clicks = 0
        self.wire: list[tuple[str, bool]] = []
        self.delivered: list[tuple[str, str]] = []
        # Deeper lookups: `older` rows arrive `page_size` at a time on "Load more"; the Start chat picker filters
        # `contacts` (raw picker rows: {name, number, continued}) and `groups` ({href, name}) by what is typed.
        self.older = list(older or [])
        self.contacts, self.groups, self.page_size = list(contacts or []), list(groups or []), page_size
        self.picking, self.typed, self.group_mode = False, "", False
        self.typed_log: list[str] = []
        self.chips: list[str] = []
        self.loads_more = 0
        self.created: list[list[str]] = []
        self.new_title = new_title  # the title a conversation Start chat opens shows (default: its members)
        self.titles: dict[str, str] = {}
        # The conversation menu's Details dialog: `details` {thread: [{name, number}] as the dialog shows them (a
        # non-contact's number in the name's place)}; by default the members of the row's name, a contact's number
        # from `contacts`, and a created conversation's picked numbers. live_match: the picker matches a contact by
        # its number as saved, from the start (LIVE: "+1 555..." is found by "+1555...", not by "555...").
        # group_lag: "Start group conversation" is missing for one click after the input is emptied (LIVE).
        self.details = dict(details or {})
        self.bubble_text = bubble_text or (lambda text: text)  # how a sent text reads back in its bubble
        self.live_match, self.group_lag, self.sticky_details = live_match, group_lag, sticky_details
        self.menu_open = self.details_open = False
        self.details_reads = 0
        self.group_hidden = 0
        self.picked: list[str] = []
        self.numbers: dict[str, list[str]] = {}
        self.page = ScriptedPage(answers={
            "/* messages:details */": self.details_shown, "/* messages:overlay */": lambda _s: (
                self.details_open or self.menu_open),
            "/* messages:picker */": self.picker, "/* messages:click */": self.click_selector,
            "/* messages:pick */": self.pick,
            "/* messages:state */": self.state, "/* messages:use-here */": self.click_use_here,
            "/* messages:conversations */": lambda _s: list(self.rows), "/* messages:open */": self.open,
            "/* messages:messages */": self.thread_bubbles,
            "/* messages:header */": lambda _sels: self.titles.get(self.thread) or (
                self.title(self.thread) if callable(self.title) else self.title),
            "/* messages:open-image */": self.open_image, "/* messages:preview */": self.preview,
            "/* messages:lightbox */": self.lightbox, "/* messages:blob */": self.blob})
        self.page.keyboard = SimpleNamespace(press=self.press)
        self.page.options = {}
        self.page.locator = self.locator
        goto = self.page.goto

        async def load(url, **options):
            await goto(url, **options)
            if url == "about:blank":  # the command leaves the app before it lets the session go
                self.path, self.picking, self.page.url = "", False, url
                return
            if "/web/conversations/" in url and self.path.startswith("/web/conversations"):
                wanted = url.rsplit("/", 1)[-1]  # a conversation's own link, in the app already
                if any(parse.thread_id(r["href"]) == wanted for r in [*self.rows, *self.groups]):
                    self.thread, self.path = wanted, f"/web/conversations/{wanted}"
                    self.page.url = "https://messages.google.com" + self.path
                return
            self.path = AUTH_PATH if not self.paired else "/web/conversations"
            if self.paired:
                # As the live app: it signs in and checks its connection first. Without the sign-in it never shows
                # the list (it gives up on unableToConnect); without the check it asks the phone for no messages.
                if not self.relay_raw("POST", RPC_GOOGLE + "Registration/SignInGaia", b"[]"):
                    self.path = "/web/unableToConnect"
                self.online = self.relay_raw("POST", RPC_GOOGLE + "Messaging/Echo", b"[]")
                self.relay("LIST_CONVERSATIONS")
                self.relay_raw("POST", "https://play.google.com/log?format=json", b"[]")  # telemetry
            self.page.url = "https://messages.google.com" + self.path

        self.page.goto = load

    def relay_raw(self, method: str, url: str, body: bytes | None) -> bool:
        return guard.check(SimpleNamespace(method=method, url=url, post_data_buffer=body), own_page=True)

    def relay(self, action: str) -> bool:
        ok = self.relay_raw("POST", SEND, pblite(A[action]))
        self.wire.append((action, ok))
        return ok

    def state(self, _s) -> dict:
        listing = self.paired and self.loads and not self.use_here and self.path != "/web/unableToConnect"
        return {"path": self.path, "qr": not self.paired, "list": listing, "items": len(self.rows) if listing else 0,
                "loading": False, "useHere": self.use_here, "messages": len(self.shown()),
                "thread": self.thread is not None, "compose": self.thread is not None}

    def shown(self) -> list[dict]:
        return self.bubbles.get(self.thread, []) if self.online else []

    def thread_bubbles(self, _s) -> list[dict]:
        if self.dies_after_send and self.delivered:
            raise RuntimeError("Target page, context or browser has been closed")
        return list(self.shown())

    def click_use_here(self, _patterns) -> bool:
        self.use_here_clicks += 1
        self.use_here = False
        self.relay("GET_UPDATES")  # this tab becomes the active session
        return True

    def open(self, arg) -> bool:
        _s, wanted = arg
        if not any(parse.thread_id(r["href"]) == wanted for r in self.rows):
            return False
        self.thread, self.path, self.picking = wanted, f"/web/conversations/{wanted}", False
        self.page.url = "https://messages.google.com" + self.path
        if self.online:
            self.relay("LIST_MESSAGES")
            if any(b.get("image") for b in self.shown()):  # the thumbnails: the app downloads each attachment
                self.media = self.relay_raw("GET", MEDIA, None)
        self.relay("MESSAGE_READ")  # opening an unread conversation marks it read on the phone
        if self.queued:
            self.relay(self.queued)  # a message the app still had queued tries to go out
        return True

    def open_image(self, arg) -> bool:
        _s, bubble, part = arg
        shown = self.shown()
        if bubble >= len(shown) or part >= int(shown[bubble].get("images") or shown[bubble].get("image")):
            return False
        self.viewing = (bubble, part)
        return True

    def preview(self, arg) -> str | None:
        _s, bubble, part = arg
        return f"blob:https://messages.google.com/thumb-{bubble}-{part}" if self.media else None

    def lightbox(self, _sel) -> dict | None:
        if self.viewing is None:
            return None
        loaded = self.media and self.full_size
        return {"src": "blob:https://messages.google.com/full-%d-%d" % self.viewing if loaded else None}

    def blob(self, src: str) -> str:
        return "data:image/jpeg;base64," + base64.b64encode(JPEG + src.rsplit("/", 1)[-1].encode()).decode()

    async def press(self, key: str) -> None:
        self.page.keys.append(key)
        if key == "Escape" and not self.sticky:
            self.viewing = None
        if key == "Escape" and not self.sticky_details:
            self.menu_open = self.details_open = False

    def locator(self, selector: str) -> Locator:
        if selector == SELECTORS["contactInput"]:
            return Locator(self, "contact" if self.picking else None)
        if selector == INPUTS[0]:
            return Locator(self, "compose")
        return Locator(self, "send" if selector == SEND_BUTTON else None)

    # --- Deeper lookups and Start chat ---------------------------------------------------------------------------

    def _blocks(self) -> list[list[dict]]:
        """The contacts as blocks: a row and the continuation rows (more numbers) under it."""
        blocks: list[list[dict]] = []
        for row in self.contacts:
            if row.get("continued") and blocks:
                blocks[-1].append(row)
            else:
                blocks.append([row])
        return blocks

    def _shown_contacts(self) -> list[dict]:
        typed = self.typed.strip()
        if not typed:
            return []
        want = parse.digits(typed)
        out = []
        for block in self._blocks():
            name = block[0].get("name", "")
            if self.live_match:
                by_number = any(parse.digits(r.get("number")).startswith(want) for r in block)
            else:
                by_number = any(want in parse.digits(r.get("number")) for r in block)
            if typed.casefold() in name.casefold() or (len(want) >= 3 and by_number):
                out.extend(block)
        return out

    def picker(self, _s) -> dict:
        if not self.picking:
            return {"open": False}
        shown = self._shown_contacts()
        typed = self.typed.strip()
        number = parse.normalize_number(typed)
        groups = [] if not typed or number else [g for g in self.groups if typed.casefold() in g["name"].casefold()]
        return {"open": True, "input": self.typed, "contacts": shown,
                "sendTo": typed if number and not shown else None, "groups": groups, "loading": False,
                "chips": list(self.chips), "next": self.group_mode and bool(self.chips)}

    def click_selector(self, selector: str) -> bool:
        if selector == SELECTORS["start"]:
            self.picking, self.path, self.thread = True, "/web/conversations/new", None
            self.page.url = "https://messages.google.com" + self.path
            return True
        if selector == SELECTORS["loadMore"]:
            if not self.older:
                return False
            self.loads_more += 1
            self.relay("LIST_CONVERSATIONS")
            self.rows = [*self.rows, *self.older[:self.page_size]]
            self.older = self.older[self.page_size:]
            return True
        if selector == SELECTORS["startGroup"] and self.picking:
            if self.group_hidden:
                self.group_hidden -= 1
                return False
            self.group_mode = True
            return True
        if selector == SELECTORS["menu"] and self.thread and not self.picking:
            self.menu_open = True
            return True
        if selector == SELECTORS["detailsButton"] and self.menu_open:
            self.menu_open, self.details_open = False, True
            self.details_reads += 1
            return True
        if selector == SELECTORS["dialogCancel"] and self.details_open and not self.sticky_details:
            self.details_open = False
            return True
        if selector == SELECTORS["next"] and self.group_mode and self.chips:
            return self.create(list(self.chips))
        return False

    def pick(self, arg) -> bool:
        _s, kind, index = arg
        if kind == "sendTo":
            shown = number = self.typed.strip()
        else:
            rows = self._shown_contacts()
            if index >= len(rows):
                return False
            name = ""
            for row in rows[:index + 1]:
                name = row.get("name") or name if not row.get("continued") else name
            shown, number = name or rows[index]["number"], rows[index]["number"]
        self.picked.append(number)
        if self.group_mode:
            self.chips.append(shown)
            return True
        return self.create([shown])

    def create(self, members: list[str]) -> bool:
        """GET_OR_CREATE_CONVERSATION: the conversation opens only if the guard lets it out."""
        if not self.relay("GET_OR_CREATE_CONVERSATION"):
            return True  # the click happened; the page just never opens a conversation
        thread = f"n{len(self.created) + 1}"
        self.created.append(members)
        self.numbers[thread], self.picked = self.picked[-len(members):], []
        name = ", ".join(members)
        self.rows = [{"href": f"/web/conversations/{thread}", "name": name, "preview": "", "timestamp": "",
                      "unread": False}, *self.rows]
        self.titles[thread] = self.new_title if self.new_title is not None else name
        self.thread, self.path, self.picking = thread, f"/web/conversations/{thread}", False
        self.page.url = "https://messages.google.com" + self.path
        return True

    def _contact_number(self, name: str) -> str:
        for block in self._blocks():
            if block[0].get("name", "").casefold() == name.casefold():
                return block[0].get("number", "")
        return ""

    def members(self, thread: str) -> list[dict]:
        """The Details dialog's people for `thread`, as the page shows them."""
        if thread in self.details:
            return self.details[thread]
        row = next((r for r in [*self.rows, *self.groups] if parse.thread_id(r["href"]) == thread), None)
        parts = parse.header_parts(self.titles.get(thread) or (row or {}).get("name"))
        numbers = self.numbers.get(thread) or [None] * len(parts)
        out = []
        for part, number in zip(parts, numbers):
            if parse.normalize_number(part):
                out.append({"name": part, "number": ""})  # not a contact: the number in the name's place
            else:
                out.append({"name": part, "number": number or self._contact_number(part) or "(202) 555-0188"})
        return out

    def details_shown(self, _s) -> list[dict] | None:
        return self.members(self.thread) if self.details_open and self.thread else None

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
            guard.state.begin(policy, send=options.get("send", False), download=options.get("download", False))
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
    ("POST", RPC_GOOGLE + "Registration/SignInGaia", b"[]", True),  # the app's sign-in, on clients6
    ("POST", RPC_GOOGLE + "Messaging/Echo", b"[]", True),  # its connection check
    ("OPTIONS", RPC_GOOGLE + "Registration/SignInGaia", None, True),
    ("POST", RPC + "Registration/SignInGaia", b"[]", False),  # only on the host the app calls them on
    ("POST", RPC + "Messaging/Echo", b"[]", False),
    ("GET", RPC_GOOGLE + "Messaging/Echo", None, False),
    ("POST", RPC_GOOGLE.replace("https:", "http:") + "Registration/SignInGaia", b"[]", False),
    ("POST", RPC_GOOGLE + "Messaging/Echo/x", b"[]", False),
    ("POST", JMS + "Messaging/Echo", b"[]", False),  # the regional relay: not needed
    ("POST", JMS + "Registration/ListIdentities", b"[]", False),
    ("POST", JMS + "Messaging/SendMessage", pblite(A["LIST_CONVERSATIONS"]), False),
    ("GET", "https://lh3.googleusercontent.com/a/ACg8ocK=s64", None, False),  # avatars: not needed
    ("POST", "https://ogads-pa.clients6.google.com/$rpc/google.internal.onegoogle.asyncdata.v1.AsyncDataService/"
             "GetAsyncData", b"[]", False),
    ("POST", "https://waa-pa.clients6.google.com/$rpc/google.internal.waa.v1.Waa/Create", b"[]", False),
    ("GET", "https://accounts.google.com/RotateCookiesPage?og_pid=1", None, False),
    ("GET", "https://www.google.com/", None, False),
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


@pytest.mark.parametrize(("method", "url", "ok"), [
    ("GET", MEDIA, True),  # the app's attachment download
    ("POST", MEDIA, False),  # an upload, download or not
    ("PUT", MEDIA, False),
    ("OPTIONS", MEDIA, False),
    ("GET", MEDIA + "?upload_id=1", False),  # exactly the download the app makes: no query
    ("GET", MEDIA + "/x", False),
    ("GET", MEDIA.replace("https:", "http:"), False),
    ("GET", "https://instantmessaging-pa.clients6.google.com/upload", False),
    ("GET", "https://instantmessaging-pa-jms-us.clients6.google.com/upload", False),
    ("GET", "https://lh3.googleusercontent.com/a/ACg8ocK=s64", False),  # download opens nothing else
    ("GET", "https://api.tenor.com/v1/gifs", False),
])
def test_the_media_download_passes_only_with_download(method, url, ok):
    body = b"x" if method in ("POST", "PUT") else None
    assert request_allowed(method, url, body, download=True) is ok
    assert not request_allowed(method, url, body)  # never without it
    request = SimpleNamespace(method=method, url=url, post_data_buffer=body)
    assert POLICY.allowed(request, guard.Guard(download=True)) is ok
    assert not POLICY.allowed(request, guard.Guard(send=True))  # send isn't download


def test_download_lets_no_write_out():
    for action in ("SEND_MESSAGE", "MESSAGE_READ", "TYPING_UPDATES", "DELETE_MESSAGE", "DOWNLOAD_MESSAGE"):
        assert not request_allowed("POST", SEND, pblite(A[action]), download=True)
    assert not request_allowed("POST", SEND, pblite(3), scope=SendScope(), download=True)


@pytest.mark.parametrize(("url", "body"), [
    (SEND_GOOGLE, pblite(A["SEND_MESSAGE"])),
    (SEND_GOOGLE, binary(A["SEND_MESSAGE"])),
    (SEND_GOOGLE, pblite(A["MESSAGE_READ"])),
    (SEND_GOOGLE, pblite(A["TYPING_UPDATES"])),
    (SEND_GOOGLE, pblite(A["SEND_REACTION"])),
    (SEND_GOOGLE, pblite(A["CREATE_GAIA_PAIRING_CLIENT_INIT"])),
    (SEND_GOOGLE, pblite(A["CREATE_GAIA_PAIRING_CLIENT_FINISHED"])),
    (SEND_GOOGLE, pblite(A["UNPAIR_GAIA_PAIRING"])),
    (RPC_GOOGLE + "Pairing/RegisterPhoneRelay", b"[]"),
    (RPC_GOOGLE + "Pairing/RevokeRelayPairing", b"[]"),
    ("https://instantmessaging-pa.clients6.google.com/upload", b"x"),
])
def test_writes_stay_blocked_on_the_host_the_app_signs_in_on(url, body):
    assert not request_allowed("POST", url, body)
    assert not request_allowed("POST", url, body, scope=SendScope())  # an unarmed scope opens nothing


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


def test_image_parts_number_every_image_and_follow_the_limit():
    raw = [{"text": "hi"}, {"image": True, "images": 2}, {"text": ""}, {"image": True}, {"text": "bye"}]
    assert parse.image_parts(raw) == [(1, 0, 1), (1, 1, 2), (3, 0, 3)]
    assert parse.image_parts(raw, 2) == [(3, 0, 3)]  # the last 2 rows: n stays the conversation's
    assert parse.image_parts(raw, 1) == []
    assert parse.messages(raw, {1: ["/d/a.jpg", "/d/b.jpg"]}) == [
        {"direction": "received", "text": "hi", "timestamp": "", "images": []},
        {"direction": "received", "text": "[image]", "timestamp": "", "images": ["/d/a.jpg", "/d/b.jpg"]},
        {"direction": "received", "text": "[image]", "timestamp": "", "images": []},
        {"direction": "received", "text": "bye", "timestamp": "", "images": []}]
    assert "images" not in parse.messages(raw)[0]


@pytest.mark.parametrize(("data", "ext"), [(JPEG, ".jpg"), (b"\x89PNG\r\n\x1a\n....", ".png"), (b"GIF89a", ".gif"),
                                           (b"RIFF\0\0\0\0WEBPVP8 ", ".webp"), (b"<html>", ".bin"), (b"", ".bin")])
def test_image_ext_reads_the_bytes(data, ext):
    assert parse.image_ext(data) == ext


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


def test_messages_open_elsewhere_stops_without_clicking_use_here(web):
    site = web(MessagesWeb(use_here=True))
    with pytest.raises(ProfileBusy, match="open in another window or tab") as caught:
        asyncio.run(fetch.conversations())
    assert caught.value.error == "messages_open_elsewhere"
    assert site.use_here_clicks == 0 and ("GET_UPDATES", True) not in site.wire


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


def test_read_without_image_out_lets_no_attachment_download_out(web):
    site = web(MessagesWeb())
    found = asyncio.run(fetch.messages("alice"))
    assert site.page.options == {"download": False} and site.media is False
    assert "GET instantmessaging-pa.googleapis.com/upload" in guard.state.blocked
    assert all("images" not in m for m in found["messages"]) and "imagesMissing" not in found
    assert site.page.keys == ["Escape"]  # no lightbox opened: only the Details dialog, closed again


def test_read_image_out_saves_each_image_full_size_and_stays_read_only(web, tmp_path):
    site = web(MessagesWeb())
    found = asyncio.run(fetch.messages("alice", image_dir=str(tmp_path)))
    assert site.page.options == {"download": True} and site.media is True
    path = tmp_path / "101-1.jpg"
    assert path.read_bytes() == JPEG + b"full-2-0"  # the lightbox's blob, not the thumbnail's
    assert [m["images"] for m in found["messages"]] == [[], [], [str(path.resolve())], []]
    assert found["imagesMissing"] == 0 and site.viewing is None and site.page.keys == ["Escape"] * 2
    assert ("MESSAGE_READ", False) in site.wire  # still unread
    assert [action for action, ok in site.wire if ok] == ["LIST_CONVERSATIONS", "LIST_MESSAGES"]


def test_read_image_out_saves_only_the_limited_rows_and_every_image_of_a_bubble(web, tmp_path):
    site = web(MessagesWeb())
    site.bubbles["101"][2] = {**site.bubbles["101"][2], "images": 2}
    assert [m["images"] for m in asyncio.run(fetch.messages("101", 1, image_dir=str(tmp_path)))["messages"]] == [[]]
    assert saved_files(tmp_path) == []
    rows = asyncio.run(fetch.messages("101", 2, image_dir=str(tmp_path)))["messages"]
    assert rows[0]["images"] == [str((tmp_path / "101-1.jpg").resolve()), str((tmp_path / "101-2.jpg").resolve())]
    assert (tmp_path / "101-2.jpg").read_bytes() == JPEG + b"full-2-1"


def test_a_lightbox_that_wont_close_is_never_read_for_the_next_image(web, tmp_path):
    site = web(MessagesWeb(sticky=True))
    site.bubbles["101"][2] = {**site.bubbles["101"][2], "images": 2}
    found = asyncio.run(fetch.messages("alice", image_dir=str(tmp_path)))
    assert (tmp_path / "101-1.jpg").read_bytes() == JPEG + b"full-2-0"
    assert not (tmp_path / "101-2.jpg").exists() and found["imagesMissing"] == 1  # never the first one's blob again
    assert site.viewing == (2, 0)


def test_a_lightbox_still_loading_counts_as_open(web, tmp_path):
    site = web(MessagesWeb(sticky=True, full_size=False))  # open with no image yet, and it won't close
    site.bubbles["101"][2] = {**site.bubbles["101"][2], "images": 2}
    found = asyncio.run(fetch.messages("alice", image_dir=str(tmp_path)))
    assert (tmp_path / "101-1.jpg").read_bytes() == JPEG + b"thumb-2-0"
    assert not (tmp_path / "101-2.jpg").exists() and found["imagesMissing"] == 1


def test_a_lightbox_that_never_loads_falls_back_to_the_thumbnail(web, tmp_path):
    web(MessagesWeb(full_size=False))
    found = asyncio.run(fetch.messages("alice", image_dir=str(tmp_path)))
    assert (tmp_path / "101-1.jpg").read_bytes() == JPEG + b"thumb-2-0" and found["imagesMissing"] == 0


def test_an_image_that_never_loads_is_counted_missing(web, tmp_path, monkeypatch):
    site = web(MessagesWeb())
    monkeypatch.setattr(site, "relay_raw", lambda method, url, body: url != MEDIA and MessagesWeb.relay_raw(
        site, method, url, body))  # the attachment download fails
    found = asyncio.run(fetch.messages("alice", image_dir=str(tmp_path)))
    assert found["imagesMissing"] == 1 and saved_files(tmp_path) == []
    assert [m["images"] for m in found["messages"]] == [[], [], [], []]


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


@pytest.mark.parametrize("title_checked", [True, False])
def test_a_verified_send_exits_0_whether_or_not_a_title_was_checked(calls, title_checked):
    _, answers = calls
    answers["send"] = lambda *a, confirm, headed: {**send_answer(*a, confirm=confirm, headed=headed),
                                                   "titleChecked": title_checked}
    result = run("send", "--to", "Alice", "--text", "hi", "--confirm", "--format", "json")
    assert result.exit_code == 0 and result.stderr == ""
    assert json.loads(result.stdout)["confirmed"] is True


@pytest.mark.parametrize("title_checked", [True, False])
def test_a_sent_but_unverified_send_exits_8_and_says_check_not_resend(calls, title_checked):
    _, answers = calls
    answers["send"] = lambda *a, confirm, headed: {**send_answer(*a, confirm=confirm, headed=headed),
                                                   "confirmed": False, "titleChecked": title_checked}
    result = run("send", "--to", "Alice", "--text", "hi", "--yes", "--format", "json")
    assert result.exit_code == 8
    out = json.loads(result.stdout)
    assert (out["sent"], out["confirmed"], out["titleChecked"]) == (True, False, title_checked)
    err = json.loads(result.stderr)
    assert (err["error"], err["code"]) == ("sent_unverified", 8)
    assert err["message"] == ("The text was sent, but it couldn't be verified: conversation 101 didn't show it "
                              "within 15s.")
    assert "webcli messages read 101" in err["hint"] and "instead of sending it again" in err["hint"]


@pytest.mark.parametrize("why", ["REFUSED: the compose box doesn't hold exactly the text. Nothing was sent.",
                                 "the click sent nothing within 10s. Nothing was sent."])
def test_a_refused_or_failed_send_exits_5_with_nothing_on_stdout(calls, why):
    seen, answers = calls

    def refuse(*args, **kwargs):
        raise RuntimeError(why)

    answers["send"] = refuse
    result = run("send", "--to", "Alice", "--text", "hi", "--confirm", "--format", "json")
    assert result.exit_code == 5 and result.stdout == ""
    assert json.loads(result.stderr) == {"error": why, "code": 5}
    assert len(seen) == 1  # one call, never retried


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


def test_read_image_out_passes_an_absolute_created_dir_and_prints_images(calls, tmp_path, monkeypatch):
    seen, answers = calls
    monkeypatch.chdir(tmp_path)

    def answer(query, limit, *, image_dir, headed):
        rows = parse.messages(BUBBLES["101"], {2: [f"{image_dir}/101-1.jpg"]})
        return {"conversation": parse.conversations(ROWS)[0], "messages": rows[-limit:] if limit else rows,
                "imagesMissing": 1}

    answers["messages"] = answer
    result = run("read", "Alice", "--limit", "2", "--image-out", "pics", "--format", "json")
    assert result.exit_code == 0, result.output
    out_dir = str((tmp_path / "pics").resolve())
    assert (tmp_path / "pics").is_dir() and seen[-1][3] == {"image_dir": out_dir, "headed": False}
    assert json.loads(result.stdout)[0] == {"direction": "received", "text": "[image]", "timestamp": "2:13 PM",
                                            "images": [f"{out_dir}/101-1.jpg"]}
    assert "1 image(s) didn't load" in result.stderr  # a warning, even in JSON mode
    assert run("read", "Alice", "--csv", "--limit", "2", "--image-out", "pics").stdout.splitlines()[0] == (
        "direction,text,timestamp,images")


def test_search_exits_3_on_no_match(calls):
    seen, answers = calls
    answers["search"] = lambda query, pages, headed: {
        "results": parse.search_deep(parse.conversations(ROWS), query), "scanned": 4, "complete": True}
    result = run("search", "gate code", "--format", "json")
    hits = json.loads(result.stdout)
    assert [(r["kind"], r["id"], r["matched"]) for r in hits] == [("conversation", "103", ["preview"])]
    assert seen[-1][1:] == ("search", ("gate code", None), {"headed": False})
    assert run("search", "zebra").exit_code == 3
    run("search", "x", "--pages", "3")
    assert seen[-1][2] == ("x", 3)


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
