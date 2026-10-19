"""Offline tests (from musecli) for muse's fetch.py with a scripted fake page instead of Chrome: sign-in states, the
answer wait in `ask`, the chat id after sending, chats (clicking sidebar rows), one chat, and login."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from conftest import FIXTURES
from webcli.core import browser, guard
from webcli.core.errors import AuthRequired, NotFound
from webcli.sites.muse import fetch
from webcli.sites.muse.selectors import SELECTORS

UUID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
UUID2 = "0f8fad5b-d9cb-469f-a165-70867728950e"
UUID3 = "a1b2c3d4-e5f6-0718-293a-4b5c6d7e8f90"
# A FakePage answers each look with the next of `snapshots` (the last one repeats); once `ask`
# presses Enter, it moves on to `after` the same way.
CHAT = {"path": "/", "login": False, "composer": True, "replies": 0, "lastText": "", "streaming": False,
        "stop": False, "approvals": 0}


def fixture(name: str):
    return json.loads((FIXTURES / "muse" / name).read_text())


def snap(**changes) -> dict:
    return {**CHAT, **changes}


class FakeLocator:
    def __init__(self, page: "FakePage", selector: str, index: int | None = None):
        self.page, self.selector, self.index = page, selector, index

    def filter(self, **_):
        return self

    @property
    def first(self):
        return self.nth(0)

    def nth(self, index: int):
        return FakeLocator(self.page, self.selector, index)

    def _key(self) -> str:
        return next((k for k, s in SELECTORS.items() if s == self.selector), self.selector)

    async def wait_for(self, **_):
        if self._key() in self.page.absent:
            raise type("TimeoutError", (Exception,), {})("timed out")

    async def is_visible(self) -> bool:
        return self._key() not in self.page.hidden

    async def click(self):
        self.page.actions.append(("click", self._key(), self.index))
        if self._key() == "thread_row":
            self.page.url = self.page.row_urls.get(self.index, self.page.url)

    async def fill(self, text: str):
        self.page.actions.append(("fill", text))

    async def press(self, key: str):
        self.page.actions.append(("press", key))
        self.page.snapshots = self.page.after or self.page.snapshots
        if self.page.url_after_send:
            self.page.url = self.page.url_after_send


class FakePage:
    def __init__(self, *, snapshots=(snap(),), after=(), lands_on=None, auth=(200, {}), rows=(), row_urls=None,
                 messages=(), turn=(), absent=(), hidden=(), url_after_send=None):
        self.snapshots, self.after, self.lands_on, self.auth = list(snapshots), list(after), lands_on, auth
        self.rows, self.messages, self.turn = list(rows), list(messages), list(turn)
        self.row_urls = {0: "https://muse.ai/", **(row_urls or {})}
        self.absent, self.hidden, self.url_after_send = set(absent), set(hidden), url_after_send
        self.url = "about:blank"
        self.actions: list = []
        self.context = SimpleNamespace(pages=[self])

    def is_closed(self) -> bool:
        return False

    async def goto(self, target, **_):
        self.actions.append(("goto", target))
        self.url = self.lands_on or target

    async def go_back(self):
        self.actions.append(("back",))
        self.url = "https://muse.ai/"

    def locator(self, selector):
        return FakeLocator(self, selector)

    async def evaluate(self, js, arg=None):
        if js == fetch.SNAPSHOT_JS:
            return self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]
        if js == fetch.AUTH_CHECK_JS:
            assert arg[0] == "/api/auth/check"
            return {"status": self.auth[0], "data": self.auth[1]}
        if js == fetch.ROWS_JS:
            return self.rows
        if js == fetch.MESSAGES_JS:
            return self.messages
        if js == fetch.TURN_JS:
            return self.turn[arg[1]:]
        raise AssertionError(f"unexpected script: {js[:60]}")


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(fetch, "POLL", 0.001)
    monkeypatch.setattr(fetch, "SETTLE_SECONDS", 0.05)
    monkeypatch.setattr(fetch, "STABLE_SECONDS", 0.02)
    monkeypatch.setattr(fetch, "NAV_SECONDS", 0.05)


@pytest.fixture
def page(monkeypatch):
    def use(**kwargs) -> FakePage:
        fake = FakePage(**kwargs)

        @asynccontextmanager
        async def fake_open_page(**opts):
            fake.opened_with = opts
            yield fake

        monkeypatch.setattr(fetch, "open_page", fake_open_page)
        return fake

    return use


def run(coro):
    return asyncio.run(coro)


def test_open_page_is_the_muse_policy_headless_unless_headed(monkeypatch):
    seen = []

    @asynccontextmanager
    async def site_page(policy, *, mode, send):
        seen.append((policy, mode, send))
        yield "page"

    async def go(**opts):
        async with fetch.open_page(**opts) as p:
            return p

    monkeypatch.setattr(browser, "site_page", site_page)
    run(go(headless=True))
    run(go(headless=True, send=True))
    run(go(headless=False))
    assert seen == [(fetch.POLICY, "headless", False), (fetch.POLICY, "headless", True),
                    (fetch.POLICY, "visible", False)]
    assert fetch.POLICY.sockets is not None and fetch.APP_URL == "https://muse.ai/"  # what core.capture opens


def test_signed_out_held_and_redirected_pages_need_login(page):
    page(snapshots=[snap(login=True, composer=False)])
    with pytest.raises(AuthRequired, match="Not signed in"):
        run(fetch.chats(10))
    page(snapshots=[snap(path="/access/waitlist", composer=False)])
    with pytest.raises(AuthRequired, match="waitlist page"):
        run(fetch.chats(10))
    page(lands_on="https://www.instagram.com/accounts/login/")
    with pytest.raises(AuthRequired, match="www.instagram.com"):
        run(fetch.chats(10))


def test_missing_composer_names_what_the_guard_blocked(page):
    page(snapshots=[snap(composer=False)])
    guard.state.blocked.extend(["POST muse.ai/api/falco/v2", "POST muse.ai/api/stefi op=ConversationsQuery",
                                "POST muse.ai/api/stefi op=ConversationsQuery"])
    with pytest.raises(RuntimeError) as caught:
        run(fetch.chats(10))
    message = str(caught.value)
    assert "composer never appeared" in message
    assert "The guard blocked: POST muse.ai/api/stefi op=ConversationsQuery." in message  # once, telemetry left out
    assert "falco" not in message and "python -m webcli.core.capture muse" in message


def test_status_states(page):
    page(auth=(200, {"name": "Test Owner"}))
    assert run(fetch.status()) == {"signedIn": True, "account": "Test Owner", "access": "ok"}
    page(auth=(401, {}))
    assert run(fetch.status())["signedIn"] is False
    page(auth=(0, {}))  # redirected to sign-in
    assert run(fetch.status())["signedIn"] is False
    page(auth=(200, {"authenticated": False}))
    assert run(fetch.status())["signedIn"] is False
    page(snapshots=[snap(login=True, composer=False)])
    assert run(fetch.status())["signedIn"] is False
    page(snapshots=[snap(path="/access/invite", composer=False)])
    assert run(fetch.status()) == {"signedIn": True, "account": None, "access": "invite"}
    fake = page(snapshots=[snap(composer=False)])  # a composer selector that misses can't hide a session
    assert run(fetch.status()) == {"signedIn": True, "account": None, "access": "ok"}
    assert fake.opened_with == {"headless": True}  # guarded, hidden, no send
    page(lands_on="https://auth.muse.ai/aymh/")  # never got back from the Meta session cycle
    assert run(fetch.status()) == fetch.signed_out()


def streaming(before: int, prompt: str):
    """Snapshots after sending: nothing yet, the prompt echoed, streaming, done."""
    return [snap(replies=before)] + [
        snap(replies=before, streaming=True),
        snap(replies=before + 1, lastText=prompt),
        snap(replies=before + 1, lastText="Here", streaming=True),
        snap(replies=before + 1, lastText="Here is the plan", streaming=True),
        snap(replies=before + 1, lastText="Here is the plan.", streaming=False),
    ]


def test_ask_new_chat(page):
    prompt = "Plan a weekend in Lisbon."
    turn = [{"id": "old", "role": "assistant", "text": "old", "links": []}] * 2 + [
        {"id": f"assistant-msg-{UUID2}", "role": "assistant", "text": "Assistant message: Here is the plan.", "links": [
            {"href": "https://www.visitlisboa.com/en", "text": "Visit Lisboa"},
            {"href": "https://muse.ai/library", "text": "library"}]}]
    fake = page(snapshots=[snap(replies=2)], after=streaming(2, prompt), turn=turn,
                url_after_send=f"https://muse.ai/thread/{UUID}")
    result = run(fetch.ask(prompt, timeout=5))
    assert result == {"chatId": UUID, "chatUrl": f"https://muse.ai/thread/{UUID}", "prompt": prompt,
                      "answer": "Here is the plan.",
                      "citations": [{"title": "Visit Lisboa", "url": "https://www.visitlisboa.com/en"}]}
    assert fake.opened_with == {"headless": True, "send": True}
    assert fake.actions == [("goto", "https://muse.ai/thread/new"), ("fill", prompt), ("press", "Enter")]


def test_ask_waits_while_the_answer_streams(page):
    # The text holds still for longer than STABLE_SECONDS, but the body still says it's streaming.
    page(after=[snap(replies=1, lastText="Thinking it over", streaming=True)],
         url_after_send=f"https://muse.ai/thread/{UUID}")
    with pytest.raises(TimeoutError) as caught:
        run(fetch.ask("Slow one", timeout=0.2))
    assert "No finished answer after 0.2s (16 chars so far)" in str(caught.value)
    assert f"llmcli muse chats show {UUID}" in str(caught.value)


def test_ask_chat_id_comes_from_the_url_after_sending(page):
    done = [snap(replies=1, lastText="pong")]
    turn = [{"id": "a", "role": "assistant", "text": "pong", "links": []}]
    page(after=done, turn=turn, url_after_send=f"https://muse.ai/thread/{UUID}")
    assert run(fetch.ask("ping", timeout=5))["chatId"] == UUID
    page(after=done, turn=turn)  # the new chat never got its /thread/<uuid>
    result = run(fetch.ask("ping", timeout=5))
    assert (result["chatId"], result["chatUrl"]) == (None, None)
    fake = page(after=done, turn=turn)
    result = run(fetch.ask("ping", chat_id="main", timeout=5))
    assert (result["chatId"], result["chatUrl"]) == ("main", "https://muse.ai/")  # stays on "/"
    assert fake.actions[0] == ("goto", "https://muse.ai/")


def test_ask_existing_chat_joins_a_multi_message_turn(page):
    turn = [{"id": "a1", "role": "assistant", "text": "Checking flights…", "links": []},
            {"id": "a2", "role": "assistant", "text": "Found two under $300.", "links": []}]
    fake = page(after=[snap(replies=1, lastText="Checking flights…"), snap(replies=2, lastText="Found two under $300.")],
                turn=turn)
    result = run(fetch.ask("Any flights?", chat_id=UUID, timeout=5))
    assert fake.actions[0] == ("goto", f"https://muse.ai/thread/{UUID}")
    assert result["chatId"] == UUID  # the URL never changed: the given id is kept
    assert result["answer"] == "Checking flights…\n\nFound two under $300."
    assert result["citations"] == []


def test_ask_counts_replies_only_after_the_history_has_loaded(page):
    # The composer shows first; the chat's two old replies render a moment later.
    history = [snap(), snap(replies=1, lastText="old one"), snap(replies=2, lastText="old two")]
    turn = [{"id": t, "role": "assistant", "text": t, "links": []} for t in ("old one", "old two", "New answer.")]
    page(snapshots=history, after=[snap(replies=3, lastText="New answer.")], turn=turn)
    assert run(fetch.ask("Next?", chat_id=UUID, timeout=5))["answer"] == "New answer."


def test_ask_never_approves(page):
    page(after=[snap(replies=1, lastText="I can send that email.", approvals=1)],
         url_after_send=f"https://muse.ai/thread/{UUID}")
    with pytest.raises(RuntimeError, match=f"never approves; decide at https://muse.ai/thread/{UUID}"):
        run(fetch.ask("Email Bob", timeout=5))


def test_ask_ignores_approval_and_stop_controls_that_were_already_showing(page):
    always = {"approvals": 1, "stop": True}  # e.g. an Approvals link and a "Stop task" button elsewhere
    page(snapshots=[snap(**always)], after=[snap(replies=1, lastText="Done.", **always)],
         turn=[{"id": "a", "role": "assistant", "text": "Done.", "links": []}])
    assert run(fetch.ask("Hi", timeout=5))["answer"] == "Done."


def test_ask_accepts_an_answer_that_repeats_the_prompt(page):
    # Replies are matched by data-message-role="assistant", so this "pong" is Muse's, not the echo.
    page(after=[snap(replies=1, lastText="pong")], turn=[{"id": "a", "role": "assistant", "text": "pong", "links": []}])
    assert run(fetch.ask("pong", timeout=5))["answer"] == "pong"


def test_ask_needs_a_new_reply(page):
    page(after=[snap(replies=2, lastText="old answer")], snapshots=[snap(replies=2, lastText="old answer")])
    with pytest.raises(TimeoutError, match="no reply seen"):
        run(fetch.ask("Next?", chat_id=UUID, timeout=0.1))


def test_chats_clicks_each_side_chat_for_its_id_and_comes_back(page):
    fake = page(rows=fixture("rows.json"), row_urls={1: f"https://muse.ai/thread/{UUID}",
                                                     2: f"https://muse.ai/thread/{UUID2}",
                                                     3: f"https://muse.ai/thread/{UUID3}"})
    rows = run(fetch.chats(3))
    assert rows == [
        {"id": "main", "title": "Main chat", "updated": None, "url": "https://muse.ai/"},
        {"id": UUID, "title": "Pong reply request", "updated": "2m", "url": f"https://muse.ai/thread/{UUID}"},
        {"id": UUID2, "title": "Trip to Lisbon", "updated": "Tue", "url": f"https://muse.ai/thread/{UUID2}"},
    ]
    assert fake.opened_with == {"headless": True}  # guarded, no send
    clicks = [a for a in fake.actions if a[0] == "click"]
    assert clicks == [("click", "thread_row", 1), ("click", "thread_row", 0),
                      ("click", "thread_row", 2), ("click", "thread_row", 0)]  # row 3 is past --limit
    assert fake.url == "https://muse.ai/"


def test_chats_limit_one_is_the_main_chat_without_clicking(page):
    fake = page(rows=fixture("rows.json"))
    assert [r["id"] for r in run(fetch.chats(1))] == ["main"]
    assert not any(a[0] == "click" for a in fake.actions)


def test_chats_goes_back_when_the_main_row_is_hidden_and_skips_repeats(page):
    fake = page(rows=fixture("rows.json"), hidden={"thread_row"},
                row_urls={1: f"https://muse.ai/thread/{UUID}", 2: f"https://muse.ai/thread/{UUID}",
                          3: f"https://muse.ai/thread/{UUID3}"})
    assert [r["id"] for r in run(fetch.chats(10))] == ["main", UUID, UUID3]
    assert fake.actions.count(("back",)) == 3


def test_chats_fails_when_a_row_click_goes_nowhere(page):
    page(rows=fixture("rows.json"), row_urls={1: "https://muse.ai/"})
    with pytest.raises(RuntimeError, match="'Pong reply request' in the sidebar didn't open a /thread/<id> page"):
        run(fetch.chats(5))
    page(rows=[], absent={"thread_row"})
    with pytest.raises(NotFound) as caught:
        run(fetch.chats(5))
    assert "webcli.core.capture muse" in caught.value.hint


def test_one_chat(page):
    fake = page(messages=fixture("thread.json"))
    chat = run(fetch.chat(UUID))
    assert fake.actions == [("goto", f"https://muse.ai/thread/{UUID}")]
    assert chat["id"] == UUID and chat["url"] == f"https://muse.ai/thread/{UUID}"
    assert [(m["id"], m["role"]) for m in chat["messages"]] == [
        ("user-msg-1", "user"), (f"assistant-msg-{UUID2}", "assistant"), ("user-msg-2", "user"), (None, "assistant")]
    assert chat["messages"][0]["text"] == "Plan a weekend in Lisbon under $800."  # "You:" label gone
    assert chat["messages"][1]["citations"][0]["url"] == "https://www.visitlisboa.com/en"


def test_the_main_chat_opens_at_home(page):
    fake = page(messages=fixture("thread.json"))
    chat = run(fetch.chat("main"))
    assert fake.actions == [("goto", "https://muse.ai/")]
    assert (chat["id"], chat["url"]) == ("main", "https://muse.ai/")


def test_one_chat_not_found(page):
    page(lands_on="https://muse.ai/", messages=fixture("thread.json"))
    with pytest.raises(NotFound, match="went to / instead"):
        run(fetch.chat(UUID))
    page(messages=[])
    with pytest.raises(NotFound, match="No messages"):
        run(fetch.chat(UUID))


@pytest.fixture
def login_page(monkeypatch):
    """login's own Chrome: browser.open_page(None, mode="visible"), recorded instead of launched."""
    async def no_sleep(_seconds):
        pass

    monkeypatch.setattr(fetch.asyncio, "sleep", no_sleep)

    def use(**kwargs) -> FakePage:
        fake = FakePage(**kwargs)

        @asynccontextmanager
        async def fake_open_page(policy, **opts):
            fake.opened_with = (policy, opts)
            yield fake

        monkeypatch.setattr(browser, "open_page", fake_open_page)
        return fake

    return use


def test_login_is_unguarded_visible_and_waits_for_full_access(login_page):
    fake = login_page(snapshots=[snap(login=True, composer=False), snap(path="/access/disclosure", composer=False),
                                 snap()], auth=(200, {"name": "Test Owner"}))
    states = []
    assert run(fetch.login(60, states.append)) == {"signedIn": True, "account": "Test Owner", "access": "ok",
                                                   "name": "Test Owner"}
    assert fake.opened_with == (None, {"mode": "visible"})  # plain Chrome for a person: no guard
    assert states == ["signing in"] and fake.actions == [("goto", "https://muse.ai/")]


def test_login_reports_a_hold_page_at_the_deadline(login_page):
    login_page(snapshots=[snap(path="/access/waitlist", composer=False)])
    with pytest.raises(AuthRequired, match="still shows its waitlist page"):
        run(fetch.login(0.05))
    login_page(snapshots=[snap(login=True, composer=False)])
    with pytest.raises(AuthRequired, match="Timed out after 0.05s waiting for Muse sign-in"):
        run(fetch.login(0.05))


node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


@node
def test_in_page_scripts_compile():
    scripts = {name: getattr(fetch, name) for name in dir(fetch) if name.endswith("_JS")}
    checks = "".join(f"new Function('return ' + {json.dumps(js)});\n" for js in scripts.values())
    subprocess.run(["node", "-e", checks], capture_output=True, text=True, timeout=30, check=True)
    assert sorted(scripts) == ["AUTH_CHECK_JS", "MESSAGES_JS", "ROWS_JS", "SNAPSHOT_JS", "TURN_JS"]


# A few DOM nodes, just enough for fetch._OUTER's textOf: attribute selectors, childNodes, display.
DOM_STUB = r"""
const hasAttr = (node, s) => s.split(', ').some((one) => {
  const m = one.match(/^\[([\w-]+)(?:="([^"]*)")?\]$/);
  return !!m && m[1] in node.attrs && (m[2] === undefined || node.attrs[m[1]] === m[2]);
});
const text = (t) => ({nodeType: 3, textContent: t});
const el = (tagName, attrs, display, ...childNodes) => {
  const node = {nodeType: 1, tagName, attrs, display, childNodes,
    matches: (s) => hasAttr(node, s),
    querySelector: (s) => { for (const k of childNodes) { if (k.nodeType !== 1) continue;
      const hit = k.matches(s) ? k : k.querySelector(s); if (hit) return hit; } return null; },
    get innerText() { return childNodes.map((k) => k.nodeType === 3 ? k.textContent : k.innerText).join(''); }};
  return node;
};
const getComputedStyle = (node) => ({display: node.display});
const sr = (t) => el('SPAN', {'data-message-accessibility-surrogate': ''}, 'block', text(t));
"""


@node
def test_message_text_leaves_out_screen_reader_and_search_copies():
    cases = """[
      el('DIV', {'data-message-role': 'user'}, 'block', sr('You:'),
         el('DIV', {}, 'block', text('Say '), el('B', {}, 'inline', text('pong')), text(' please'))),
      el('DIV', {'data-message-role': 'assistant'}, 'block', sr('Assistant message: '),
         el('DIV', {'data-hatch-assistant-message-body': '', 'data-hatch-markdown-streaming': 'false'}, 'block',
            text('pong'))),
      el('P', {}, 'block', text('Hello '), el('SPAN', {'data-message-search-surrogate': ''}, 'inline', text('Hello world.')),
         el('B', {}, 'inline', text('world')), text('.')),
      el('DIV', {}, 'block', sr('You:'), el('P', {}, 'block', text('one')), el('P', {}, 'block', text('two'))),
      el('DIV', {}, 'block', sr('You:'), text('a'), el('BR', {}, 'inline'), text('b'),
         el('DIV', {}, 'none', text('hidden'))),
      el('DIV', {}, 'block', text('no surrogate here')),
    ]"""
    script = f"const sel = {json.dumps(SELECTORS)};\n{DOM_STUB}\n{fetch._OUTER}\n" \
             f"console.log(JSON.stringify({cases}.map((m) => textOf(m).trim())));"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=True).stdout
    assert json.loads(out) == ["Say pong please", "pong", "Hello world.", "one\n\ntwo", "a\nb", "no surrogate here"]
