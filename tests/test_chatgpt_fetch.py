"""chatgpt fetch against a fake page: how API answers map to errors, status / models / chats / chat, cap notices, image
downloads, sign-in polling, and the refusals `ask` makes before anything is typed or sent. No Chrome:
browser.site_page yields the fake, and the fake has no locator, keyboard, or click, so a test that reaches the composer
fails.

Ported from chatgpt-web-cli's overflow/chatgptwebclitests (tests/test_fetch.py, e157dde): webcli's fetch is the same
code, opening its page through webcli.core.browser (site_page, and open_page for sign-in)."""

from __future__ import annotations

import asyncio
import base64
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from webcli.core import browser
from webcli.core.errors import AuthRequired, Challenged
from webcli.sites.chatgpt import fetch

FIXTURES = Path(__file__).parent / "fixtures" / "chatgpt"
CHAT = "00000000-0000-4000-8000-000000000001"
SIGNED_IN = {"status": 200, "signedIn": True, "email": "owner.name@example.com", "name": "Owner", "plan": "plus"}


def ok(body) -> dict:
    return {"status": 200, "contentType": "application/json", "body": json.dumps(body)}


class Page:
    """Answers the page.evaluate scripts fetch runs. api maps a path prefix to one answer or a list (one per call)."""

    def __init__(self, *, url="https://chatgpt.com/", session=None, api=None, notices=None, tail="", downloads=None):
        self.url, self.session, self.api = url, session or SIGNED_IN, api or {}
        self.notices, self.tail, self.downloads = notices or [], tail, downloads or []
        self.paths: list[str] = []

    async def evaluate(self, script, arg=None):
        if script == fetch.SESSION_JS:
            return self.session
        if script == fetch.API_JS:
            self.paths.append(arg["path"])
            for prefix, answer in self.api.items():
                if arg["path"].startswith(prefix):
                    return answer.pop(0) if isinstance(answer, list) else answer
            return {"status": 404, "contentType": "", "body": ""}
        if script == fetch.NOTICES_JS:
            if isinstance(self.notices, Exception):
                raise self.notices
            return self.notices
        if script == fetch.TAIL_JS:
            if isinstance(self.tail, Exception):
                raise self.tail
            return self.tail
        if script == fetch.DOWNLOAD_JS:
            self.paths.append(arg["path"])
            return self.downloads.pop(0)
        raise AssertionError(f"unexpected script: {script[:60]!r}")


@pytest.fixture
def opened(monkeypatch):
    """Route with_app to a fake page; returns the list of (mode, url, send) it was opened with."""
    seen: list[tuple] = []
    state = {"page": Page()}

    @asynccontextmanager
    async def site_page(policy, *, mode, url=None, send=False, **_options):
        assert policy is fetch.POLICY  # every command page runs under ChatGPT's request guard
        seen.append((mode, url, send))
        yield state["page"]

    monkeypatch.setattr(browser, "site_page", site_page)
    seen_page = state
    return seen, seen_page


def go(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize(("answer", "error", "message"), [
    ({"status": 403, "cfMitigated": "challenge", "body": ""}, Challenged, "(page Chrome)"),
    ({"status": 403, "body": ""}, AuthRequired, "ChatGPT refused /backend-api/models (HTTP 403)"),
    ({"status": 401, "body": ""}, AuthRequired, "(HTTP 401); the session is missing or expired."),
    ({"status": 0, "body": ""}, AuthRequired, "(HTTP 0)"),  # a redirect (to sign-in) is opaque
    ({"status": 404, "body": ""}, RuntimeError, "/backend-api/models failed: HTTP 404. ChatGPT's API may have"),
    ({"status": -1, "body": "TimeoutError"}, RuntimeError, "/backend-api/models failed in the page (TimeoutError)."),
    ({"status": 502, "body": "<html>"}, RuntimeError, "failed: HTTP 502."),
    ({"status": 200, "body": "<html>"}, RuntimeError, "answered something that isn't JSON."),
])
def test_api_json_maps_answers_to_errors(answer, error, message):
    page = Page(api={"/backend-api/models": answer})
    with pytest.raises(error) as raised:
        go(fetch.api_json(page, "/backend-api/models?iim=false"))
    assert message in str(raised.value)
    assert "iim" not in str(raised.value)  # the query string never reaches a message


def test_api_json_404_on_an_item_is_not_found_and_200_is_json():
    page = Page(api={"/backend-api/conversation/": {"status": 404, "body": ""}, "/ok": ok({"a": [1]})})
    with pytest.raises(fetch.NotFound) as raised:
        go(fetch.api_json(page, f"/backend-api/conversation/{CHAT}", item=f"Chat {CHAT}"))
    assert (str(raised.value), raised.value.hint) == (f"Chat {CHAT} not found.",
                                                      "If every chat fails like this, check `llmcli chatgpt status`.")
    assert go(fetch.api_json(page, "/ok")) == {"a": [1]}


def test_session_summary_raises_on_a_challenge_and_account_masks_the_email():
    with pytest.raises(Challenged):
        go(fetch.session_summary(Page(session={"status": 403, "cfMitigated": "challenge"})))
    assert go(fetch.session_summary(Page())) == SIGNED_IN
    assert fetch._account(SIGNED_IN) == {"signedIn": True, "plan": "plus", "email": "o***@example.com"}
    assert fetch._account({"status": 200, "signedIn": False, "email": "x@y.z"}) == fetch.signed_out()


@pytest.mark.parametrize(("url", "session", "expected"), [
    ("https://chatgpt.com/", SIGNED_IN, {"signedIn": True, "plan": "plus", "email": "o***@example.com"}),
    ("https://chatgpt.com/", {"status": 200, "signedIn": False}, {"signedIn": False, "plan": None, "email": None}),
    ("https://auth.openai.com/log-in", SIGNED_IN, {"signedIn": False, "plan": None, "email": None}),
    ("https://chatgpt.com/auth/login", SIGNED_IN, {"signedIn": False, "plan": None, "email": None}),
])
def test_status(opened, url, session, expected):
    seen, state = opened
    state["page"] = Page(url=url, session=session)
    assert go(fetch.status()) == expected
    assert seen == [("headless", fetch.APP_URL, False)]


def test_status_headed_opens_a_visible_chrome(opened):
    seen, _state = opened
    go(fetch.status(headed=True))
    assert seen == [("visible", fetch.APP_URL, False)]


def test_models_reads_the_list_and_needs_a_session(opened):
    _seen, state = opened
    catalog = json.loads((FIXTURES / "models.json").read_text())
    state["page"] = page = Page(api={"/backend-api/models": ok(catalog)})
    rows = go(fetch.models())
    assert len(rows) == 19 and page.paths == ["/backend-api/models?iim=false&is_gizmo=false"]
    state["page"] = Page(session={"status": 200, "signedIn": False})
    with pytest.raises(AuthRequired, match="Not signed in to ChatGPT."):
        go(fetch.models())
    state["page"] = Page(url="https://auth.openai.com/log-in")
    with pytest.raises(AuthRequired, match="ChatGPT sent Chrome to auth.openai.com/log-in;"):
        go(fetch.models())


def item(n: int) -> dict:
    return {"id": f"00000000-0000-4000-8000-{n:012d}", "title": f"chat {n}", "update_time": 1760000000 + n}


def test_chats_pages_by_offset_and_drops_repeats(opened):
    _seen, state = opened
    first = {"items": [item(n) for n in range(28)], "total": 32}
    second = {"items": [item(27), item(28), item(29)], "total": 32}  # a new chat pushed 27 down between the reads
    third = {"items": [item(30)], "total": 32}
    state["page"] = page = Page(api={"/backend-api/conversations": [ok(first), ok(second), ok(third)]})
    rows = go(fetch.chats(31))
    assert [r["id"][-2:] for r in rows] == [f"{n:02d}" for n in range(31)]
    assert page.paths == ["/backend-api/conversations?offset=0&limit=28&order=updated",
                          "/backend-api/conversations?offset=28&limit=3&order=updated",
                          "/backend-api/conversations?offset=31&limit=1&order=updated"]
    assert rows[0] == {"id": item(0)["id"], "title": "chat 0", "updated": "2025-10-09T08:53:20Z", "created": None,
                       "pinned": False, "url": f"https://chatgpt.com/c/{item(0)['id']}"}


def test_chats_stops_at_the_total_and_at_an_empty_page(opened):
    _seen, state = opened
    state["page"] = page = Page(api={"/backend-api/conversations": [ok({"items": [item(1), item(2)], "total": 2})]})
    assert len(go(fetch.chats(50))) == 2 and len(page.paths) == 1
    state["page"] = page = Page(api={"/backend-api/conversations": [ok({"items": [item(1)] * 1, "total": 99}),
                                                                    ok({"items": [], "total": 99})]})
    assert len(go(fetch.chats(50))) == 1 and len(page.paths) == 2
    state["page"] = Page(api={"/backend-api/conversations": [ok({"items": [item(n) for n in range(5)],
                                                                 "total": 5})]})
    assert len(go(fetch.chats(3))) == 3


def test_chat_returns_the_transcript_and_not_found(opened):
    _seen, state = opened
    record = json.loads((FIXTURES / "record_text.json").read_text())
    state["page"] = Page(api={f"/backend-api/conversation/{CHAT}": ok(record)})
    chat = go(fetch.chat(CHAT))
    assert (chat["chatId"], chat["url"], chat["title"]) == (CHAT, f"https://chatgpt.com/c/{CHAT}", record.get("title"))
    assert [m["role"] for m in chat["messages"]] == ["user", "assistant"]
    state["page"] = Page()
    with pytest.raises(fetch.NotFound, match=f"Chat {CHAT} not found."):
        go(fetch.chat(CHAT))


def test_check_limit_raises_on_a_cap_notice_only():
    page = Page(notices=["Saved to memory", "You've reached our limit of messages per hour.  Try again\nafter 4:22 PM."])
    with pytest.raises(fetch.RateLimited) as raised:
        go(fetch._check_limit(page, sent=True))
    error = raised.value
    assert error.message == "You've reached our limit of messages per hour. Try again after 4:22 PM."
    assert error.sent is True and error.resets_at.startswith("20") and "T16:22:00" in error.resets_at
    go(fetch._check_limit(Page(notices=["Saved to memory"]), sent=False))
    go(fetch._check_limit(Page(notices=RuntimeError("page closed")), sent=False))  # a failed check: no notice


def test_check_stalled_looks_past_the_prompt_for_a_cap_notice():
    prompt = "Explain what you've reached your limit means"
    go(fetch._check_stalled(Page(tail=f"Title\n{prompt}\nChatGPT can make mistakes."), prompt))  # only the prompt
    with pytest.raises(fetch.RateLimited) as raised:
        go(fetch._check_stalled(Page(tail=f"{prompt}\nYou've hit your usage cap. Try again tomorrow.\n"), prompt))
    assert raised.value.message == "You've hit your usage cap. Try again tomorrow." and raised.value.sent is True
    go(fetch._check_stalled(Page(tail=RuntimeError("closed")), prompt))


def test_download_tries_each_route_and_reports_every_distinct_failure():
    image = {"fileId": "file_00/x y", "width": 2, "height": 3, "bytes": 6}
    good = {"status": 200, "type": "image/png", "b64": base64.b64encode(b"\x89PNG..").decode()}
    page = Page(downloads=[{"status": 404, "error": "no download link"}, good])
    data = go(fetch._download(page, CHAT, image))
    assert data == {"data": b"\x89PNG..", "sourceWidth": 2, "sourceHeight": 3, "sourceBytes": 6}
    assert page.paths[0] == (f"/backend-api/files/download/file_00%2Fx%20y?conversation_id={CHAT}"
                             "&download_intent=true&inline=false")
    page = Page(downloads=[{"status": 404, "error": "no download link"}] * 4 + [{"status": -1, "error": "TypeError"}])
    with pytest.raises(RuntimeError) as raised:
        go(fetch._download(page, CHAT, image))
    assert str(raised.value) == ("Downloading image file_00/x y failed (HTTP 404 no download link; HTTP -1 TypeError). "
                                 + fetch.SENT_HINT)


def test_attachments_come_from_the_user_message_on_the_branch():
    record = {"current_node": "b", "mapping": {
        "a": {"message": {"id": "u1", "author": {"role": "user"}, "metadata": {"attachments": [
            {"name": "notes.txt", "size": 5, "mime_type": "text/plain", "id": "file-x"}, "junk"]}}, "parent": None},
        "b": {"message": {"id": "r1", "author": {"role": "assistant"}}, "parent": "a"}}}
    assert fetch._attachments(record, "u1") == [{"name": "notes.txt", "bytes": 5, "mime": "text/plain"}]
    assert fetch._attachments(record, "r1") == [] and fetch._attachments(record, "nope") == []
    assert fetch._attachments(None, None) == []


@pytest.mark.parametrize(("turn", "want_images", "ended_ago", "settled", "done"), [
    (None, False, None, False, False),
    ({"endTurn": False, "statuses": ["finished_successfully"], "images": []}, False, None, False, False),
    ({"endTurn": True, "statuses": ["in_progress", "finished_successfully"], "images": []}, False, None, False, False),
    ({"endTurn": True, "statuses": ["finished_successfully"], "images": []}, False, 0, True, True),
    ({"endTurn": True, "statuses": ["finished_successfully"], "images": []}, True, 0, True, False),  # image may lag
    ({"endTurn": True, "statuses": ["finished_successfully"], "images": []}, True, fetch.IMAGE_GRACE + 1, True, True),
    ({"endTurn": True, "statuses": ["finished_successfully"], "images": [{}]}, True, 0, True, True),
])
def test_settled_and_done(monkeypatch, turn, want_images, ended_ago, settled, done):
    monkeypatch.setattr(fetch.time, "monotonic", lambda: 1000.0)
    ended_at = None if ended_ago is None else 1000.0 - ended_ago
    assert fetch._settled(turn) is settled
    assert fetch._done(turn, want_images, ended_at) is done


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_ask_refuses_a_bad_timeout_before_opening_chrome(opened, timeout):
    seen, _state = opened
    with pytest.raises(ValueError, match="--timeout must be a finite positive number of seconds."):
        go(fetch.ask("q", timeout=timeout))
    assert seen == []


def test_ask_timeout_names_the_stage_and_says_nothing_was_submitted(monkeypatch):
    async def slow(work, **_kwargs):
        await asyncio.sleep(5)

    monkeypatch.setattr(fetch, "with_app", slow)
    with pytest.raises(RuntimeError) as raised:
        go(fetch.ask("q", timeout=0.05))
    assert str(raised.value) == ("Timed out after 0.05s while opening Chrome and waiting for the profile lock "
                                 "(raise --timeout). The prompt was not submitted.")


def catalog_page(**kwargs) -> Page:
    catalog = json.loads((FIXTURES / "models.json").read_text())
    return Page(api={"/backend-api/models": ok(catalog), **kwargs.pop("api", {})}, **kwargs)


@pytest.mark.parametrize(("model", "message", "hint"), [
    ("no-such-model", "No model matches 'no-such-model'. Available: gpt-5-6,", "run: llmcli chatgpt models"),
    ("sol", "'sol' matches more than one model:", "run: llmcli chatgpt models"),
    ("research", "research isn't in ChatGPT's model picker, so llmcli can't choose it.", "Pick a slug the picker"),
])
def test_ask_refuses_a_model_it_cannot_pick_before_touching_the_composer(opened, model, message, hint):
    seen, state = opened
    state["page"] = catalog_page()
    with pytest.raises(fetch.NotFound) as raised:
        go(fetch.ask("q", model=model, timeout=5))
    assert message in str(raised.value) and raised.value.hint.startswith(hint)
    assert seen == [("headless", fetch.APP_URL, True)]


def test_ask_into_a_missing_chat_is_not_found_before_anything_is_sent(opened):
    seen, state = opened
    state["page"] = page = catalog_page()
    with pytest.raises(fetch.NotFound, match=f"Chat {CHAT} not found."):
        go(fetch.ask("q", chat_id=CHAT, timeout=5))
    assert seen == [("headless", f"https://chatgpt.com/c/{CHAT}", True)]
    assert page.paths == [f"/backend-api/conversation/{CHAT}"]


def test_ask_signed_out_is_auth_required(opened):
    _seen, state = opened
    state["page"] = catalog_page(session={"status": 200, "signedIn": False})
    with pytest.raises(AuthRequired):
        go(fetch.ask("q", timeout=5))


# login: a visible window, polled gently.

class Tab:
    def __init__(self, url, title="ChatGPT", session=None, closed=False, fails=False):
        self.url, self._title, self.session, self.closed, self.fails = url, title, session, closed, fails

    def is_closed(self):
        return self.closed

    async def title(self):
        if self.fails:
            raise RuntimeError("navigating")
        return self._title

    async def evaluate(self, script):
        assert script == fetch.SESSION_JS
        if isinstance(self.session, Exception):
            raise self.session
        return self.session


class LoginPage:
    def __init__(self, polls):
        self.polls, self.gone_to = list(polls), []
        self.context = self

    @property
    def pages(self):
        return self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]

    async def goto(self, url, **_kwargs):
        self.gone_to.append(url)
        raise TimeoutError("slow redirect")  # a failed goto keeps waiting


@pytest.fixture
def login_page(monkeypatch):
    opened_with = []
    holder = {}

    @asynccontextmanager
    async def open_page(policy, **kwargs):
        opened_with.append({"policy": policy, **kwargs})
        yield holder["page"]

    monkeypatch.setattr(browser, "open_page", open_page)
    monkeypatch.setattr(fetch, "LOGIN_POLL", 0)
    return holder, opened_with


def test_login_waits_through_cloudflare_and_returns_the_signed_in_tab(login_page):
    holder, opened_with = login_page
    signed_in = {"status": 200, "signedIn": True, "email": "owner@example.com", "plan": "pro"}
    holder["page"] = page = LoginPage([
        [Tab("https://chatgpt.com/", title="Just a moment...")],
        [Tab("https://auth.openai.com/log-in", title="Log in"), Tab("https://chatgpt.com/", fails=True)],
        [Tab("https://chatgpt.com/", session=RuntimeError("navigated")), Tab("https://chatgpt.com/", session=signed_in)],
    ])
    states = []
    assert go(fetch.login(30, states.append)) == {"signedIn": True, "plan": "pro", "email": "o***@example.com"}
    assert states == ["cloudflare", "signing in"]
    assert opened_with == [{"policy": None, "mode": "visible", "match_headless": True}] and page.gone_to == [fetch.LOGIN_URL]


def test_login_fails_when_the_window_closes_or_time_runs_out(login_page, monkeypatch):
    holder, _opened = login_page
    holder["page"] = LoginPage([[Tab("https://chatgpt.com/", closed=True)]])
    with pytest.raises(AuthRequired, match="The Chrome window closed before sign-in finished."):
        go(fetch.login(30))
    holder["page"] = LoginPage([[Tab("https://chatgpt.com/", session={"status": 200, "signedIn": False})]])
    with pytest.raises(AuthRequired, match="Timed out after 0s waiting for ChatGPT sign-in."):
        go(fetch.login(0))
