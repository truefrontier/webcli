"""Offline tests for fetch.py: RPC status handling, paging, and the ask flow, with a fake page instead of Chrome."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeout

from webcli.core import guard
from webcli.core.errors import AuthRequired
from webcli.sites.gemini import fetch, parse, selectors as sel
from webcli.sites.gemini.guard import POLICY

FIX = Path(__file__).parent / "fixtures" / "gemini"
OK = 200
CHANNEL = "https://geminiweb-pa.clients6.google.com/v1/processSession"
NEW_CHAT = "https://gemini.google.com/app/7e6d5c4b3a291807"
ITEM = sel.MODEL_ITEM.split(", ")[0]  # the MODEL_ITEM alternative the fake menu matches


def body(name: str) -> str:
    return (FIX / name).read_text()


class Locator:
    def __init__(self, page, selector, index=None):
        self.page, self.selector, self.index = page, selector, index

    first = property(lambda self: self)

    def nth(self, index):
        self.page.events.append(("nth", self.selector, index))
        return Locator(self.page, self.selector, index)

    async def count(self):
        return self.page.counts.get(self.selector, 0)

    async def click(self, **kwargs):
        page = self.page
        page.events.append(("click", self.selector))
        if self.selector == sel.SEND_BUTTON:
            page.submit()
        elif self.selector == sel.MODEL_BUTTON:
            page.menu_open = page.dead_clicks == 0
            page.dead_clicks = max(page.dead_clicks - 1, 0)
        elif self.selector == sel.THINKING_ROW:
            page.submenu_open = page.menu_open
        elif self.selector == sel.CREATE_IMAGE_ITEM:  # the Create image tool turns on: its chip shows
            page.present.add(sel.IMAGE_TOOL_CHIP)
        elif self.selector == sel.CANVAS_ITEM:
            page.present.add(sel.CANVAS_TOOL_CHIP)
        elif self.selector == sel.THINKING_ITEM:  # a level row: both menus close
            page.menu_open = page.submenu_open = False
            if page.dead_levels:
                page.dead_levels -= 1
            else:
                page.thinking = fetch.THINKING_LEVELS[self.index]
        elif self.index is not None:  # a model row: the picker shows its short name ("3.5 Flash-Lite" -> "Flash-Lite")
            page.menu_open = False
            if page.dead_picks:
                page.dead_picks -= 1
            else:
                page.texts[sel.MODEL_BUTTON_LABEL] = page.menu[self.index]["name"].split()[-1]

    async def inner_text(self, **kwargs):
        return self.page.texts.get(self.selector, "")

    async def get_attribute(self, name, **kwargs):
        return None

    async def wait_for(self, **kwargs):
        if self.selector not in self.page.present:
            raise PlaywrightTimeout("Timeout 30000ms exceeded.")

    async def fill(self, text):
        self.page.texts[self.selector] = text
        self.page.events.append(("fill", text))

    async def press(self, key):
        self.page.events.append(("press", key))


class Request:
    def __init__(self, url, method="POST"):
        self.url, self.method = url, method


class Response:
    url = "https://gemini.google.com/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate?rt=c"
    status = 200
    request = Request(url)

    def __init__(self, text):
        self._text = text
        self.hang = False

    async def finished(self):
        if self.hang:
            await asyncio.sleep(3600)

    async def text(self):
        return self._text


class ApiResponse:
    def __init__(self, status, headers, data):
        self.status, self.headers, self._data = status, headers, data

    async def body(self):
        return self._data


class ApiRequest:
    """page.request: `routes` maps a URL to (status, headers, body), or to a list of them answered in turn."""

    def __init__(self, routes=None):
        self.routes, self.calls = routes or {}, []

    async def get(self, url, **kwargs):
        assert kwargs.get("max_redirects") == 0  # every hop goes back through image_url_allowed
        self.calls.append(url)
        answer = self.routes.get(url, (404, {"content-type": "text/html"}, b"<html>not found</html>"))
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        return ApiResponse(*answer)


class Keyboard:
    def __init__(self, page):
        self.page = page

    async def press(self, key):
        self.page.events.append(("key", key))
        if key == "Escape":
            self.page.menu_open = self.page.submenu_open = False

    async def insert_text(self, text):
        self.page.events.append(("insert", text))


class Context:
    def __init__(self):
        self.handlers = {}

    def on(self, event, handler):
        self.handlers[event] = handler


class FakePage:
    """`answers`: the send button's click streams this StreamGenerate body. `channel`: the click sends a
    WebChannel (processSession) POST instead. `url_after_send`: where the click leaves the page.
    `dead_clicks` / `dead_picks` / `dead_levels`: how many clicks on the model picker / a model row / a
    thinking level row do nothing. `thinking`: the selected thinking level; None means the model menu has
    no Thinking level row."""

    def __init__(self, rpcs=None, *, token=True, lands_on=None, answers=None, channel=False, url_after_send=None,
                 dom_text=None, menu=None, dom_images=0, dom_key=None, image_routes=None):
        self.rpcs = rpcs or {}  # (rpcid, payload json) -> (status, body)
        self.token, self.lands_on = token, lands_on
        self.answers, self.channel, self.url_after_send = answers, channel, url_after_send
        self.dom_text, self.menu = dom_text, menu or []
        self.dom_images, self.dom_key = dom_images, dom_key  # images the new response shows, and its content id
        self.request = ApiRequest(image_routes)
        self.notices_before = []
        self.notices_after = []
        self.sent = False
        self.url = "about:blank"
        self.calls: list[tuple] = []
        self.events: list[tuple] = []
        self.response = None
        self.old_answer = None
        self.dom_count = 1
        self.dom_canvases = 0
        self.dom_chips = []  # the new response's citation chips: {title, aria} each, a parse.CHIP in dom_text
        self.uploads = []
        self.button_ready = True
        self.menu_open = False
        self.dead_clicks = self.dead_picks = self.dead_levels = 0
        self.submenu_open = False
        self.thinking = "low"
        self.disabled_levels = set()
        self.counts = {}
        self.texts = {sel.MODEL_BUTTON_LABEL: "Fast"}
        self.present = {sel.PROMPT_INPUT, sel.MODEL_BUTTON}
        self.context = Context()
        self.keyboard = Keyboard(self)

    async def goto(self, target, **kwargs):
        self.url = self.lands_on or target

    def submit(self):
        """What the app does when the send button is clicked."""
        self.sent = True
        if self.url_after_send:
            self.url = self.url_after_send
        if self.answers:
            self.response = Response(self.answers)
            self.context.handlers["request"](Response.request)
            self.context.handlers["response"](self.response)
        if self.channel:
            self.context.handlers["request"](Request(CHANNEL + "?VER=8&RID=81906&CVER=22&zx=x&t=1"))

    def locator(self, selector):
        return Locator(self, selector)

    async def wait_for_selector(self, selector, **kwargs):
        if ((selector == sel.MODEL_ITEM and not self.menu_open)
                or (selector == sel.THINKING_ROW and not (self.menu_open and self.thinking))
                or (selector == sel.THINKING_ITEM and not self.submenu_open)):
            raise PlaywrightTimeout("Timeout 4000ms exceeded.")

    async def wait_for_function(self, *args, **kwargs):
        self.events.append(("ready",))
        if not self.button_ready:
            raise PlaywrightTimeout("Timeout 15000ms exceeded.")

    async def wait_for_timeout(self, ms):
        pass

    @asynccontextmanager
    async def expect_file_chooser(self, timeout):
        page = self

        class Chooser:
            async def set_files(self, files):
                page.events.append(("files", tuple(files)))

        class Pending:
            @property
            async def value(self):
                return Chooser()

        yield Pending()

    async def evaluate(self, js, arg=None):
        if js == fetch.NOTICES_JS:
            return self.notices_after if self.sent else self.notices_before
        if js == fetch.HAS_TOKEN_JS:
            return self.token
        if js == fetch.ACCOUNT_JS:
            return {"name": "Test Owner", "email": "owner@example.com"}
        if js == fetch.UPLOADS_JS:  # each state in turn, the last one from then on; default: all uploaded
            if self.uploads:
                return self.uploads.pop(0) if len(self.uploads) > 1 else self.uploads[0]
            return {"count": sum(len(e[1]) for e in self.events if e[0] == "files"), "busy": 0, "failed": 0}
        if js == fetch.LAST_RESPONSE_JS:
            if not self.sent:  # before sending: the chat's previous answer, if any
                return self.old_answer
            if self.dom_text is None and not self.dom_images:
                return None
            return {"count": self.dom_count, "text": self.dom_text or "", "busy": False, "images": self.dom_images,
                    "imagesPending": 0, "canvases": self.dom_canvases, "key": self.dom_key, "chips": self.dom_chips}
        if js == fetch.MENU_JS and arg[0] == sel.THINKING_ITEM:
            return {"selector": sel.THINKING_ITEM, "items": [
                {"name": level.title(), "description": None, "current": level == self.thinking,
                 "disabled": level in self.disabled_levels} for level in fetch.THINKING_LEVELS]}
        if js == fetch.MENU_JS:
            return {"selector": ITEM, "items": [dict(item) for item in self.menu]}
        assert js == fetch.BATCH_JS
        key = (arg["rpcid"], json.dumps(arg["payload"]))
        self.calls.append(key)
        answer = self.rpcs.get(key, self.rpcs.get(arg["rpcid"], (OK, ")]}'\n\n")))
        if isinstance(answer, list):  # answered in turn, the last one from then on
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        status, text = answer
        return {"status": status, "contentType": "application/json", "body": text}


@pytest.fixture(autouse=True)
def fast_polls(monkeypatch):
    monkeypatch.setattr(fetch, "STABLE_SECONDS", 0)
    monkeypatch.setattr(fetch, "SETTLE_SECONDS", 0)
    monkeypatch.setattr(fetch, "SEND_SECONDS", 0.3)
    monkeypatch.setattr(fetch, "PICK_SECONDS", 0)
    guard.state.blocked.clear()


@pytest.fixture
def page(monkeypatch):
    holder = {}

    def use(*args, **kwargs):
        holder["page"] = FakePage(*args, **kwargs)

        @asynccontextmanager
        async def fake_open_page(**open_kwargs):
            holder["page"].opened_with = open_kwargs
            yield holder["page"]

        monkeypatch.setattr(fetch, "open_page", fake_open_page)
        return holder["page"]

    return use


def run(coro):
    return asyncio.run(coro)


def key(rpcid, payload):
    return (rpcid, json.dumps(payload))


FIRST = key("MaZiqc", [34, None, [1, None, 1]])  # pinned chats
RECENT = key("MaZiqc", [34, None, [0, None, 1]])  # the rest, newest first
SECOND = key("MaZiqc", [20, "cursor_page_2", [0, None, 1]])


def read_key(cursor=None):
    return key("hNvQHb", ["c_1a2b3c4d5e6f7a8b", 10, cursor, 1, [0], [4], None, 1])


def test_rpc_status_mapping(page):
    fake = page({"MaZiqc": (401, "")})
    fake.url = "https://gemini.google.com/app"
    with pytest.raises(AuthRequired):
        run(fetch.rpc(fake, "list_chats", []))
    for status, match in ((-1, "failed in the page"), (500, "HTTP 500.*capture")):
        fake = page({"MaZiqc": (status, "TypeError: Failed to fetch")})
        with pytest.raises(RuntimeError, match=match):
            run(fetch.rpc(fake, "list_chats", []))
    fake = page({"MaZiqc": (OK, body("rpc_denied.txt"))})
    with pytest.raises(AuthRequired, match="code 7"):
        run(fetch.rpc(fake, "list_chats", []))
    fake = page({"hNvQHb": (OK, body("rpc_error.txt"))})
    with pytest.raises(fetch.NotFound, match="Chat x not found"):
        run(fetch.rpc(fake, "read_chat", [], item="Chat x"))
    fake = page({"MaZiqc": (OK, "<html>sign in</html>")})
    with pytest.raises(RuntimeError, match="no MaZiqc result"):
        run(fetch.rpc(fake, "list_chats", []))


def test_open_app_redirects(page):
    with pytest.raises(AuthRequired, match="accounts.google.com"):
        run(fetch.open_app(page(lands_on="https://accounts.google.com/v3/signin/identifier")))
    with pytest.raises(RuntimeError, match="redirected to www.google.com"):
        run(fetch.open_app(page(lands_on="https://www.google.com/sorry/index")))


def test_session_and_status(page):
    page({FIRST: (OK, body("list_chats_page1.txt"))})
    assert run(fetch.status()) == {"signedIn": True, "name": "Test Owner", "email": "owner@example.com", "api": "ok"}
    page({FIRST: (500, "")})
    state = run(fetch.status())
    assert state["signedIn"] is True and "HTTP 500" in state["api"]
    fake = page(token=False)
    assert run(fetch.status())["signedIn"] is False and fake.calls == []
    page({FIRST: (401, "")})
    assert run(fetch.status())["signedIn"] is False
    page(lands_on="https://accounts.google.com/ServiceLogin")
    assert run(fetch.status()) == {"signedIn": False, "name": None, "email": None, "api": None}


def test_data_commands_need_a_token(page):
    page(token=False)
    with pytest.raises(AuthRequired, match="Not signed in"):
        run(fetch.chats(5))


def test_chats_lists_pinned_then_the_rest_and_stops_at_the_limit(page):
    lists = {FIRST: (OK, body("list_chats_pinned.txt")), RECENT: (OK, body("list_chats_page1.txt")),
             SECOND: (OK, body("list_chats_page2.txt"))}
    fake = page(lists)
    rows = run(fetch.chats(50))
    assert [(r["id"], r["pinned"]) for r in rows] == [
        ("5f4e3d2c1b0a9988", True),  # the pinned list (one call, no cursor)
        ("1a2b3c4d5e6f7a8b", True), ("0f1e2d3c4b5a6978", False), ("9a8b7c6d5e4f3a2b", False),  # then the rest
        ("00112233445566aa", False)]  # ...and its next page
    assert fake.calls == [FIRST, RECENT, SECOND]
    assert fake.opened_with == {"headless": True}  # read-only guard: no send=True
    fake = page(lists)
    assert len(run(fetch.chats(2))) == 2 and fake.calls == [FIRST, RECENT]
    fake = page(lists)
    assert [r["id"] for r in run(fetch.chats(1))] == ["5f4e3d2c1b0a9988"] and fake.calls == [FIRST]


def test_chat_transcript_pages_oldest_first(page):
    fake = page({read_key(): (OK, body("read_chat_paged.txt")), read_key("cursor_older"): (OK, body("read_chat.txt"))})
    result = run(fetch.chat("1a2b3c4d5e6f7a8b"))
    assert fake.calls == [read_key(), read_key("cursor_older")]
    assert [m["text"] for m in result["messages"]][-2:] == ["And in winter?", "Winter in Kyoto is quiet[^1][^2].\n\n"
        "[^1]: Kyoto City Official Travel Guide, https://kyoto.travel/en/\n"
        "[^2]: Kyoto | JNTO, https://www.japan.travel/en/destinations/kansai/kyoto/"]
    assert result["messages"][0]["text"] == "Plan 3 days in Kyoto"
    assert result["url"] == "https://gemini.google.com/app/1a2b3c4d5e6f7a8b"


def test_chat_not_found(page):
    page({read_key(): (OK, body("rpc_error.txt"))})
    with pytest.raises(fetch.NotFound):
        run(fetch.chat("1a2b3c4d5e6f7a8b"))
    page({read_key(): (OK, ")]}'\n\n" + json.dumps([["wrb.fr", "hNvQHb", "[]", None, None, [0], "generic"]]))})
    with pytest.raises(fetch.NotFound, match="not found"):
        run(fetch.chat("1a2b3c4d5e6f7a8b"))


MENU = [
    {"name": "Fast", "description": "Answers quickly", "current": True, "disabled": False},
    {"name": "Thinking", "description": "Solves complex problems", "current": False, "disabled": False},
    {"name": "Pro", "description": "Advanced math and code", "current": False, "disabled": False},
    {"name": "Thinking with Pro", "description": "Deep reasoning", "current": False, "disabled": True},
]


def test_match_model():
    assert fetch.match_model(MENU, "fast") == 0
    assert fetch.match_model(MENU, " THINKING ") == 1  # exact beats the substring in "Thinking with Pro"
    assert fetch.match_model(MENU, "with pro") == 3
    assert fetch.match_model(MENU, "pro ") == 2  # exact, even though "Thinking with Pro" contains it
    with pytest.raises(fetch.NotFound, match="more than one model: Thinking, Thinking with Pro"):
        fetch.match_model(MENU, "ink")
    with pytest.raises(fetch.NotFound, match="Available: Fast, Thinking, Pro, Thinking with Pro"):
        fetch.match_model(MENU, "gpt-5")


LIVE = ["3.5 Flash-Lite", "3.8 Flash", "3.1 Pro", "4 Argon"]  # the menu on 2026-10-15


def live_menu(current=None):
    return [{"name": n, "description": None, "current": n == current, "disabled": False} for n in LIVE]


def test_match_model_on_the_live_names():
    assert fetch.match_model(live_menu(), "Argon") == 3
    assert fetch.match_model(live_menu(), "argon") == 3
    assert fetch.match_model(live_menu(), "3.8 Flash") == 1
    assert fetch.match_model(live_menu(), "pro") == 2
    assert fetch.match_model(live_menu(), "flash") == 1  # normalized exact beats Flash-Lite
    with pytest.raises(fetch.NotFound, match="more than one model: 3.5 Flash-Lite, 3.8 Flash"):
        fetch.match_model(live_menu(), "fla")


def test_mark_current_tells_flash_from_flash_lite():
    assert [r["current"] for r in fetch._mark_current(live_menu(), "Flash-Lite")] == [True, False, False, False]
    assert [r["current"] for r in fetch._mark_current(live_menu(), "Flash")] == [False, True, False, False]
    assert [r["current"] for r in fetch._mark_current(live_menu(), "Argon")] == [False, False, False, True]


def test_model_menu_clicks_again_when_a_click_opens_nothing(page):
    fake = page(menu=live_menu("3.5 Flash-Lite"))
    fake.dead_clicks = 1  # the picker is on the page before Angular wires up its click handler
    selector, items = run(fetch._open_model_menu(fake))
    assert (selector, [i["name"] for i in items]) == (ITEM, LIVE)
    picker = [e for e in fake.events if e in (("click", sel.MODEL_BUTTON), ("key", "Escape"))]
    assert picker == [("click", sel.MODEL_BUTTON), ("key", "Escape"), ("click", sel.MODEL_BUTTON)]
    fake = page(menu=live_menu())
    fake.dead_clicks = 3
    with pytest.raises(RuntimeError, match="Couldn't open Gemini's model menu"):
        run(fetch._open_model_menu(fake))
    assert fake.events.count(("click", sel.MODEL_BUTTON)) == 3


def test_pick_model_clicks_again_until_the_picker_names_the_row(page):
    fake = page(menu=live_menu("3.8 Flash"))
    fake.texts[sel.MODEL_BUTTON_LABEL] = "Flash"
    fake.dead_picks = 1  # the first click on the row doesn't take
    assert run(fetch._pick_model(fake, "flash-lite")) == "Flash-Lite"
    assert fake.events.count(("nth", ITEM, 0)) == 2
    fake = page(menu=live_menu("3.8 Flash"))
    fake.texts[sel.MODEL_BUTTON_LABEL] = "Flash"  # contained in "3.5 Flash-Lite", but it names 3.8 Flash
    fake.dead_picks = 2
    with pytest.raises(RuntimeError, match="Picked '3.5 Flash-Lite'.*still shows 'Flash'"):
        run(fetch._pick_model(fake, "flash-lite"))
    assert fake.events.count(("nth", ITEM, 0)) == 2


def test_models_reads_the_menu_and_picks_nothing(page):
    fake = page(menu=MENU)
    rows = run(fetch.models())
    assert rows[0] == {"name": "Fast", "description": "Answers quickly", "current": True}
    assert len(rows) == 4
    assert ("key", "Escape") in fake.events and not any(e[0] == "nth" for e in fake.events)


def test_ask_reads_the_network_answer(page):
    fake = page(answers=body("stream_generate.txt"), menu=MENU)
    result = run(fetch.ask("What is 2+2?"))
    assert result == {
        "chatId": "7e6d5c4b3a291807",
        "chatUrl": "https://gemini.google.com/app/7e6d5c4b3a291807",
        "model": "Fast",
        "thinking": None,  # no --thinking: the level is left alone
        "prompt": "What is 2+2?",
        "answer": "Two plus two is **4**[^1].\n\n```python\nprint(2 + 2)\n```\n\n"
                  "[^1]: Kyoto City Official Travel Guide, https://kyoto.travel/en/",
        "imageCount": 0, "canvas": None,
        "citations": [{"n": 1, "label": "kyoto.travel", "title": "Kyoto City Official Travel Guide",
                       "url": "https://kyoto.travel/en/"}],
    }
    assert fake.opened_with == {"headless": True, "send": True, "download": False}
    assert ("insert", "What is 2+2?") in fake.events and ("click", sel.SEND_BUTTON) in fake.events
    assert fake.url == "https://gemini.google.com/app"  # a new chat by default
    assert ("click", sel.THINKING_ROW) not in fake.events


def test_ask_picks_a_model_and_continues_a_chat(page):
    fake = page({read_key(): (OK, body("read_chat.txt"))}, answers=body("stream_generate.txt"), menu=MENU)
    result = run(fetch.ask("And in spring?", model="thinking", chat_id="1a2b3c4d5e6f7a8b"))
    assert fake.url == "https://gemini.google.com/app/1a2b3c4d5e6f7a8b"
    pick = ("nth", ITEM, 1)  # the MODEL_ITEM alternative that matched
    assert pick in fake.events
    assert fake.events.index(pick) < fake.events.index(("click", sel.SEND_BUTTON))
    assert result["model"] == "Thinking"  # what the picker shows afterwards


def test_ask_picks_the_thinking_level_after_the_model(page):
    fake = page(answers=body("stream_generate.txt"), menu=MENU)
    result = run(fetch.ask("Prove it", model="thinking", thinking="high"))
    assert (result["model"], result["thinking"], fake.thinking) == ("Thinking", "high", "high")
    order = [("nth", ITEM, 1), ("click", sel.THINKING_ROW), ("nth", sel.THINKING_ITEM, 2), ("click", sel.SEND_BUTTON)]
    assert [fake.events.index(e) for e in order] == sorted(fake.events.index(e) for e in order)
    assert fake.events.count(("nth", sel.THINKING_ITEM, 2)) == 1  # then the reopened menu showed it selected


def test_thinking_level_already_selected_is_not_clicked(page):
    fake = page(answers=body("stream_generate.txt"), menu=MENU)
    fake.thinking = "medium"
    assert run(fetch.ask("hi", thinking="medium"))["thinking"] == "medium"
    assert not any(e[:2] == ("nth", sel.THINKING_ITEM) for e in fake.events)
    assert ("click", sel.THINKING_ROW) in fake.events and ("key", "Escape") in fake.events


def test_pick_thinking_clicks_again_until_the_menu_shows_it(page):
    fake = page(menu=MENU)
    fake.dead_levels = 1  # the first click on the row doesn't take
    assert run(fetch._pick_thinking(fake, "high")) == "high"
    assert fake.events.count(("nth", sel.THINKING_ITEM, 2)) == 2
    fake = page(menu=MENU)
    fake.dead_levels = 2
    with pytest.raises(RuntimeError, match="Picked thinking level 'high', but Gemini's menu still shows 'Low'"):
        run(fetch._pick_thinking(fake, "high"))
    assert fake.events.count(("nth", sel.THINKING_ITEM, 2)) == 2
    assert not fake.menu_open


def test_ask_refuses_a_thinking_level_before_sending(page):
    fake = page(answers=body("stream_generate.txt"), menu=MENU)
    fake.thinking = None  # this model's menu has no Thinking level row
    with pytest.raises(fetch.NotFound, match="no Thinking level for this model"):
        run(fetch.ask("hi", thinking="high"))
    assert ("click", sel.SEND_BUTTON) not in fake.events and not fake.menu_open
    fake = page(answers=body("stream_generate.txt"), menu=MENU)
    fake.disabled_levels = {"high"}
    with pytest.raises(fetch.NotFound, match="'High' isn't available"):
        run(fetch.ask("hi", thinking="high"))
    assert ("click", sel.SEND_BUTTON) not in fake.events and fake.thinking == "low"


def test_ask_refuses_before_sending(page):
    fake = page({read_key(): (OK, body("rpc_error.txt"))}, answers=body("stream_generate.txt"), menu=MENU)
    with pytest.raises(fetch.NotFound):
        run(fetch.ask("hi", chat_id="1a2b3c4d5e6f7a8b"))
    assert ("click", sel.SEND_BUTTON) not in fake.events
    fake = page(answers=body("stream_generate.txt"), menu=MENU)
    with pytest.raises(fetch.NotFound, match="isn't available"):
        run(fetch.ask("hi", model="thinking with pro"))
    assert ("click", sel.SEND_BUTTON) not in fake.events


def test_ask_falls_back_to_the_page_text(page):
    empty_stream = ")]}'\n\n" + json.dumps([["wrb.fr", None, json.dumps([None, ["c_7e6d5c4b3a291807", "r_1"]])]])
    page(answers=empty_stream, dom_text="  Rendered answer  ")
    result = run(fetch.ask("hi", timeout=10))
    assert (result["answer"], result["chatId"], result["citations"]) == ("Rendered answer", "7e6d5c4b3a291807", [])


def test_ask_google_error_codes(page):
    page(answers=body("stream_generate_error.txt"))
    with pytest.raises(RuntimeError, match="error 1037 .*usage limit"):
        run(fetch.ask("hi"))


def test_ask_reports_what_the_guard_blocked_when_nothing_was_sent(page):
    fake = page(answers=None)

    class Blocked:
        method = "POST"
        url = "https://gemini.google.com/_/BardChatUi/data/batchexecute?rpcids=L5adhe&f.sid=-4271880281862545069&rt=c"
        post_data = "f.req=" + json.dumps([[["L5adhe", "[]", None, "generic"]]]) + "&at=AJvLN6M-fake%3A1"

    original = fake.locator

    def locator(selector):
        if selector == sel.SEND_BUTTON:
            guard.state.begin(POLICY, send=True)
            assert guard.check(Blocked(), own_page=True) is False  # the guard records what it blocks
        return original(selector)

    fake.locator = locator
    with pytest.raises(RuntimeError) as caught:
        run(fetch.ask("hi"))
    message = str(caught.value)
    assert "Gemini never sent the prompt (no new response within 0.3s)." in message
    assert "StreamGenerate" not in message
    assert "POST gemini.google.com/_/BardChatUi/data/batchexecute rpcids=L5adhe" in message
    assert "4271880281862545069" not in message and "AJvLN6M" not in message  # no session id or token


def test_ask_without_a_prompt_box(page):
    fake = page(answers=body("stream_generate.txt"))
    fake.present = set()
    with pytest.raises(RuntimeError, match="prompt box never appeared.*selectors.py"):
        run(fetch.ask("hi"))


def test_multiline_prompts_are_typed_with_shift_enter(page):
    fake = page(answers=body("stream_generate.txt"))
    run(fetch.ask("line one\n\nline three"))
    typed = [e for e in fake.events if e[0] in ("insert", "key")]
    assert typed == [("insert", "line one"), ("key", "Shift+Enter"), ("key", "Shift+Enter"), ("insert", "line three")]


def test_current_model_comes_from_the_picker_label_when_the_menu_marks_none(page):
    rows = [{"name": n, "description": None, "current": False, "disabled": False}
            for n in ("3.5 Flash", "3.5 Flash-Lite", "3.1 Pro")]
    assert [r["current"] for r in fetch._mark_current([dict(r) for r in rows], "Flash")] == [True, False, False]
    assert [r["current"] for r in fetch._mark_current([dict(r) for r in rows], "3.1 Pro")] == [False, False, True]
    assert [r["current"] for r in fetch._mark_current([dict(r) for r in rows], "Lite")] == [False, True, False]
    fake = page(menu=rows)
    fake.texts[sel.MODEL_BUTTON_LABEL] = "Pro"
    assert [r["current"] for r in run(fetch.models())] == [False, False, True]


def test_dom_fallback_never_returns_an_old_answer(page):
    empty_stream = ")]}'\n\n" + json.dumps([["wrb.fr", None, json.dumps([None, ["c_1a2b3c4d5e6f7a8b", "r_2"]])]])
    fake = page({read_key(): (OK, body("read_chat.txt"))}, answers=empty_stream, dom_text="Old answer, re-rendered")
    fake.old_answer = {"count": 2, "text": "Old answer", "busy": False}
    fake.dom_count = 2  # no new model-response appeared
    with pytest.raises(RuntimeError, match="Timed out.*final answer.*was sent"):
        run(fetch.ask("hi", chat_id="1a2b3c4d5e6f7a8b", timeout=1))  # waits out the deadline for a new answer
    fake = page({read_key(): (OK, body("read_chat.txt"))}, answers=empty_stream, dom_text="New answer")
    fake.old_answer = {"count": 2, "text": "Old answer", "busy": False}
    fake.dom_count = 3
    assert run(fetch.ask("hi", chat_id="1a2b3c4d5e6f7a8b", timeout=1))["answer"] == "New answer"


def test_current_model_prefers_the_aria_label_over_icon_text(page):
    fake = page()
    fake.texts = {sel.MODEL_BUTTON: "Flash arrow_drop_down"}

    async def aria(self, name, **kwargs):
        return "Open mode picker, currently Flash" if self.selector == sel.MODEL_BUTTON else None

    Locator.get_attribute, original = aria, Locator.get_attribute
    try:
        assert run(fetch._current_model(fake)) == "Flash"
    finally:
        Locator.get_attribute = original
    fake.texts = {sel.MODEL_BUTTON: "Flash arrow_drop_down"}
    assert run(fetch._current_model(fake)) == "Flash arrow_drop_down"  # last resort


def test_ask_waits_for_uploads_before_starting_the_send_timer(page, tmp_path):
    fake = page(answers=body("stream_generate.txt"))
    run(fetch.ask("hi", files=[str(tmp_path / "a.txt")]))
    assert ("files", (str(tmp_path / "a.txt"),)) in fake.events
    assert fake.events.index(("files", (str(tmp_path / "a.txt"),))) < fake.events.index(("ready",))
    assert fake.events.index(("ready",)) < fake.events.index(("click", sel.SEND_BUTTON))


def test_ask_waits_until_gemini_shows_every_file_uploaded(page, tmp_path, monkeypatch):
    monkeypatch.setattr(fetch.asyncio, "sleep", fast_sleep)
    fake = page(answers=body("stream_generate.txt"))
    fake.uploads = [{"count": 1, "busy": 0, "failed": 0}, {"count": 2, "busy": 1, "failed": 0},
                    {"count": 2, "busy": 0, "failed": 0}]
    run(fetch.ask("hi", files=[str(tmp_path / "a.txt"), str(tmp_path / "b.png")]))
    assert fake.uploads == [{"count": 2, "busy": 0, "failed": 0}] and fake.sent


@pytest.mark.parametrize(("state", "notice", "message"), [
    ({"count": 2, "busy": 2, "failed": 2}, None, r"couldn't upload the --file attachments \(2 of 2 failed\)"),
    ({"count": 2, "busy": 1, "failed": 0}, "File upload error, Couldn't upload",
     r"\(File upload error, Couldn't upload\)\. The prompt was not sent\."),
])
def test_a_failed_upload_stops_before_sending(page, tmp_path, state, notice, message):
    fake = page(answers=body("stream_generate.txt"))
    fake.uploads = [state]
    fake.notices_before = [notice] if notice else []
    with pytest.raises(RuntimeError, match=message):
        run(fetch.ask("hi", files=[str(tmp_path / "a.txt"), str(tmp_path / "b.png")]))
    assert not fake.sent


def test_uploads_that_never_finish_stop_before_sending(page, tmp_path, monkeypatch):
    monkeypatch.setattr(fetch, "UPLOAD_SECONDS", 0)
    fake = page(answers=body("stream_generate.txt"))
    fake.uploads = [{"count": 1, "busy": 1, "failed": 0}]
    with pytest.raises(RuntimeError, match=r"still uploading .* after 0s \(0 of 1 done\)\. The prompt was not sent"):
        run(fetch.ask("hi", files=[str(tmp_path / "a.txt")]))
    assert not fake.sent


def test_ask_sends_with_enter_only_when_the_button_never_enables(page):
    fake = page(answers=body("stream_generate.txt"))
    fake.button_ready = False
    with pytest.raises(RuntimeError, match="never sent the prompt"):  # the fake only answers the button
        run(fetch.ask("hi"))
    assert ("press", "Enter") in fake.events and ("click", sel.SEND_BUTTON) not in fake.events
    fake = page(answers=body("stream_generate.txt"))
    run(fetch.ask("hi"))
    assert ("press", "Enter") not in fake.events and fake.events.count(("click", sel.SEND_BUTTON)) == 1


def _after_send(fake, action):
    """A Locator.click that runs `action(fake)` right after the send button is clicked."""
    original = Locator.click

    async def click(self, **kwargs):
        await original(self, **kwargs)
        if self.selector == sel.SEND_BUTTON:
            action(fake)

    return click


def test_ask_reports_a_broken_stream_instead_of_waiting_out_the_timeout(page, monkeypatch):
    def break_stream(fake):
        fake.response.hang = True
        fake.response.request.failure = "net::ERR_CONNECTION_RESET"
        asyncio.get_running_loop().call_later(0.05, fake.context.handlers["requestfailed"], fake.response.request)

    fake = page(answers=body("stream_generate.txt"))
    monkeypatch.setattr(Locator, "click", _after_send(fake, break_stream))
    with pytest.raises(RuntimeError, match="stream broke .*ERR_CONNECTION_RESET.*was sent"):
        run(fetch.ask("hi", timeout=30))


def test_ask_http_errors_are_not_answers(page, monkeypatch):
    fake = page(answers=body("stream_generate.txt"))
    monkeypatch.setattr(Locator, "click", _after_send(fake, lambda f: setattr(f.response, "status", 429)))
    with pytest.raises(RuntimeError, match="HTTP 429"):
        run(fetch.ask("hi"))


def test_ask_rejects_an_answer_without_a_completion_marker(page):
    stream = body("stream_generate.txt")
    page(answers=stream[: stream.index("889\n")])  # the frames before the final one: status 1, no [25]
    with pytest.raises(RuntimeError, match="without its completion marker.*was sent"):
        run(fetch.ask("hi"))


def test_ask_never_says_never_sent_once_the_request_left(page, monkeypatch):
    class Sent:
        url, method = Response.url, "POST"

    fake = page(answers=None)
    monkeypatch.setattr(Locator, "click", _after_send(fake, lambda f: f.context.handlers["request"](Sent())))
    with pytest.raises(RuntimeError, match="didn't start answering.*was sent") as caught:
        run(fetch.ask("hi"))
    assert "never sent" not in str(caught.value)


def test_ask_over_the_webchannel_reads_the_answer_off_the_page(page):
    fake = page(channel=True, url_after_send=NEW_CHAT, dom_text="  pong  ")
    result = run(fetch.ask("Reply with exactly: pong"))
    assert result == {"chatId": "7e6d5c4b3a291807", "chatUrl": "https://gemini.google.com/app/7e6d5c4b3a291807",
                      "model": "Fast", "thinking": None, "prompt": "Reply with exactly: pong", "answer": "pong",
                      "citations": [], "imageCount": 0, "canvas": None}
    assert fake.opened_with == {"headless": True, "send": True, "download": False}
    assert fake.events.count(("click", sel.SEND_BUTTON)) == 1 and ("press", "Enter") not in fake.events


def test_ask_over_the_webchannel_continues_a_chat(page):
    fake = page({read_key(): (OK, body("read_chat.txt"))}, channel=True, dom_text="Spring is busy.")
    fake.old_answer = {"count": 2, "text": "Winter in Kyoto is quiet.", "busy": False}
    fake.dom_count = 3
    result = run(fetch.ask("And in spring?", chat_id="1a2b3c4d5e6f7a8b"))
    assert (result["chatId"], result["answer"]) == ("1a2b3c4d5e6f7a8b", "Spring is busy.")


def test_ask_waits_for_a_new_answer_after_the_webchannel_send(page):
    fake = page(channel=True, url_after_send=NEW_CHAT, dom_text=None)  # sent, but no answer ever renders
    with pytest.raises(RuntimeError, match="Timed out.*final answer.*was sent"):
        run(fetch.ask("hi", timeout=1))
    assert fake.sent


def test_ask_listens_for_the_prompt_only_from_the_click_on(page):
    fake = page(channel=True, url_after_send=NEW_CHAT, dom_text="pong")
    listening = []
    ready = fake.wait_for_function

    async def note_listeners(*args, **kwargs):  # the send button's readiness check, just before the click
        listening.append(sorted(fake.context.handlers))
        return await ready(*args, **kwargs)

    fake.wait_for_function = note_listeners
    run(fetch.ask("hi"))
    assert listening == [[]]
    assert sorted(fake.context.handlers) == ["request", "requestfailed", "response"]


@pytest.mark.parametrize(("url", "method", "kind"), [
    (Response.url, "POST", "StreamGenerate"),
    (CHANNEL + "?VER=8&RID=81906&CVER=22&zx=x&t=1", "POST", "processSession"),
    (CHANNEL + "?VER=8&SID=x&RID=81907&AID=3&zx=x&t=1", "POST", "processSession"),
    (CHANNEL + "?VER=8&SID=x&TYPE=terminate&zx=x", "POST", None),  # closing the channel sends nothing
    (CHANNEL + "?VER=8&RID=rpc&SID=x&CI=0&AID=3&TYPE=xmlhttp&zx=x&t=1", "GET", None),  # the back channel
    ("https://geminiweb-pa.clients6.google.com/v1/otherThing", "POST", None),
    ("https://gemini.google.com/_/BardChatUi/data/batchexecute?rpcids=MaZiqc", "POST", None),
])
def test_prompt_request(url, method, kind):
    assert fetch._prompt_request(Request(url, method)) == kind


def test_each_sign_that_the_prompt_left():
    async def sent(url_before, url_after, *, outbound=(), before=None, after=None):
        fake = FakePage()
        fake.url, fake.sent = url_after, True
        fake.dom_text = after and after["text"]
        fake.dom_count = after["count"] if after else 0
        deadline = asyncio.get_running_loop().time() + 0.05
        return await fetch._sent(fake, before, url_before, list(outbound), deadline)

    old = {"count": 2, "text": "old", "busy": False}
    assert run(sent("https://gemini.google.com/app", "https://gemini.google.com/app", outbound=["processSession"]))
    assert run(sent("https://gemini.google.com/app", NEW_CHAT))  # the new chat got its id
    assert run(sent("https://gemini.google.com/app", "https://gemini.google.com/app",
                    after={"count": 1, "text": "", "busy": True}))  # a new model-response
    assert run(sent(NEW_CHAT, NEW_CHAT, before=old, after={"count": 3, "text": "", "busy": True}))
    assert not run(sent(NEW_CHAT, NEW_CHAT, before=old, after=old))  # same chat, same responses
    assert not run(sent("https://gemini.google.com/app", "https://gemini.google.com/app"))
    assert not run(sent("https://gemini.google.com/app", "https://gemini.google.com/settings"))


@pytest.mark.parametrize('wanted', ['4 Argon', ' 4   ARGON ', 'Argon', 'argon'])
@pytest.mark.parametrize('name', ['4 Argon', 'Argon'])
def test_argon_version_alias(wanted, name):
    items = [{'name': name}]
    assert fetch.match_model(items, wanted) == 0
    assert fetch._labelled_row(items, wanted) == 0


def test_version_alias_does_not_silently_pick_ambiguous_models():
    with pytest.raises(fetch.NotFound, match='more than one'):
        fetch.match_model([{'name': '4 Argon'}, {'name': '5 Argon'}], 'Argon')
    assert fetch.match_model([{'name': '4 Argon'}, {'name': '5 Argon'}], '4 Argon') == 0


@pytest.mark.parametrize(('state', 'before', 'fresh'), [
    ({'count': 2, 'key': 'new'}, {'count': 2, 'keys': ['old']}, True),
    ({'count': 3, 'key': 'old'}, {'count': 2, 'keys': ['old']}, False),
    (None, None, False),
])
def test_response_identity_beats_node_count(state, before, fresh):
    assert fetch._fresh_response(state, before) is fresh


def test_dom_wait_resets_for_busy_period_and_replaced_candidate(monkeypatch):
    states = iter([
        {'count': 1, 'key': 'a', 'text': 'placeholder', 'busy': True},
        {'count': 1, 'key': 'a', 'text': 'partial', 'busy': False, 'complete': False},
        {'count': 1, 'key': 'b', 'text': 'final', 'busy': False},
        {'count': 1, 'key': 'b', 'text': 'final', 'busy': True},
        {'count': 1, 'key': 'b', 'text': 'final', 'busy': False},
    ])
    seen = []

    async def snapshot(page):
        state = next(states, {'count': 1, 'key': 'b', 'text': 'final', 'busy': False})
        seen.append(state)
        return state

    monkeypatch.setattr(fetch, '_last_response', snapshot)

    async def go():
        return await fetch._dom_answer(None, None, asyncio.get_running_loop().time() + 5)

    assert run(go()) == {'text': 'final', 'chips': [], 'images': 0, 'canvases': 0, 'key': 'b'}
    assert len(seen) == 6  # not when the busy flag first disappears after a long pause


@pytest.mark.parametrize('stage', ['_signed_in_page', '_pick_model', '_send_button_ready', '_sent', '_dom_answer'])
def test_overall_timeout_bounds_every_stage_and_cleans_up(page, monkeypatch, stage):
    fake = page(channel=True, dom_text='answer', menu=live_menu())
    cleaned = []

    @asynccontextmanager
    async def context(**kwargs):
        try:
            yield fake
        finally:
            cleaned.append(True)

    async def hangs(*args, **kwargs):
        await asyncio.sleep(3600)

    async def baseline(page):
        return None

    monkeypatch.setattr(fetch, '_settled_last_response', baseline)
    monkeypatch.setattr(fetch, 'open_page', context)
    monkeypatch.setattr(fetch, stage, hangs)
    with pytest.raises(RuntimeError, match=r'Timed out after 0.05s while .*--timeout') as error:
        run(fetch.ask('hi', model='Argon', timeout=0.05))
    assert cleaned == [True]
    assert ('was sent' in str(error.value)) == (stage in ('_sent', '_dom_answer'))


@pytest.mark.parametrize('timeout', [0, -1, float('inf'), float('nan')])
def test_ask_rejects_unbounded_timeout(timeout):
    with pytest.raises(ValueError, match='finite positive'):
        run(fetch.ask('hi', timeout=timeout))


def test_stream_response_must_belong_to_request_observed_after_submission(page):
    fake = page(answers=body('stream_generate.txt'))
    submit = fake.submit

    def old_response_then_submit():
        old = Response(body('stream_generate_error.txt'))
        old.request = Request(old.url)  # this request started before our listeners were registered
        old.request.failure = 'stale stream failed'
        fake.context.handlers['response'](old)
        fake.context.handlers['requestfailed'](old.request)
        submit()

    fake.submit = old_response_then_submit
    assert run(fetch.ask('What is 2+2?'))['answer'].startswith('Two plus two is **4**')


@pytest.mark.parametrize('after_send', [False, True])
def test_limit_banner_before_or_after_send_fails_without_retry(page, after_send):
    fake = page(channel=True)
    message = 'Your limit resets at 1:53 PM'
    if after_send:
        fake.notices_after = [message]
    else:
        fake.notices_before = [message]
        fake.present.clear()  # a blocking dialog must win over a missing prompt box
    with pytest.raises(fetch.RateLimited) as error:
        run(fetch.ask('hi', timeout=2))
    assert str(error.value) == message
    assert error.value.sent == after_send
    assert fake.sent == after_send
    assert fake.events.count(('click', sel.SEND_BUTTON)) == int(after_send)


def test_limit_banner_while_stream_is_stalled(page):
    fake = page(answers=body('stream_generate.txt'))
    original_submit = fake.submit

    def submit():
        original_submit()

        async def finish():
            fake.notices_after = ['Quota exceeded. Try again tomorrow at 1:53 PM.']
            await asyncio.sleep(60)
        fake.response.finished = finish

    fake.submit = submit
    with pytest.raises(fetch.RateLimited, match='Quota exceeded'):
        run(fetch.ask('hi', timeout=2))


def test_limit_banner_while_waiting_for_dom_answer(page, monkeypatch):
    fake = page(channel=True)
    original = fetch._last_response

    async def state(page):
        result = await original(page)
        if fake.sent:
            fake.notices_after = ['Usage limit reached']
        return result

    monkeypatch.setattr(fetch, '_last_response', state)
    with pytest.raises(fetch.RateLimited, match='Usage limit'):
        run(fetch.ask('hi', timeout=2))


def test_limit_answer_with_incomplete_dom_marker(page, monkeypatch):
    fake = page(channel=True)
    original = fetch._last_response

    async def state(page):
        if fake.sent:
            return {'count': 1, 'text': "You've reached your limit", 'busy': True, 'complete': False}
        return await original(page)

    monkeypatch.setattr(fetch, '_last_response', state)
    assert run(fetch.ask('hi', timeout=2))['answer'] == "You've reached your limit"


# ask --image-out: the Create image tool, the transcript, the full-size link (c8o8Fe), and the image hops.
IMAGE_CHAT = "https://gemini.google.com/app/0000000000000001"  # read_chat_image.txt's chat
IMAGE_READ = key("hNvQHb", ["c_0000000000000001", 10, None, 1, [0], [4], None, 1])
IMAGE_KEY = "model-response-message-contentr_0000000000000002"
GG_DL = "https://lh3.googleusercontent.com/gg-dl/FAKE-FULL"
FIFE = "https://work.fife.usercontent.google.com/rd-gg-dl/FAKE-HOP=s0-d-I-rp?alr=yes"
FINAL = "https://lh3.googleusercontent.com/rd-gg-dl/FAKE-LAST=s0-d-I-rp"
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x0a\xc0\x00\x00\x06\x00\x08\x02\x00\x00\x00" + b"\x00" * 16
TEXT = {"content-type": "text/plain; charset=UTF-8"}


def full_size_link(url=GG_DL):
    envelope = [["wrb.fr", "c8o8Fe", json.dumps([url]), None, None, None, "generic"]]
    return (OK, ")]}'\n\n" + json.dumps(envelope))


def image_page(page, rpcs=None, routes=None):
    rpcs = {IMAGE_READ: (OK, body("read_chat_image.txt")), "c8o8Fe": full_size_link(), **(rpcs or {})}
    routes = {GG_DL + "=d-I-rp?alr=yes": (200, TEXT, FIFE.encode()),  # each hop answers the next URL...
              FIFE: (302, {"location": FINAL}, b""),  # ...or redirects to it
              FINAL: (200, {"content-type": "image/png"}, PNG), **(routes or {})}
    return page(rpcs, channel=True, url_after_send=IMAGE_CHAT, dom_images=1, dom_key=IMAGE_KEY, image_routes=routes)


def test_ask_with_images_turns_on_the_tool_and_saves_the_full_size_image(page):
    fake = image_page(page)
    result = run(fetch.ask("a seamless tileable pattern of small blue stars on white", image_format="png"))
    assert result["imageData"] == [{"data": PNG, "source_url_kind": "full_size"}]
    assert (result["answer"], result["imageCount"], result["chatId"]) == ("", 1, "0000000000000001")
    assert result["chatUrl"] == IMAGE_CHAT
    assert fake.opened_with == {"headless": True, "send": True, "download": True}
    assert fake.request.calls == [GG_DL + "=d-I-rp?alr=yes", FIFE, FINAL]
    full_size = [c for c in fake.calls if c[0] == "c8o8Fe"]
    assert [json.loads(c[1])[1] for c in full_size] == [["r_0000000000000002", "rc_0000000000000003", "c_0000000000000001", None, ""]]
    clicks = [e for e in fake.events if e[0] in ("click", "insert")]
    assert clicks.index(("click", sel.CREATE_IMAGE_ITEM)) < clicks.index(("insert", "a seamless tileable pattern of small blue stars on white"))
    assert ("click", sel.UPLOAD_MENU_BUTTON) in clicks


def test_ask_with_images_leaves_the_tool_on_when_it_already_is(page):
    fake = image_page(page)
    fake.counts[sel.IMAGE_TOOL_CHIP] = 1
    run(fetch.ask("stars", image_format="png"))
    assert ("click", sel.CREATE_IMAGE_ITEM) not in fake.events  # a click would turn it off


def test_ask_refuses_before_sending_when_the_image_tool_is_missing(page, monkeypatch):
    fake = image_page(page)

    async def no_tool(self, **kwargs):
        if self.selector == sel.CREATE_IMAGE_ITEM:
            raise PlaywrightTimeout("Timeout 10000ms exceeded.")
        self.page.events.append(("click", self.selector))

    monkeypatch.setattr(Locator, "click", no_tool)
    with pytest.raises(RuntimeError, match="Create image tool"):
        run(fetch.ask("stars", image_format="png"))
    assert not fake.sent


def test_the_jpeg_format_asks_the_image_server_for_jpeg(page):
    fake = image_page(page, routes={GG_DL + "=d-I?alr=yes": (200, {"content-type": "image/jpeg"}, b"\xff\xd8\xff\xe0")})
    assert run(fetch.ask("stars", image_format="jpeg"))["imageData"][0]["data"] == b"\xff\xd8\xff\xe0"
    assert fake.request.calls == [GG_DL + "=d-I?alr=yes"]


def test_a_failed_full_size_link_falls_back_to_the_preview(page):
    preview = "https://lh3.googleusercontent.com/gg/FAKE-TOKEN-4=d-I-rp?alr=yes"
    fake = image_page(page, rpcs={"c8o8Fe": (OK, ")]}'\n\n" + json.dumps([["er", None, None, None, None, 500]]))},
                      routes={preview: (200, {"content-type": "image/png"}, PNG)})
    result = run(fetch.ask("stars", image_format="png"))
    assert result["imageData"] == [{"data": PNG, "source_url_kind": "preview"}]
    assert fake.request.calls == [preview]


def test_an_image_hop_to_another_host_is_never_fetched(page):
    evil = "https://evil.example/steal?c=1"
    fake = image_page(page, routes={FIFE: (302, {"location": evil}, b""),
                                    "https://lh3.googleusercontent.com/gg/FAKE-TOKEN-4=d-I-rp?alr=yes": (200, TEXT, evil.encode())})
    with pytest.raises(RuntimeError, match=r"Refused to fetch an image from GET evil\.example/steal: not one of") as caught:
        run(fetch.ask("stars", image_format="png"))
    assert evil not in fake.request.calls
    assert "c=1" not in str(caught.value)  # no query string (signed URLs carry tokens there)


def test_a_403_on_the_last_hop_starts_the_download_over(page, monkeypatch):
    monkeypatch.setattr(fetch.asyncio, "sleep", fast_sleep)
    fake = image_page(page, routes={FINAL: [(403, {"content-type": "text/html"}, b"denied"),
                                            (200, {"content-type": "image/png"}, PNG)]})
    assert run(fetch.ask("stars", image_format="png"))["imageData"][0]["source_url_kind"] == "full_size"
    assert fake.request.calls == [GG_DL + "=d-I-rp?alr=yes", FIFE, FINAL] * 2


def test_an_image_server_that_keeps_refusing_fails_without_the_url(page, monkeypatch):
    monkeypatch.setattr(fetch.asyncio, "sleep", fast_sleep)
    denied = (403, {"content-type": "text/html"}, b"denied")
    image_page(page, routes={FINAL: [denied], "https://lh3.googleusercontent.com/gg/FAKE-TOKEN-4=d-I-rp?alr=yes": denied})
    with pytest.raises(RuntimeError, match=r"HTTP 403 \(GET lh3\.googleusercontent\.com/gg/FAKE-TOKEN-4=d-I-rp\)"):
        run(fetch.ask("stars", image_format="png"))


def test_a_text_reply_with_images_wanted_has_no_image_data(page):
    fake = page(channel=True, url_after_send=IMAGE_CHAT, dom_text="I can't make that image.")
    result = run(fetch.ask("stars", image_format="png"))
    assert (result["answer"], result["imageCount"], result["imageData"]) == ("I can't make that image.", 0, [])
    assert fake.request.calls == [] and not [c for c in fake.calls if c[0] == "hNvQHb"]


def test_an_image_reply_without_images_wanted_only_counts_them(page):
    fake = image_page(page)
    result = run(fetch.ask("stars"))
    assert (result["answer"], result["imageCount"], "imageData" in result) == ("", 1, False)
    assert fake.opened_with["download"] is False and fake.request.calls == []


def test_a_streamed_text_and_image_reply_without_images_wanted_waits_for_the_image(page):
    fake = image_page(page)
    fake.answers = body("stream_generate.txt").replace(
        r'print(2 + 2)\\n```\"]', r'print(2 + 2)\\n```\\n\\nhttp://googleusercontent.com/image_generation_content/0\"]')
    result = run(fetch.ask("stars"))
    assert result["answer"].startswith("Two plus two is **4**")  # the stream's markdown, placeholder stripped
    assert (result["imageCount"], "imageData" in result) == (1, False)  # counted on the page, not downloaded
    assert fake.opened_with["download"] is False and fake.request.calls == []


def test_a_streamed_image_count_holds_when_the_page_settles_without_the_image(page):
    fake = page(channel=False, url_after_send=IMAGE_CHAT, dom_text="Here you go.")
    fake.answers = body("stream_generate.txt").replace(
        r'print(2 + 2)\\n```\"]', r'print(2 + 2)\\n```\\n\\nhttp://googleusercontent.com/image_generation_content/0\"]')
    assert run(fetch.ask("stars"))["imageCount"] == 1


def test_the_transcript_gets_time_to_list_the_new_images(page, monkeypatch):
    monkeypatch.setattr(fetch, "TURN_SECONDS", 0)
    monkeypatch.setattr(fetch.asyncio, "sleep", fast_sleep)
    image_page(page, rpcs={IMAGE_READ: (OK, body("read_chat.txt"))})  # an older turn, no images yet
    with pytest.raises(RuntimeError, match=r"Gemini showed 1 image\(s\), but chat 0000000000000001's transcript lists 0"):
        run(fetch.ask("stars", image_format="png"))


_real_sleep = asyncio.sleep


async def fast_sleep(seconds):
    await _real_sleep(0)


def test_dom_wait_finishes_an_image_only_reply_once_its_image_settles(monkeypatch):
    states = iter([
        {'count': 1, 'key': 'a', 'text': '', 'busy': True, 'images': 0, 'imagesPending': 0},  # thinking
        {'count': 1, 'key': 'a', 'text': '', 'busy': False, 'images': 0, 'imagesPending': 1},  # rendering
        {'count': 1, 'key': 'a', 'text': '', 'busy': False, 'images': 1, 'imagesPending': 0},
    ])
    seen = []

    async def snapshot(page):
        state = next(states, {'count': 1, 'key': 'a', 'text': '', 'busy': False, 'images': 1, 'imagesPending': 0})
        seen.append(state)
        return state

    monkeypatch.setattr(fetch, '_last_response', snapshot)
    monkeypatch.setattr(fetch, '_check_limit', lambda page, sent: _real_sleep(0))

    async def go():
        return await fetch._dom_answer(None, None, asyncio.get_running_loop().time() + 5)

    assert run(go()) == {'text': '', 'chips': [], 'images': 1, 'canvases': 0, 'key': 'a'}
    assert len(seen) == 4  # the image has to hold still once, like text


def test_dom_wait_never_finishes_an_empty_reply(monkeypatch):
    async def snapshot(page):
        return {'count': 1, 'key': 'a', 'text': '  ', 'busy': False, 'images': 0, 'imagesPending': 0}

    monkeypatch.setattr(fetch, '_last_response', snapshot)
    monkeypatch.setattr(fetch, '_check_limit', lambda page, sent: _real_sleep(0))

    async def go():
        return await fetch._dom_answer(None, None, asyncio.get_running_loop().time() + 0.3)

    assert run(go()) is None


def test_a_streamed_answer_with_images_still_downloads_them(page):
    streamed_chat = key("hNvQHb", ["c_7e6d5c4b3a291807", 10, None, 1, [0], [4], None, 1])  # the stream's chat id
    fake = image_page(page, rpcs={streamed_chat: (OK, body("read_chat_image.txt"))})
    fake.answers = body("stream_generate.txt")  # Gemini went back to StreamGenerate: text comes off the network
    result = run(fetch.ask("stars", image_format="png"))
    assert result["answer"].startswith("Two plus two is **4**")  # the stream's markdown
    assert result["imageData"] == [{"data": PNG, "source_url_kind": "full_size"}]


# ask --canvas: the Canvas tool, then the doc from the transcript (read_chat_canvas.txt, sanitized live capture).
CANVAS_MD = "# Rain Probe\n\nHeavy rain drummed on the **tin** roof.\n\nThe cabin hummed along."
CANVAS_DOM = 'I will now generate the "Rain Probe" document for you.\n\nRain Probe\nExport\n\nHeavy rain drummed'


def canvas_page(page, read=None, canvases=1):
    fake = page({IMAGE_READ: read or (OK, body("read_chat_canvas.txt"))}, channel=True, url_after_send=IMAGE_CHAT,
                dom_text=CANVAS_DOM, dom_key=IMAGE_KEY)
    fake.dom_canvases = canvases
    return fake


def test_ask_with_canvas_turns_on_the_tool_and_reads_the_doc_from_the_transcript(page):
    fake = canvas_page(page)
    result = run(fetch.ask("Make a Canvas doc titled Rain Probe", canvas=True))
    assert result["canvas"] == {"title": "Rain Probe", "markdown": CANVAS_MD}
    assert result["answer"] == ('I will now generate the "Rain Probe" document for you.\n\n'
                                "I've put together those two short sentences in the document. Would you like to "
                                "expand on this imagery or adjust the tone?")  # the transcript's, without the chip
    assert (result["chatId"], result["imageCount"], "imageData" in result) == ("0000000000000001", 0, False)
    assert fake.opened_with == {"headless": True, "send": True, "download": False}
    clicks = [e for e in fake.events if e[0] in ("click", "insert")]
    assert clicks.index(("click", sel.CANVAS_ITEM)) < clicks.index(("insert", "Make a Canvas doc titled Rain Probe"))
    assert ("click", sel.CREATE_IMAGE_ITEM) not in clicks


def test_ask_with_canvas_leaves_the_tool_on_when_it_already_is(page):
    fake = canvas_page(page)
    fake.counts[sel.CANVAS_TOOL_CHIP] = 1
    assert run(fetch.ask("scene", canvas=True))["canvas"]["title"] == "Rain Probe"
    assert ("click", sel.CANVAS_ITEM) not in fake.events  # a click would turn it off


def test_the_transcript_gets_time_to_list_the_canvas_doc(page, monkeypatch):
    monkeypatch.setattr(fetch.asyncio, "sleep", fast_sleep)
    fake = canvas_page(page, read=[(OK, body("read_chat.txt")), (OK, body("read_chat_canvas.txt"))])
    assert run(fetch.ask("scene", canvas=True))["canvas"]["markdown"] == CANVAS_MD
    assert len([c for c in fake.calls if c[0] == "hNvQHb"]) == 2


def test_a_canvas_reply_without_a_doc_reads_the_transcript_once(page):
    fake = canvas_page(page, read=(OK, body("read_chat_image.txt")), canvases=0)
    fake.dom_text = "Here is a scene: rain on a roof."
    result = run(fetch.ask("scene", canvas=True))
    assert (result["canvas"], result["answer"]) == (None, "Here is a scene: rain on a roof.")  # transcript's is empty
    assert len([c for c in fake.calls if c[0] == "hNvQHb"]) == 1


def test_without_canvas_the_reply_is_read_off_the_page(page):
    fake = canvas_page(page)
    result = run(fetch.ask("scene"))
    assert (result["canvas"], result["answer"]) == (None, CANVAS_DOM)
    assert not [c for c in fake.calls if c[0] == "hNvQHb"] and ("click", sel.CANVAS_ITEM) not in fake.events


def test_ask_refuses_before_sending_when_the_canvas_tool_is_missing(page, monkeypatch):
    fake = canvas_page(page)

    async def no_tool(self, **kwargs):
        if self.selector == sel.CANVAS_ITEM:
            raise PlaywrightTimeout("Timeout 10000ms exceeded.")
        self.page.events.append(("click", self.selector))

    monkeypatch.setattr(Locator, "click", no_tool)
    with pytest.raises(RuntimeError, match="Canvas tool"):
        run(fetch.ask("scene", canvas=True))
    assert not fake.sent


# A cited reply: the page shows one chip per list item; the transcript (read_chat_cited_files.txt) has the sources.
CITED_CHAT = "https://gemini.google.com/app/00000000000000a1"
CITED_READ = key("hNvQHb", ["c_00000000000000a1", 10, None, 1, [0], [4], None, 1])
CITED_DOM = (f"outline.md: A lighthouse keeper finds a map in the lamp room. She sails out to follow it and comes back "
             f"changed.{parse.CHIP}\n\ncast.md: Mara keeps the light and Teo fixes boats.{parse.CHIP}")
CITED_CHIPS = [{"title": "MD", "aria": "View source details for citations from MD: outline.md and MD: outline.md. "
                                       "Press Enter to open sources dialog."},
               {"title": "MD", "aria": "View source details for citation from MD: cast.md. Press Enter to open sources "
                                       "dialog."}]


def cited_page(page, read):
    fake = page({CITED_READ: read}, channel=True, url_after_send=CITED_CHAT, dom_text=CITED_DOM,
                dom_key="model-response-message-contentr_00000000000000a2")
    fake.dom_chips = CITED_CHIPS
    return fake


def test_a_cited_answer_is_the_transcript_markdown_with_footnotes(page):
    from test_gemini_parse import FILES_CITATIONS, FILES_FOOTNOTED

    fake = cited_page(page, (OK, body("read_chat_cited_files.txt")))
    result = run(fetch.ask("Summarize each attached file, citing it"))
    assert (result["answer"], result["citations"]) == (FILES_FOOTNOTED, FILES_CITATIONS)
    assert CITED_READ in fake.calls
    cited_page(page, (OK, body("read_chat_cited_files.txt")))
    bare = run(fetch.ask("Summarize each attached file, citing it", footnotes=False))
    assert "[^" not in bare["answer"] and bare["answer"].endswith("Keep the sea quiet until the end.")
    assert bare["citations"] == FILES_CITATIONS


def test_without_the_transcript_the_chips_still_become_footnotes(page, monkeypatch):
    monkeypatch.setattr(fetch.asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(fetch, "TURN_SECONDS", 0)
    cited_page(page, (OK, body("rpc_error.txt")))  # reading the chat fails: the answer isn't lost
    result = run(fetch.ask("Summarize each attached file, citing it"))
    assert result["answer"] == (
        "outline.md: A lighthouse keeper finds a map in the lamp room. She sails out to follow it and comes back "
        "changed.[^1]\n\ncast.md: Mara keeps the light and Teo fixes boats.[^2]\n\n[^1]: outline.md\n[^2]: cast.md")
    assert result["citations"] == [{"n": 1, "label": "MD", "file": "outline.md"},
                                   {"n": 2, "label": "MD", "file": "cast.md"}]
    cited_page(page, (OK, body("read_chat.txt")))  # a transcript without this turn
    assert run(fetch.ask("again"))["citations"][1] == {"n": 2, "label": "MD", "file": "cast.md"}


def test_a_new_chats_failed_first_read_is_retried(page, monkeypatch):
    from test_gemini_parse import FILES_FOOTNOTED

    monkeypatch.setattr(fetch.asyncio, "sleep", fast_sleep)
    fake = cited_page(page, [(OK, body("rpc_error.txt")), (OK, body("read_chat_cited_files.txt"))])
    assert run(fetch.ask("Summarize each attached file, citing it"))["answer"] == FILES_FOOTNOTED
    assert fake.calls.count(CITED_READ) == 2


def test_only_login_and_headed_open_a_visible_chrome(monkeypatch):
    """Hidden-browser policy: every command runs headless unless --headed; only login shows a window. There is no
    visible fallback, and nothing else ever launches headed."""
    launches = []

    class Stop(Exception):
        pass

    @asynccontextmanager
    async def fake_open_page(*, headless, **kwargs):
        launches.append((headless, kwargs.get("sign_in", False)))
        raise Stop
        yield

    monkeypatch.setattr(fetch, "open_page", fake_open_page)
    commands = [fetch.status(), fetch.models(), fetch.chats(5), fetch.chat("c_0123456789abcdef"),
                fetch.ask("q", timeout=5)]
    for coro in commands:
        with pytest.raises((Stop, RuntimeError)):
            run(coro)
    assert launches == [(True, False)] * len(commands)
    launches.clear()
    with pytest.raises(Stop):
        run(fetch.models(headed=True))
    with pytest.raises(Stop):
        run(fetch.login(5))
    assert launches == [(False, False), (False, True)]
