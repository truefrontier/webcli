"""Offline tests (from musecli) for muse's parse.py: ids, URLs, hold pages, page snapshots, POST operation labels,
and the usage-cap check."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from conftest import FIXTURES
from webcli.sites.muse import parse

FIX = FIXTURES / "muse"
UUID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"


def fixture(name: str):
    return json.loads((FIX / name).read_text())


@pytest.mark.parametrize(("value", "expected"), [
    (UUID, UUID),
    (f"  https://muse.ai/thread/{UUID}?ref=x#m3 ", UUID),
    (f"muse.ai/thread/{UUID}", UUID),
    ("main", "main"),
    ("https://muse.ai/thread/main", "main"),
])
def test_clean_chat_id(value, expected):
    assert parse.clean_chat_id(value) == expected


@pytest.mark.parametrize("bad", ["", "new", "https://muse.ai/thread/new", "../api/logout", "a b", "x" * 129,
                                 "https://muse.ai/library/recent"])
def test_clean_chat_id_rejects(bad):
    with pytest.raises(ValueError, match="not a Muse chat id"):
        parse.clean_chat_id(bad)


def test_thread_id():
    assert parse.thread_id(f"https://muse.ai/thread/{UUID}") == UUID
    assert parse.thread_id(f"/thread/{UUID}") == UUID
    assert parse.thread_id("https://muse.ai/thread/new") is None
    assert parse.thread_id("https://muse.ai/") == "main"  # the main chat lives at "/"
    assert parse.thread_id("https://muse.ai/thread/main") == "main"
    assert parse.thread_id("https://muse.ai/library") is None
    assert parse.thread_id("about:blank") is None
    assert parse.thread_id(f"https://evil.example/thread/{UUID}") is None
    assert parse.thread_id("https://evil.example/") is None


def test_on_chat():
    assert parse.on_chat(f"https://muse.ai/thread/{UUID}?x=1", UUID)
    assert not parse.on_chat("https://muse.ai/", UUID)
    assert parse.on_chat("https://muse.ai/", "main") and parse.on_chat("https://muse.ai/thread/main", "main")
    assert not parse.on_chat("https://muse.ai/library", "main")


@pytest.mark.parametrize(("path", "hold"), [
    ("/", None), ("/thread/main", None), ("/accessibility", None),
    ("/access", "access"), ("/access/waitlist", "waitlist"), ("/access/invite/abc", "invite"),
    ("/blocked", "blocked"), ("/unavailable/session", "unavailable"),
])
def test_gate(path, hold):
    assert parse.gate(path) == hold


def test_auth_check():
    assert parse.auth_check({"name": "Test Owner"}) == (True, "Test Owner")
    assert parse.auth_check({"user": {"username": "testowner"}}) == (True, "testowner")
    assert parse.auth_check({}) == (True, None)  # shape unknown: 2xx without a flag counts
    assert parse.auth_check(None) == (True, None)
    assert parse.auth_check({"authenticated": False, "name": "x"}) == (False, None)
    assert parse.auth_check({"error": True}) == (False, None)
    raw = fixture("auth_check.json")  # what AUTH_CHECK_JS would filter; even unfiltered, only the name comes out
    assert parse.auth_check(raw) == (True, "Test Owner")


def test_chat_row():
    rows = fixture("rows.json")
    assert parse.chat_row(rows[0], "main") == {"id": "main", "title": "Main chat", "updated": None,
                                               "url": "https://muse.ai/"}
    assert parse.chat_row(rows[2], UUID) == {"id": UUID, "title": "Trip to Lisbon", "updated": "Tue",
                                             "url": f"https://muse.ai/thread/{UUID}"}
    assert parse.chat_row({}, UUID)["title"] is None


def test_messages_ids_labels_and_citations():
    messages = [m for m in map(parse.message, fixture("thread.json")) if m]
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]
    assert messages[0] == {"id": "user-msg-1", "role": "user", "text": "Plan a weekend in Lisbon under $800."}
    assert messages[1] == {
        "id": "assistant-msg-0f8fad5b-d9cb-469f-a165-70867728950e",
        "role": "assistant",
        "text": "Here's a plan.\n\n1. Fly in Friday night.\n2. Stay in Alfama.",
        "citations": [{"title": "Visit Lisboa", "url": "https://www.visitlisboa.com/en"},
                      {"title": "Alfama", "url": "https://en.wikipedia.org/wiki/Alfama"}],
    }
    assert messages[3] == {"id": None, "role": "assistant", "text": "Done: swap the hotel for a guesthouse."}


@pytest.mark.parametrize(("text", "role", "clean"), [
    ("You:\n\npong?", "user", "pong?"),
    ("User message: pong?", "user", "pong?"),
    ("Assistant message: pong", "assistant", "pong"),
    ("  pong  ", "assistant", "pong"),
    ("You: are the label only for the user", "assistant", "You: are the label only for the user"),
    ("Assistant message: stays on a user message", "user", "Assistant message: stays on a user message"),
    ("Thank you: pong", "user", "Thank you: pong"),  # only a leading label
    ("You:", "user", ""),
])
def test_clean_text_strips_a_leading_screen_reader_label(text, role, clean):
    assert parse.clean_text(text, role) == clean
    assert parse.message({"role": role, "text": text}) == (
        {"id": None, "role": role, "text": clean} if clean else None)


def test_post_op_graphql_form():
    body = (FIX / "graphql_form.txt").read_text()
    assert parse.post_op({}, body) == "useHatchConversationsQuery"
    assert parse.post_op({"X-FB-Friendly-Name": "useHatchConversationsQuery"}, body) == "useHatchConversationsQuery"
    with pytest.raises(ValueError, match="disagree"):
        parse.post_op({"x-fb-friendly-name": "useHatchDeleteSessionMutation"}, body)
    with pytest.raises(ValueError, match="disagree"):
        parse.post_op({}, body + "&fb_api_req_friendly_name=useHatchDeleteSessionMutation")
    assert parse.post_op({}, "doc_id=123&variables=%7B%7D") == "doc_id:123"
    with pytest.raises(ValueError, match="doc_id"):
        parse.post_op({}, "doc_id=123&doc_id=456")


def test_post_op_json_action_and_plain():
    assert parse.post_op({}, json.dumps({"operationName": "ThreadQuery", "variables": {}})) == "ThreadQuery"
    assert parse.post_op({}, json.dumps({"doc_id": "987"})) == "doc_id:987"
    assert parse.post_op({"Next-Action": "7f3a9c"}, "") == "next-action:7f3a9c"
    assert parse.post_op({}, json.dumps({"vmAddress": "a", "vmName": "b"})) is None
    assert parse.post_op({}, None) is None
    with pytest.raises(ValueError, match="batched"):
        parse.post_op({}, json.dumps([{"operationName": "A"}, {"operationName": "B"}]))
    with pytest.raises(ValueError):
        parse.post_op({}, "{not json")


@pytest.mark.parametrize(("answer", "capped"), [
    ("You've reached your daily message limit. Your limit resets at 4:00 PM.", True),
    ("Sorry, you have hit the usage limit for today.", True),
    ("Your message limit resets in 3 hours.", True),
    ("Too many requests. Try again later.", True),
    ("Usage limit reached. Come back tomorrow.", True),
    ("Here is how API rate limits work: you've reached a limit when the server answers 429. " * 5, False),  # long
    ("Rate limits protect servers. You've reached the end of my notes.", False),  # doesn't open with a notice
    ("pong", False),
    ("", False),
    (None, False),
])
def test_limit_reply_is_strict(answer, capped):
    assert parse.limit_reply(answer) is capped


def test_resets_at_is_the_shared_parser():
    now = datetime.fromisoformat("2026-10-19T10:00:00-05:00")
    assert parse.resets_at("Your limit resets at 4:00 PM.", now) == "2026-10-19T16:00:00-05:00"
    assert parse.resets_at("You've reached your limit.", now) is None
