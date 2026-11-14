"""chatgpt fetch._ask_impl, the send flow, against a fake composer page: what is typed, that the prompt goes out once (Enter
only when the click sent nothing), what is read back, and every way it stops, saying whether the prompt was sent.
No Chrome: the fake page answers the page scripts and plays ChatGPT's part when Send is clicked.

Ported from chatgpt-web-cli's overflow/chatgptwebclitests (tests/test_ask_flow.py, 62d99bf): webcli's send flow is
the same code, with the model guard's state in webcli.core.guard.state."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import pytest

from webcli.core import guard
from webcli.sites.chatgpt import fetch, parse
from webcli.sites.chatgpt import selectors as sel

CATALOG = json.loads((Path(__file__).parent / "fixtures" / "chatgpt" / "models.json").read_text())
CHAT = "00000000-0000-4000-8000-0000000000aa"
CONVERSATION = "https://chatgpt.com/backend-api/f/conversation"
SESSION = {"status": 200, "signedIn": True, "email": "o@example.com", "plan": "plus"}


def chat_record(*, answer="pong", model="gpt-6-thinking", done=True, reply=True, images=0) -> dict:
    """The chat record ChatGPT keeps: the user's message msg-1 and (reply=True) the assistant's answer."""
    mapping = {
        "root": {"message": None, "parent": None},
        "u": {"parent": "root", "message": {
            "id": "msg-1", "author": {"role": "user"}, "content": {"content_type": "text", "parts": ["q"]},
            "metadata": {"attachments": [{"name": "notes.txt", "size": 5, "mime_type": "text/plain"}]}}},
    }
    leaf = "u"
    for n in range(images):
        mapping[f"t{n}"] = {"parent": leaf, "message": {"id": f"t{n}", "author": {"role": "tool"}, "content": {
            "content_type": "multimodal_text", "parts": [{"content_type": "image_asset_pointer", "width": 2,
                                                          "height": 2, "size_bytes": 4,
                                                          "asset_pointer": f"sediment://file_{n}"}]}}}
        leaf = f"t{n}"
    if reply:
        mapping["a"] = {"parent": leaf, "message": {
            "id": "a1", "author": {"role": "assistant"}, "content": {"content_type": "text", "parts": [answer]},
            "status": "finished_successfully" if done else "in_progress", "end_turn": done, "recipient": "all",
            "metadata": {"model_slug": model}}}
        leaf = "a"
    return {"title": "T", "current_node": leaf, "mapping": mapping, "default_model_slug": "gpt-6"}


class Request:
    def __init__(self, method, url, post_data=None):
        self.method, self.url, self.post_data = method, url, post_data


class Response:
    def __init__(self, url, status, request, body=""):
        self.url, self.status, self.request, self.body = url, status, request, body
        self.ok = 200 <= status < 300

    async def text(self):
        return self.body

    async def finished(self):
        return None


class Context:
    def __init__(self):
        self.handlers: dict[str, list] = {}

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        self.handlers[event].remove(handler)

    def emit(self, event, obj):
        for handler in list(self.handlers.get(event, [])):
            handler(obj)


class Keyboard:
    def __init__(self, page):
        self.page = page

    async def press(self, key):
        self.page.keys.append(key)

    async def insert_text(self, text):
        self.page.typed.append(text)


class Locator:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector

    @property
    def first(self):
        return self

    async def count(self):
        return 0  # no dialog, no open menu

    async def wait_for(self, state=None, timeout=None):
        if self.selector == sel.PROMPT_INPUT and not self.page.prompt_box:
            raise TimeoutError("no prompt box")

    async def click(self, timeout=None):
        self.page.clicks.append(self.selector)
        if self.selector == sel.SEND_BUTTON:
            await self.page.on_send_click()

    async def focus(self):
        pass

    async def press(self, key):
        self.page.keys.append(key)
        if self.selector == sel.PROMPT_INPUT and key == "Enter":
            self.page.send()

    async def set_input_files(self, files, timeout=None):
        self.page.attached = list(files)
        if self.page.uploads:
            for n, _name in enumerate(files):
                request = Request("POST", f"https://chatgpt.com/backend-api/files/file-{n}/uploaded")
                self.page.context.emit("response", Response(request.url, 200, request))


class Composer:
    """A signed-in chatgpt.com page. On Send it plays ChatGPT: the prompt request (body model = page_model), then
    its response (status/body), then the page moves to the chat. records: what each read of the chat record returns
    (the last one repeats)."""

    def __init__(self, *, click="sends", status=200, body="", respond=True, send_ready=True, notices=(), tail="",
                 uploads=True, records=None, page_model="gpt-6-thinking", moves_to_chat=True, prompt_box=True):
        self.url = "https://chatgpt.com/"
        self.context, self.keyboard = Context(), Keyboard(self)
        self.click, self.status, self.body, self.respond = click, status, body, respond
        self.send_ready, self.notices, self.tail, self.uploads = send_ready, list(notices), tail, uploads
        self.records = records or [chat_record()]
        self.page_model, self.moves_to_chat, self.prompt_box = page_model, moves_to_chat, prompt_box
        self.keys: list[str] = []
        self.typed: list[str] = []
        self.clicks: list[str] = []
        self.attached: list[str] = []
        self.sent = 0
        self.models_at_send: list = []
        self.reads: list[str] = []

    def locator(self, selector):
        return Locator(self, selector)

    async def wait_for_function(self, script, arg=None, timeout=None):
        if not self.send_ready:
            raise TimeoutError("Send stayed disabled")

    def send(self):
        self.sent += 1
        self.models_at_send.append(guard.state.expect_models)
        body = {"action": "next", "model": self.page_model, "messages": [{"id": "msg-1"}]}
        request = Request("POST", CONVERSATION, json.dumps(body))
        self.context.emit("request", request)
        if self.respond:
            self.context.emit("response", Response(CONVERSATION, self.status, request, self.body))
            if self.moves_to_chat:
                self.url = f"https://chatgpt.com/c/{CHAT}"

    async def on_send_click(self):
        if self.click == "fails":  # covered by an overlay: nothing goes out
            raise TimeoutError("click intercepted")
        if self.click == "silent":  # the click lands but the page sends nothing
            return
        if self.click == "blocked":  # the guard aborts a prompt naming another model
            self.models_at_send.append(guard.state.expect_models)
            guard.state.blocked.append("POST chatgpt.com/backend-api/f/conversation")
            return
        self.send()
        if self.click == "sends, then times out":
            raise TimeoutError("click timed out after sending")

    async def evaluate(self, script, arg=None):
        if script == fetch.SESSION_JS:
            return SESSION
        if script == fetch.NOTICES_JS:
            return self.notices
        if script == fetch.TAIL_JS:
            return self.tail
        if script == fetch.DOWNLOAD_JS:
            return {"status": 200, "type": "image/png", "b64": base64.b64encode(b"\x89PNG").decode()}
        if script == fetch.API_JS:
            path = arg["path"]
            self.reads.append(path.split("?")[0])
            if path.startswith("/backend-api/models"):
                return {"status": 200, "body": json.dumps(CATALOG)}
            if path == f"/backend-api/conversation/{CHAT}":
                record = self.records.pop(0) if len(self.records) > 1 else self.records[0]
                return {"status": 200, "body": json.dumps(record)}
            return {"status": 404, "body": ""}
        raise AssertionError(f"unexpected script {script[:50]!r}")


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(fetch, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(fetch, "SEND_SECONDS", 0.3)
    monkeypatch.setattr(fetch, "UPLOAD_SECONDS", 0.3)
    monkeypatch.setattr(fetch, "STALL_SECONDS", 0)
    monkeypatch.setattr(guard.state, "blocked", [])
    monkeypatch.setattr(guard.state, "expect_models", None)

    async def picked(page, choice):  # the picker itself is a UI walk; here it "takes"
        page.picked = choice

    monkeypatch.setattr(fetch, "_pick_model", picked)


def ask(page, prompt="Reply with exactly: pong", *, timeout=3.0, model=None, chat_id=None, files=(),
        want_images=False):
    async def main():
        deadline = asyncio.get_running_loop().time() + timeout
        return await fetch._ask_impl(page, prompt, model=model, chat_id=chat_id, files=list(files),
                                     want_images=want_images, deadline=deadline, progress={})
    return asyncio.run(main())


def no_listeners(page) -> bool:
    return all(not handlers for handlers in page.context.handlers.values())


def test_the_prompt_is_typed_sent_once_and_read_back_from_the_record():
    page = Composer()
    out = ask(page, "line one\n\nline three")
    assert page.typed == ["line one", "line three"]
    assert page.keys == ["ControlOrMeta+A", "Backspace", "Shift+Enter", "Shift+Enter"]  # a cleared box, no Enter
    assert page.sent == 1 and page.clicks.count(sel.SEND_BUTTON) == 1
    assert out == {"chatId": CHAT, "chatUrl": f"https://chatgpt.com/c/{CHAT}", "requestedModel": None,
                   "servedModel": "gpt-6-thinking", "servedModelDisplay": "GPT-6 Thinking", "fallback": False,
                   "fallbackFrom": None, "fallbackTo": None, "prompt": "line one\n\nline three", "answer": "pong",
                   "imageCount": 0, "attachments": [{"name": "notes.txt", "bytes": 5, "mime": "text/plain"}],
                   "citations": []}
    assert page.models_at_send == [None] and no_listeners(page)


def test_a_click_that_sent_and_then_timed_out_is_never_followed_by_enter():
    page = Composer(click="sends, then times out")
    assert ask(page)["answer"] == "pong"
    assert page.sent == 1 and "Enter" not in page.keys


def test_enter_sends_only_when_the_click_sent_nothing():
    page = Composer(click="fails")
    assert ask(page)["answer"] == "pong"
    assert page.sent == 1 and page.keys.count("Enter") == 1


def test_a_reply_still_being_written_is_polled_until_it_settles():
    page = Composer(records=[chat_record(reply=False), chat_record(done=False, answer="po"), chat_record()])
    assert ask(page)["answer"] == "pong"
    assert page.reads.count(f"/backend-api/conversation/{CHAT}") == 3


def test_continuing_a_chat_checks_it_exists_first_and_reads_the_new_turn():
    page = Composer(moves_to_chat=False)
    page.url = f"https://chatgpt.com/c/{CHAT}"
    out = ask(page, chat_id=CHAT)
    assert out["chatId"] == CHAT and page.reads[:2] == [f"/backend-api/conversation/{CHAT}", "/backend-api/models"]


def test_m_sets_the_guard_to_the_pickers_slugs_and_reports_a_fallback():
    page = Composer(records=[chat_record(model="gpt-5-6-thinking")])
    out = ask(page, model="GPT-6-Thinking")
    assert page.picked["sends"] == ["gpt-6-thinking"] and page.models_at_send == [frozenset({"gpt-6-thinking"})]
    assert (out["requestedModel"], out["servedModel"], out["servedModelDisplay"]) == (
        "gpt-6-thinking", "gpt-5-6-thinking", "GPT-5.6 Sol Thinking")
    assert (out["fallback"], out["fallbackFrom"], out["fallbackTo"]) == (True, "gpt-6-thinking", "gpt-5-6-thinking")
    assert guard.state.expect_models == frozenset({"gpt-6-thinking"})  # page_at resets it when the page closes


def test_an_auto_slug_served_as_its_variant_is_not_a_fallback():
    page = Composer(page_model="gpt-6", records=[chat_record(model="gpt-6-instant")])
    out = ask(page, model="gpt-6")
    assert page.models_at_send == [frozenset({"gpt-6-instant", "gpt-6"})] and out["fallback"] is False


def test_image_replies_download_each_image_once_with_want_images():
    page = Composer(records=[chat_record(answer="", images=2)])
    out = ask(page, want_images=True)
    assert out["imageCount"] == 2
    assert out["imageData"] == [{"data": b"\x89PNG", "sourceWidth": 2, "sourceHeight": 2, "sourceBytes": 4}] * 2
    assert "imageData" not in ask(Composer(records=[chat_record(answer="", images=1)]))


def test_files_are_attached_and_sent_after_their_uploads_finish():
    page = Composer()
    ask(page, files=["/tmp/notes.txt"])
    assert page.attached == ["/tmp/notes.txt"] and page.sent == 1 and no_listeners(page)


# Stopped before anything is sent: the message says so, and Send was never clicked.

def not_sent(page, message, *, clicked=False, **kwargs):
    with pytest.raises(Exception) as raised:
        ask(page, **kwargs)
    assert message in str(raised.value)
    assert page.sent == 0 and (sel.SEND_BUTTON in page.clicks) is clicked and "Enter" not in page.keys
    assert no_listeners(page)
    return raised.value


def test_uploads_that_never_finish_stop_before_sending():
    error = not_sent(Composer(uploads=False), "ChatGPT was still uploading the --file attachments after 0.3s "
                                              "(0 of 1 done). The prompt was not sent.", files=["/tmp/a.png"])
    assert isinstance(error, RuntimeError)


def test_a_send_button_that_never_enables_stops_before_sending():
    not_sent(Composer(send_ready=False), "ChatGPT's Send button never enabled. The prompt was not sent.")


def test_a_cap_notice_on_the_page_stops_before_typing():
    error = not_sent(Composer(notices=["You've reached our limit of messages per hour."]),
                     "You've reached our limit of messages per hour.")
    assert isinstance(error, fetch.RateLimited) and error.sent is False
    assert error.message == "You've reached our limit of messages per hour."


def test_no_prompt_box_stops_before_typing():
    not_sent(Composer(prompt_box=False), "")


def test_a_picker_that_did_not_take_is_stopped_by_the_guard_and_reported_not_sent():
    page = Composer(click="blocked")
    error = not_sent(page, "The model picker didn't take: the prompt asked for another model than gpt-6-thinking, "
                           "so llmcli stopped it. The prompt was not sent.", model="gpt-6-thinking", clicked=True)
    assert page.models_at_send == [frozenset({"gpt-6-thinking"})] and isinstance(error, RuntimeError)


def test_a_click_that_sends_nothing_says_it_never_sent():
    not_sent(Composer(click="silent"), "ChatGPT never sent the prompt (no request within 0.3s).", clicked=True)


# Stopped after the prompt went out: the message says it was sent, so nobody resends.

@pytest.mark.parametrize(("status", "body", "message"), [
    (429, ('{"detail": {"message": "You\'ve reached our limit of messages per hour.", "code": "model_cap_exceeded", '
           '"clears_in": 1800}}'), "You've reached our limit of messages per hour."),
    (403, '{"detail": {"message": "Usage cap hit", "code": "model_cap_exceeded"}}', "Usage cap hit"),
    (400, '{"detail": "Too many requests in 1 hour. Try again later."}', "Too many requests in 1 hour. Try again later."),
])
def test_a_refusal_for_a_cap_is_rate_limited_and_sent(status, body, message):
    page = Composer(status=status, body=body)
    with pytest.raises(fetch.RateLimited) as raised:
        ask(page)
    assert (raised.value.message, raised.value.sent) == (message, True) and page.sent == 1


def test_clears_in_becomes_resets_at():
    page = Composer(status=429, body='{"detail": {"message": "Slow down", "clears_in": 1800}}')
    with pytest.raises(fetch.RateLimited) as raised:
        ask(page)
    now = parse.datetime.now(parse.RESET_TZ)
    resets = parse.datetime.fromisoformat(raised.value.resets_at)
    assert 1795 <= (resets - now).total_seconds() <= 1805


@pytest.mark.parametrize(("status", "body", "message"), [
    (400, '{"detail": {"message": "Unsupported capability", "code": "unsupported_capability"}}',
     "ChatGPT refused the prompt: HTTP 400 (Unsupported capability)."),  # "cap" inside a word isn't a cap
    (500, "<html>oops</html>", "ChatGPT refused the prompt: HTTP 500 (HTTP 500)."),
    (413, '{"detail": {"code": "content_too_large"}}', "ChatGPT refused the prompt: HTTP 413 (content_too_large)."),
])
def test_any_other_refusal_is_an_error_that_says_the_prompt_was_sent(status, body, message):
    page = Composer(status=status, body=body)
    with pytest.raises(RuntimeError) as raised:
        ask(page)
    assert not isinstance(raised.value, fetch.RateLimited)
    assert str(raised.value) == f"{message} {fetch.SENT_HINT}" and no_listeners(page)


def test_a_prompt_that_went_out_without_an_answer_says_it_was_sent():
    page = Composer(respond=False)
    with pytest.raises(RuntimeError) as raised:
        ask(page)
    assert str(raised.value) == f"ChatGPT got the prompt but didn't answer within 0.3s. {fetch.SENT_HINT}"


def test_an_answer_without_a_chat_id_says_it_was_sent():
    page = Composer(moves_to_chat=False)
    with pytest.raises(RuntimeError) as raised:
        ask(page, timeout=0.3)
    assert str(raised.value) == ("ChatGPT answered, but the page shows no chat id to read the reply from. "
                                 + fetch.SENT_HINT)


def test_a_reply_unfinished_at_the_deadline_says_it_was_sent():
    page = Composer(records=[chat_record(done=False)])
    with pytest.raises(RuntimeError) as raised:
        ask(page, timeout=0.3)
    assert str(raised.value) == f"ChatGPT's reply wasn't finished at the deadline (raise --timeout). {fetch.SENT_HINT}"


def test_a_stream_that_ended_without_a_reply_looks_for_a_cap_notice_on_the_page():
    prompt = "Reply with exactly: pong"
    page = Composer(records=[chat_record(reply=False)], tail=f"{prompt}\nYou've hit your limit. Try again tomorrow.")
    with pytest.raises(fetch.RateLimited) as raised:
        ask(page, prompt)
    assert (raised.value.message, raised.value.sent) == ("You've hit your limit. Try again tomorrow.", True)


def test_a_cap_notice_after_sending_is_rate_limited_and_sent():
    page = Composer(records=[chat_record(reply=False)])

    async def notices_after_send(script, arg=None, _evaluate=page.evaluate):
        if script == fetch.NOTICES_JS and page.sent:
            return ["You've reached the current usage cap for GPT-6, please try again after 4:22 PM."]
        return await _evaluate(script, arg)

    page.evaluate = notices_after_send
    with pytest.raises(fetch.RateLimited) as raised:
        ask(page)
    assert raised.value.sent is True and "T16:22:00" in raised.value.resets_at
