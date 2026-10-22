"""webcli whatsapp: the WhatsApp Web site, offline. Parsers run on hand-built HTML fixtures in WhatsApp Web's shape
(tests/fixtures/whatsapp); fetch runs against a scripted fake page (no browser); the CLI against a fake daemon."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from webcli.apps import webcli as webcli_app
from webcli.core import browser, daemon
from webcli.core.errors import AuthRequired, NotFound, UsageError
from webcli.sites import BUILTIN
from webcli.sites.whatsapp import ADAPTER, dom, fetch, parse
from webcli.sites.whatsapp.endpoints import MEDIA, SELECTORS
from webcli.sites.whatsapp.guard import POLICY, request_allowed, socket_allowed

from conftest import make_profile

FIXTURES = Path(__file__).parent / "fixtures" / "whatsapp"
TODAY = date(2026, 10, 22)  # a Thursday


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


# --- The site -------------------------------------------------------------------------------------------------------

def test_the_site_is_registered_as_a_data_site():
    assert BUILTIN["whatsapp"] == "webcli.sites.whatsapp:ADAPTER"
    assert (ADAPTER.name, ADAPTER.kind, ADAPTER.status_op) == ("whatsapp", "data", "status")
    assert set(ADAPTER.ops) == {"status", "chats", "messages", "search", "send"}


def test_every_selector_compiles():
    for value in SELECTORS.values():
        if isinstance(value, str):
            dom.alternatives(value)
            dom.select(dom.build("<div></div>"), value)
    for selector, _word in MEDIA:
        dom.select(dom.build("<div></div>"), selector)


# --- The guard ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("method", "url", "ok"), [
    ("GET", "https://web.whatsapp.com/", True),
    ("GET", "https://static.whatsapp.net/rsrc.php/v4/app.js", True),
    ("HEAD", "https://web.whatsapp.com/favicon.ico", True),
    ("POST", "https://web.whatsapp.com/", False),
    ("GET", "http://web.whatsapp.com/", False),
    ("GET", "https://evil.example/", False),
    ("GET", "https://web.whatsapp.com.evil.example/", False),
])
def test_the_request_policy(method, url, ok):
    assert request_allowed(method, url) is ok


@pytest.mark.parametrize(("url", "ok"), [("wss://web.whatsapp.com/ws/chat", True), ("wss://web.whatsapp.com/ws", True),
                                         ("ws://web.whatsapp.com/ws/chat", False), ("wss://evil.example/ws", False),
                                         ("wss://web.whatsapp.com/other", False)])
def test_only_whatsapps_own_socket_is_let_open(url, ok):
    assert socket_allowed(url) is ok
    assert POLICY.site == "whatsapp"


# --- Parsing --------------------------------------------------------------------------------------------------------

def test_the_chat_list_rows():
    rows = parse.chats(fixture("chat_list.html"))
    assert [r["name"] for r in rows] == ["Family Group", "Alice Smith", "Alice Smith (work)", "Sam Jones", "Sam Lee",
                                         "+1 (555) 010-2000"]  # the Archived button is no row; bidi marks dropped
    assert rows[0] == {"name": "Family Group", "preview": "Mom: Dinner at 7? 🍝", "time": "10:41 AM", "unread": 3,
                       "pinned": True, "muted": False}
    assert rows[1]["preview"] == "See you at 6" and rows[1]["unread"] == 0
    assert rows[3]["muted"] is True and rows[3]["time"] == "Monday"
    assert rows[4]["unread"] == 1  # "Marked as unread" counts as one
    assert rows[5]["time"] == "10/1/2026"


def test_search_results_carry_their_section():
    rows = parse.search(fixture("search.html"))
    assert [(r["type"], r["name"]) for r in rows] == [("chat", "Sam Jones"), ("chat", "Sam Lee"),
                                                      ("contact", "Samantha Park"), ("message", "Alice Smith")]
    assert rows[3]["preview"] == "ask Sam about the gate code"


def test_the_thread():
    found = parse.thread(fixture("thread.html"), today=TODAY)
    assert found["header"] == "Family Group"
    rows = found["messages"]
    assert [(r["sender"], r["text"]) for r in rows] == [
        ("Mom", "Dinner at 7? 🍝"), ("You", "Yes! 🎉"), ("Dad", "[image] The new porch"), ("Dad", "[image]"),
        ("Mom", "[voice note]"), ("Dad", "[document] Q3-plan.pdf"), ("Mom", "[deleted]"),
        ("You", "Groceries:\nmilk\neggs"), ("You", "On my way")]  # notices and banners are no messages
    assert rows[0]["datetime"] == "2026-10-21T18:58" and rows[0]["direction"] == "received"
    assert rows[1]["quoted"] == {"sender": "Mom", "text": "Dinner at 7? 🍝"}
    assert [r["status"] for r in rows if r["direction"] == "sent"] == ["read", "delivered", "pending"]
    assert rows[3]["datetime"] == "2026-10-22T09:02"  # no pre-plain-text: its day is the TODAY separator
    assert rows[4]["media"] == "voice note" and rows[4]["status"] is None


@pytest.mark.parametrize(("label", "found"), [
    ("TODAY", date(2026, 10, 22)), ("Yesterday", date(2026, 10, 21)), ("Monday", date(2026, 10, 19)),
    ("THURSDAY", date(2026, 10, 15)), ("10/1/2026", date(2026, 10, 1)), ("22/10/2026", date(2026, 10, 22)),
    ("1.10.2026", date(2026, 10, 1)), ("2026-10-02", date(2026, 10, 2)), ("October 3, 2026", date(2026, 10, 3)),
    ("2 UNREAD MESSAGES", None), ("", None)])
def test_dates_as_whatsapp_shows_them(label, found):
    assert parse.parse_date(label, TODAY) == found


def test_since():
    now = datetime(2026, 10, 22, 12, 0)
    assert parse.parse_since("2026-10-20", now) == datetime(2026, 10, 20)
    assert parse.parse_since("48h", now) == datetime(2026, 10, 20, 12, 0)
    assert parse.parse_since("1w", now) == datetime(2026, 10, 15, 12, 0)
    with pytest.raises(ValueError):
        parse.parse_since("last tuesday", now)
    rows = parse.thread(fixture("thread.html"), today=TODAY)["messages"]
    assert [r["text"] for r in parse.since_filter(rows, datetime(2026, 10, 22, 10, 0))] == [
        "Groceries:\nmilk\neggs", "On my way"]


@pytest.mark.parametrize(("snap", "kind"), [
    ({"list": True, "rows": 5}, "linked"), ({"qr": True}, "unlinked"), ({"loggedOut": True}, "unlinked"),
    ({"block": "too many attempts"}, "blocked"), ({"useHere": True}, "use_here"), ({}, "loading"),
    ({"list": True, "loading": True}, "loading")])
def test_the_app_state(snap, kind):
    assert parse.state(snap) == kind


# --- Which chat a name means ----------------------------------------------------------------------------------------

def test_an_exact_name_wins_over_names_containing_it():
    rows = parse.chats(fixture("chat_list.html"))
    assert parse.resolve(rows, "Alice Smith")["name"] == "Alice Smith"
    assert parse.resolve(rows, "alice smith (WORK)")["name"] == "Alice Smith (work)"
    assert parse.resolve(rows, "Family")["name"] == "Family Group"
    assert parse.resolve(rows, "nobody") is None


def test_an_ambiguous_name_lists_the_candidates_and_opens_nothing():
    rows = parse.chats(fixture("chat_list.html"))
    with pytest.raises(UsageError, match=r"2 chats match 'Sam': Sam Jones; Sam Lee\. Nothing was opened\."):
        parse.resolve(rows, "Sam")
    with pytest.raises(UsageError, match="2 chats match 'alice'"):
        parse.resolve(rows, "alice")


def test_names_are_compared_exactly():
    assert parse.names_equal("Alice  Smith", "Alice Smith")
    assert parse.names_equal("\u202a+1 (555) 010-2000\u202c", "+1 (555) 010-2000")
    assert not parse.names_equal("alice smith", "Alice Smith")
    assert not parse.names_equal("Alice Smith", "Alice Smith (work)")


def test_message_text():
    assert parse.message_text("  hi\r\nthere ") == "hi\nthere"
    for bad in ("", "   ", "x" * 4001):
        with pytest.raises(ValueError):
            parse.message_text(bad)


# --- fetch, against a scripted page ---------------------------------------------------------------------------------

NEW = "<!--NEW-->"


def outgoing(text: str, icon: str | None = "msg-time") -> str:
    tick = f'<span data-icon="{icon}"></span>' if icon else ""
    body = "<br>".join(text.split("\n"))
    return (f'<div role="row"><div data-id="true_x@g.us_NEW"><div class="message-out">'
            f'<div class="copyable-text" data-pre-plain-text="[11:00 AM, 10/22/2026] Kevin K: ">'
            f'<span class="selectable-text copyable-text"><span>{body}</span></span></div>'
            f'<span dir="auto">11:00 AM</span>{tick}</div></div></div>{NEW}')


def thread_for(name: str) -> str:
    return fixture("thread.html").replace(">Family Group<", f">{name}<")


class Loc:
    def __init__(self, page: "WhatsAppPage", sel: str):
        self.page, self.sel = page, sel

    @property
    def first(self) -> "Loc":
        return self

    def kind(self) -> str | None:
        p, s = self.page, self.sel
        if s == fetch.TARGET:
            return "target" if p.marked else None
        if "footer" in s and "contenteditable" in s:
            return "compose" if p.open and p.has_compose else None
        if "footer" in s and ("Send" in s or "send" in s):
            return "send" if p.open and p.has_send else None
        if s.startswith("#side") or "Search" in s:
            return "search"
        return None

    async def count(self) -> int:
        return 1 if self.kind() else 0

    async def is_visible(self) -> bool:
        return bool(self.kind())

    async def click(self, **_options) -> None:
        kind = self.kind()
        self.page.actions.append(("click", kind))
        self.page.focus = kind
        if kind == "target":
            self.page.open = self.page.opens_as.get(self.page.marked, self.page.marked)
            self.page.main = thread_for(self.page.open)
        elif kind == "send":
            self.page.press_send()

    async def inner_text(self) -> str:
        return self.page.compose if self.kind() == "compose" else ""


class Keyboard:
    def __init__(self, page: "WhatsAppPage"):
        self.page = page

    async def type(self, text: str, delay: int = 0) -> None:
        p = self.page
        p.actions.append(("type", p.focus, text))
        if p.focus == "search":
            p.searching = text
        elif p.focus == "compose":
            p.compose += text

    async def insert_text(self, text: str) -> None:
        p = self.page
        p.actions.append(("insert", p.focus, text))
        if p.focus == "compose":
            p.compose += text
            if p.switch_on_type:
                p.main = thread_for(p.switch_on_type)

    async def press(self, key: str) -> None:
        p = self.page
        p.actions.append(("press", p.focus, key))
        assert key != "Enter", "Enter would send"
        if p.focus == "compose":
            if key == "Shift+Enter":
                p.compose += "\n"
            elif key == "Backspace":
                p.compose = ""


class WhatsAppPage:
    """WhatsApp Web as fetch sees it: evaluate() answers the tagged scripts from the fixtures; locators and the
    keyboard record every click and keystroke."""

    url = "https://web.whatsapp.com/"

    def __init__(self, *, snap=None, search_html=None, opens_as=None, has_compose=True, has_send=True,
                 draft="", delivers=True, tick="msg-time", switch_on_type=None):
        self.snap = snap if snap is not None else {"list": True, "rows": 6, "main": False}
        self.list_html = fixture("chat_list.html")
        self.search_html = search_html if search_html is not None else fixture("search.html")
        self.opens_as = opens_as or {}
        self.has_compose, self.has_send, self.compose = has_compose, has_send, draft
        self.delivers, self.tick, self.switch_on_type = delivers, tick, switch_on_type
        self.actions: list[tuple] = []
        self.searching = None
        self.marked = None
        self.open = None
        self.main = None
        self.focus = None
        self.keyboard = Keyboard(self)
        self.policy = None

    async def goto(self, url, **_options):
        self.actions.append(("goto", url))

    async def wait_for_timeout(self, _ms):
        return None

    def locator(self, sel: str) -> Loc:
        return Loc(self, sel)

    def pane(self) -> str:
        return self.search_html if self.searching else self.list_html

    async def evaluate(self, js: str, arg=None):
        if "/* wa:state */" in js:
            return dict(self.snap)
        if "/* wa:html */" in js:
            if "#pane-side" in arg:
                return self.pane()
            if "#main" in arg:
                return self.main
            return None
        if "/* wa:scroll-list */" in js or "/* wa:scroll-up */" in js or "/* wa:use-here */" in js:
            self.actions.append(("scroll" if "scroll" in js else "use-here",))
            return False
        if "/* wa:mark */" in js:
            name = arg[2]
            rows = ([r for r in parse.search(self.pane()) if r["type"] == "chat"] if self.searching
                    else parse.chats(self.pane()))
            hits = [r for r in rows if parse.names_equal(r["name"], name)]
            self.marked = name if len(hits) == 1 else None
            return len(hits)
        raise AssertionError(f"unexpected script: {js[:40]}")

    def press_send(self) -> None:
        if self.delivers and self.compose:
            self.main = self.main.replace(NEW, outgoing(self.compose, self.tick))
            self.compose = ""

    def writes(self) -> list[tuple]:
        """Every keystroke and click outside the search box: what a send would do."""
        return [a for a in self.actions if a[0] in ("insert", "press") or (a[0] == "type" and a[1] != "search")
                or (a[0] == "click" and a[1] in ("compose", "send"))]


@pytest.fixture
def web(monkeypatch):
    def install(page: WhatsAppPage) -> WhatsAppPage:
        @asynccontextmanager
        async def site_page(policy, **options):
            page.policy = policy
            yield page

        monkeypatch.setattr(browser, "site_page", site_page)
        return page

    return install


def test_status(web):
    web(WhatsAppPage())
    assert asyncio.run(fetch.status()) == {"signedIn": True, "linked": True, "status": "active"}
    web(WhatsAppPage(snap={"qr": True}))
    assert asyncio.run(fetch.status()) == {"signedIn": False, "linked": False, "status": "needs_link"}


def test_a_block_page_stops_the_command_and_clicks_nothing(web):
    page = web(WhatsAppPage(snap={"block": "too many attempts"}))
    with pytest.raises(RuntimeError, match=r"^STOPPED: .*too many attempts"):
        asyncio.run(fetch.chats())
    assert [a for a in page.actions if a[0] != "goto"] == []


def test_not_linked_is_auth_required(web):
    web(WhatsAppPage(snap={"qr": True}))
    with pytest.raises(AuthRequired):
        asyncio.run(fetch.chats())


def test_chats_read_the_list_behind_the_guard(web):
    page = web(WhatsAppPage())
    rows = asyncio.run(fetch.chats(3))
    assert [r["name"] for r in rows] == ["Family Group", "Alice Smith", "Alice Smith (work)"]
    assert page.policy is POLICY and page.writes() == []
    assert [r["name"] for r in asyncio.run(fetch.chats(unread=True))] == ["Family Group", "Sam Lee"]


def test_search_types_only_into_the_search_box(web):
    page = web(WhatsAppPage())
    rows = asyncio.run(fetch.search("sam", 2))
    assert [r["name"] for r in rows] == ["Sam Jones", "Sam Lee"]
    assert ("type", "search", "sam") in page.actions and page.writes() == [] and page.open is None


def test_read_opens_the_exact_chat_and_reads_its_messages(web):
    page = web(WhatsAppPage())
    found = asyncio.run(fetch.messages("Alice Smith", 3))
    assert found["chat"] == "Alice Smith" and page.open == "Alice Smith"
    assert [m["text"] for m in found["messages"]] == ["[deleted]", "Groceries:\nmilk\neggs", "On my way"]
    assert page.searching is None and page.writes() == []


def test_read_since(web):
    web(WhatsAppPage())
    found = asyncio.run(fetch.messages("Alice Smith", 20, "2026-10-22T10:00"))
    assert [m["text"] for m in found["messages"]] == ["Groceries:\nmilk\neggs", "On my way"]


def test_read_falls_back_to_search_and_refuses_an_ambiguous_name(web):
    page = web(WhatsAppPage())
    with pytest.raises(UsageError, match="Sam Jones; Sam Lee"):
        asyncio.run(fetch.messages("Sam"))
    assert page.open is None and page.marked is None
    page = web(WhatsAppPage())
    assert asyncio.run(fetch.messages("sam lee", 1))["chat"] == "Sam Lee"


def test_a_contact_without_a_chat_is_never_opened(web):
    page = web(WhatsAppPage())
    with pytest.raises(NotFound):
        asyncio.run(fetch.messages("Samantha"))
    assert page.open is None


def test_a_send_dry_run_resolves_and_never_opens_types_or_clicks(web):
    page = web(WhatsAppPage())
    out = asyncio.run(fetch.send("Alice Smith", "  On my way  ", "Alice Smith"))
    assert out == {"dryRun": True, "chat": "Alice Smith", "expectChat": "Alice Smith", "expectChatMatches": True,
                   "text": "On my way"}
    assert page.writes() == [] and page.open is None and page.marked is None
    assert not [a for a in page.actions if a[0] == "click"]


def test_a_dry_run_reports_an_expect_chat_mismatch(web):
    page = web(WhatsAppPage())
    out = asyncio.run(fetch.send("Alice Smith", "hi", "Alice Smith (work)"))
    assert out["expectChatMatches"] is False and out["dryRun"] is True
    assert page.writes() == [] and page.open is None


@pytest.mark.parametrize("expect", ["Alice Smith (work)", "alice smith", "Alice Smit"])
def test_a_confirmed_send_refuses_an_expect_chat_mismatch_before_opening(web, expect):
    page = web(WhatsAppPage())
    with pytest.raises(RuntimeError, match=r"^REFUSED: .*Nothing was sent\.$"):
        asyncio.run(fetch.send("Alice Smith", "hi", expect, confirm=True))
    assert page.open is None and page.writes() == []


def test_a_confirmed_send_refuses_when_the_header_isnt_the_expected_chat(web):
    page = web(WhatsAppPage(opens_as={"Alice Smith": "Alice Smith (work)"}))
    with pytest.raises(RuntimeError, match="didn't open"):
        asyncio.run(fetch.send("Alice Smith", "hi", "Alice Smith", confirm=True))
    assert page.writes() == []


def test_a_confirmed_send_types_once_clicks_send_once_and_sees_the_tick(web):
    page = web(WhatsAppPage())
    out = asyncio.run(fetch.send("Alice Smith", "See you\nsoon", "Alice Smith", confirm=True))
    assert out == {"dryRun": False, "chat": "Alice Smith", "expectChat": "Alice Smith", "expectChatMatches": True,
                   "text": "See you\nsoon", "sent": True, "confirmed": True, "status": "pending"}
    assert page.writes() == [("click", "compose"), ("insert", "compose", "See you"),
                             ("press", "compose", "Shift+Enter"), ("insert", "compose", "soon"), ("click", "send")]


def test_a_send_that_never_shows_is_sent_but_unconfirmed(web):
    page = web(WhatsAppPage(tick=None))
    out = asyncio.run(fetch.send("Alice Smith", "hi", "Alice Smith", confirm=True))
    assert (out["sent"], out["confirmed"], out["status"]) == (True, False, None)
    assert [a for a in page.writes() if a[0] == "click"].count(("click", "send")) == 1


def test_a_click_that_sends_nothing_says_nothing_was_sent(web):
    page = web(WhatsAppPage(delivers=False))
    with pytest.raises(RuntimeError, match=r"sent nothing.*Nothing was sent\."):
        asyncio.run(fetch.send("Alice Smith", "hi", "Alice Smith", confirm=True))
    assert page.compose == "" and page.writes().count(("click", "send")) == 1


@pytest.mark.parametrize(("options", "why"), [({"draft": "half a thought"}, "unsent draft"),
                                              ({"has_compose": False}, "no compose box"),
                                              ({"has_send": False}, "no Send button"),
                                              ({"switch_on_type": "Sam Lee"}, "no longer")])
def test_a_confirmed_send_refuses_on_a_page_check(web, options, why):
    page = web(WhatsAppPage(**options))
    with pytest.raises(RuntimeError, match=rf"^REFUSED: .*{why}.*Nothing was sent\."):
        asyncio.run(fetch.send("Alice Smith", "hi", "Alice Smith", confirm=True))
    assert ("click", "send") not in page.actions
    if "draft" in options:
        assert page.compose == "half a thought"  # a draft is left alone


def test_send_never_opens_a_new_contact(web):
    page = web(WhatsAppPage())
    with pytest.raises(NotFound):
        asyncio.run(fetch.send("Samantha Park", "hi", "Samantha Park", confirm=True))
    assert page.open is None and page.writes() == []


# --- The CLI --------------------------------------------------------------------------------------------------------

@pytest.fixture
def calls(monkeypatch):
    make_profile("whatsapp:default")
    seen, answers = [], {}

    async def call(ident, site, op, *args, **kwargs):
        seen.append((ident.id, op, args, kwargs))
        value = answers[op]
        return value(*args, **kwargs) if callable(value) else value

    monkeypatch.setattr(daemon, "call", call)
    return seen, answers


def run(*args):
    return CliRunner().invoke(webcli_app.build(), ["whatsapp", *args])


def send_answer(query, text, expect, *, confirm, headed):
    out = {"dryRun": not confirm, "chat": "Alice Smith", "expectChat": expect,
           "expectChatMatches": expect == "Alice Smith", "text": text}
    return {**out, "sent": True, "confirmed": True, "status": "sent"} if confirm else out


@pytest.mark.parametrize(("flags", "confirm"), [([], False), (["--dry-run"], False), (["--confirm", "--dry-run"], False),
                                                (["--confirm"], True), (["--yes"], True)])
def test_send_is_a_dry_run_unless_confirm_alone(calls, flags, confirm):
    seen, answers = calls
    answers["send"] = send_answer
    result = run("send", "Alice", "--text", " hi ", "--expect-chat", "Alice Smith", *flags, "--format", "json")
    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert out["dryRun"] is (not confirm) and out.get("sent", False) is confirm
    assert seen == [("whatsapp:default", "send", ("Alice", "hi", "Alice Smith"), {"confirm": confirm, "headed": False})]


def test_a_dry_run_whose_chat_isnt_expect_chat_prints_it_and_exits_5(calls):
    _, answers = calls
    answers["send"] = send_answer
    result = run("send", "Alice", "--text", "hi", "--expect-chat", "Alice Smyth", "--format", "json")
    assert result.exit_code == 5
    assert json.loads(result.stdout)["expectChatMatches"] is False
    assert "REFUSED (dry run)" in json.loads(result.stderr)["error"]


def test_a_refused_send_exits_5_with_nothing_on_stdout_and_is_not_retried(calls):
    seen, answers = calls

    def refuse(*args, **kwargs):
        raise RuntimeError("REFUSED: 'Alice' is the chat 'Alice Smith', not --expect-chat 'Alice'. Nothing was sent.")

    answers["send"] = refuse
    result = run("send", "Alice", "--text", "hi", "--expect-chat", "Alice", "--confirm", "--format", "json")
    assert result.exit_code == 5 and result.stdout == ""
    assert len(seen) == 1


def test_an_unverified_send_exits_8_and_says_check_not_resend(calls):
    _, answers = calls
    answers["send"] = lambda *a, confirm, headed: {**send_answer(*a, confirm=confirm, headed=headed),
                                                   "confirmed": False, "status": None}
    result = run("send", "Alice", "--text", "hi", "--expect-chat", "Alice Smith", "--yes", "--format", "json")
    assert result.exit_code == 8
    err = json.loads(result.stderr)
    assert err["error"] == "sent_unverified" and "instead of sending it again" in err["hint"]


@pytest.mark.parametrize("args", [("send", "Alice", "--text", "hi"),
                                  ("send", "Alice", "--text", "   ", "--expect-chat", "Alice"),
                                  ("send", " ", "--text", "hi", "--expect-chat", "Alice"),
                                  ("send", "Alice", "--text", "hi", "--expect-chat", " "),
                                  ("read", "  "), ("search", ""), ("read", "Alice", "--since", "someday"),
                                  ("chats", "--limit", "0"), ("chats", "--limit", "201")])
def test_bad_input_is_a_usage_error_before_any_call(calls, args):
    seen, _ = calls
    assert run(*args).exit_code == 2 and seen == []


def test_an_ambiguous_chat_exits_2_with_the_candidates(calls):
    _, answers = calls

    def ambiguous(*args, **kwargs):
        raise UsageError("2 chats match 'Sam': Sam Jones; Sam Lee. Nothing was opened.")

    answers["messages"] = ambiguous
    result = run("read", "Sam", "--format", "json")
    assert result.exit_code == 2 and "Sam Jones; Sam Lee" in result.stderr


def test_read_chats_and_search_print_rows(calls):
    seen, answers = calls
    answers["messages"] = {"chat": "Alice Smith",
                           "messages": parse.thread(fixture("thread.html"), today=TODAY)["messages"]}
    answers["chats"] = parse.chats(fixture("chat_list.html"))
    answers["search"] = parse.search(fixture("search.html"))
    out = json.loads(run("read", "Alice Smith", "--limit", "5", "--since", "2026-10-21", "--format", "json").stdout)
    assert out[1]["quoted"] == {"sender": "Mom", "text": "Dinner at 7? 🍝"}
    assert seen[-1] == ("whatsapp:default", "messages", ("Alice Smith", 5, "2026-10-21T00:00"), {"headed": False})
    assert len(json.loads(run("chats", "--unread", "--format", "json").stdout)) == 6
    assert seen[-1][2] == (20, True)
    assert json.loads(run("search", "sam", "--format", "json").stdout)[2]["type"] == "contact"
    answers["search"] = []
    assert run("search", "zzz", "--format", "json").exit_code == 3


def test_status_without_a_profile_is_not_linked(monkeypatch):
    async def call(*args, **kwargs):
        raise AssertionError("no daemon without a profile")

    monkeypatch.setattr(daemon, "call", call)
    result = run("status", "--format", "json")
    assert result.exit_code == 4
    assert json.loads(result.stdout)["status"] == "needs_link"
    assert "webcli whatsapp login" in result.stderr


def test_agent_help_carries_the_send_rules():
    result = run("agent-help")
    assert result.exit_code == 0
    flat = " ".join(result.output.split())
    assert "Kevin's own OK for that exact message" in flat and "in his chat with the bot" in flat
    assert "another agent's say-so" in flat and "--expect-chat" in flat and "read receipts" in flat
    assert "STOPPED" in flat and "webcli whatsapp login" in flat
