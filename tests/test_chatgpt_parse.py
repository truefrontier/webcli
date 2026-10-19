"""Offline tests for the parsers, against sanitized live captures (2026-10-18; ids faked, text synthetic) in
tests/fixtures/: models.json (GET /backend-api/models), conversations.json (GET /backend-api/conversations), and
three chat records: record_text.json ("pong" from gpt-6-thinking), record_instant.json ("pong" from the picker's
Instant stop, recorded as gpt-6), and record_image.json (an image reply the image tool made, routed to
gpt-5-4-thinking)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from webcli.sites.chatgpt import parse

FIXTURES = Path(__file__).parent / "fixtures" / "chatgpt"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


@pytest.mark.parametrize(("value", "expected"), [
    ("6AD5178B-2308-83EA-A3A4-12DC075D678F", "6ad5178b-2308-83ea-a3a4-12dc075d678f"),
    ("https://chatgpt.com/c/6ad5178b-2308-83ea-a3a4-12dc075d678f", "6ad5178b-2308-83ea-a3a4-12dc075d678f"),
    ("https://chatgpt.com/g/g-p-abc123-notes/c/6ad5178b-2308-83ea-a3a4-12dc075d678f?model=x",
     "6ad5178b-2308-83ea-a3a4-12dc075d678f"),
])
def test_clean_chat_id(value, expected):
    assert parse.clean_chat_id(value) == expected


@pytest.mark.parametrize("value", ["", "pong", "1a2b3c4d5e6f7a8b", "https://chatgpt.com/c/not-a-uuid"])
def test_clean_chat_id_rejects(value):
    with pytest.raises(ValueError):
        parse.clean_chat_id(value)


def test_mask_email():
    assert parse.mask_email("owner.name@example.com") == "o***@example.com"
    assert parse.mask_email(None) is None and parse.mask_email("nonsense") == "***"


def test_models_from_a_live_list():
    rows = parse.models(fixture("models.json"))
    slugs = [r["slug"] for r in rows]
    assert len(rows) == 19 and "gpt-6-instant" in slugs and "gpt-5-6-thinking" in slugs
    assert [r["slug"] for r in rows if r["default"]] == ["gpt-6"]
    gpt6 = next(r for r in rows if r["slug"] == "gpt-6")
    assert gpt6 == {"slug": "gpt-6", "title": "GPT-6", "description": gpt6["description"], "default": True,
                    "reasoning": "auto", "images": True}


@pytest.mark.parametrize(("wanted", "slug"), [
    ("gpt-6-instant", "gpt-6-instant"), ("GPT-6-Thinking", "gpt-6-thinking"), ("GPT-6 Luna", "gpt-6-luna-wm"),
    ("astra", "gpt-6-astra-wm"),
])
def test_match_model(wanted, slug):
    assert parse.match_model(parse.models(fixture("models.json")), wanted) == slug


@pytest.mark.parametrize(("wanted", "message"), [("GPT-5.6 Sol", "more than one"), ("gpt-9", "No model matches")])
def test_match_model_refuses_ambiguous_or_unknown(wanted, message):
    with pytest.raises(LookupError, match=message):
        parse.match_model(parse.models(fixture("models.json")), wanted)


def test_picker_choice_maps_slugs_to_the_version_radio_and_power_stop():
    catalog = fixture("models.json")
    assert parse.picker_choice(catalog, "gpt-6-instant") == {
        "version": "GPT-6", "labels": ["GPT-6"], "preset": "Instant", "index": 0, "sends": ["gpt-6-instant", "gpt-6"]}
    assert parse.picker_choice(catalog, "gpt-6") == parse.picker_choice(catalog, "gpt-6-instant")  # the auto slug
    thinking = parse.picker_choice(catalog, "gpt-5-6-thinking")
    assert (thinking["version"], thinking["preset"], thinking["index"], thinking["sends"]) == \
        ("GPT-5.6 Sol", "Medium", 1, ["gpt-5-6-thinking"])
    assert "5.6" in thinking["labels"]
    for slug in ("gpt-6-mini", "gpt-6-sol-wm", "research"):
        assert parse.picker_choice(catalog, slug) is None


def test_display_name():
    catalog = fixture("models.json")
    assert parse.display_name(catalog, "gpt-6-thinking") == "GPT-6 Thinking"
    assert parse.display_name(catalog, "gpt-5-6-instant") == "GPT-5.6 Sol Instant"
    assert parse.display_name(catalog, "gpt-5-4-thinking") is None and parse.display_name(catalog, None) is None


@pytest.mark.parametrize(("requested", "served", "same"), [
    ("gpt-6", "gpt-6-instant", True), ("gpt-6", "gpt-6-thinking", True), ("gpt-6-thinking", "gpt-6-thinking", True),
    ("gpt-5-4-auto-thinking", "gpt-5-4-thinking", True), (None, "gpt-6", True),
    ("gpt-6", "gpt-6-mini", False), ("gpt-6-thinking", "gpt-6-t-mini", False),
    ("gpt-6-thinking", "gpt-5-6-thinking", False), ("gpt-6-thinking", "gpt-5-4-thinking", False),
])
def test_same_model(requested, served, same):
    assert parse.same_model(requested, served) is same


def test_chats_from_a_live_list():
    rows = parse.chats(fixture("conversations.json"))
    assert len(rows) == 3
    assert rows[0] == {"id": "00000000-0000-4000-8000-000000000001", "title": "Synthetic chat title",
                       "updated": "2026-10-18T19:00:00Z", "created": "2026-10-18T19:00:00Z", "pinned": False,
                       "url": "https://chatgpt.com/c/00000000-0000-4000-8000-000000000001"}


def test_reply_turn_text():
    turn = parse.reply_turn(fixture("record_text.json"))
    assert turn["answer"] == "pong"
    assert turn["servedModel"] == "gpt-6-thinking" and turn["endTurn"]
    assert turn["statuses"] == ["finished_successfully", "finished_successfully"]
    assert turn["images"] == [] and turn["citations"] == []


def test_reply_turn_records_the_auto_slug_for_the_instant_stop():
    assert parse.reply_turn(fixture("record_instant.json"))["servedModel"] == "gpt-6"


def test_reply_turn_image():
    record = fixture("record_image.json")
    turn = parse.reply_turn(record)
    assert turn["answer"] == ""  # the image is the reply; the tool's caption and file notes are not the answer
    assert turn["servedModel"] == "gpt-5-4-thinking"  # ChatGPT routed the image turn (the picker said gpt-6-thinking)
    assert turn["requestedSlug"] == "gpt-5-4-auto-thinking" and turn["defaultModel"] == "gpt-6-thinking"
    assert turn["endTurn"] and "in_progress" not in turn["statuses"]
    assert turn["images"] == [{"fileId": "file_00000000000000000000000000000001",
                               "pointer": "sediment://file_00000000000000000000000000000001",
                               "width": 1254, "height": 1254, "bytes": 1200158, "mime": "image/png"}]  # once


def test_reply_turn_for_a_user_message_id_and_a_missing_one():
    record = fixture("record_text.json")
    user = next(m for m in parse.branch(record) if m["author"]["role"] == "user")
    assert parse.reply_turn(record, user["id"])["answer"] == "pong"
    assert parse.reply_turn(record, "00000000-0000-4000-8000-999999999999") is None
    assert parse.reply_turn({}) is None


def test_reply_turn_in_progress_is_not_ended():
    record = fixture("record_text.json")
    last = record["mapping"][record["current_node"]]["message"]
    last.update(status="in_progress", end_turn=None)
    turn = parse.reply_turn(record)
    assert not turn["endTurn"] and "in_progress" in turn["statuses"]


def test_messages_for_chats_show():
    shown = parse.messages(fixture("record_image.json"))
    assert [(m["role"], m["text"]) for m in shown][0] == \
        ("user", "Create a square image: seamless tileable pattern of small blue stars on white")
    assert any(m["images"] == [{"fileId": "file_00000000000000000000000000000001", "width": 1254, "height": 1254,
                                "bytes": 1200158, "mime": "image/png"}] for m in shown)
    assert [m["text"] for m in parse.messages(fixture("record_text.json"))] == ["Reply with exactly: pong", "pong"]


@pytest.mark.parametrize(("body", "expected"), [
    ('{"detail": {"message": "You\'ve reached our limit of messages per hour.", "code": "model_cap_exceeded", '
     '"clears_in": 1800}}', {"message": "You've reached our limit of messages per hour.",
                             "code": "model_cap_exceeded", "clearsIn": 1800}),
    ('{"detail": "Too many requests"}', {"message": "Too many requests", "code": None, "clearsIn": None}),
    ('event: delta\ndata: {"v": "po"}\n\ndata: {"error": "Something went wrong", "code": "x"}\n\ndata: [DONE]\n',
     {"message": "Something went wrong", "code": "x", "clearsIn": None}),
    ('data: {"v": "pong"}\n\ndata: [DONE]\n', None),
    ("", None),
])
def test_stream_error(body, expected):
    assert parse.stream_error(body) == expected


@pytest.mark.parametrize("text", [
    "You've reached our limit of messages per hour. Please try again later.",
    "You've hit the Plus plan limit for image generations requests. You can create more images when the limit "
    "resets in 23 hours and 52 minutes.",
    "You've reached the current usage cap for GPT-6, please try again after 4:22 PM.",
    "Too many requests in 1 hour. Try again later.",
    "You’ve hit your limit for GPT-6 Thinking. Your limit resets at 1:53 PM.",
    "Usage limit reached", "Quota exceeded",
])
def test_limit_classification(text):
    assert parse.limit_notice(text)
    assert parse.limit_reply(text)


@pytest.mark.parametrize("text", [None, "", "pong", "Here is your answer.", "Explain what a quota is.",
                                  "The usage limit for this model is described in the documentation.",
                                  "Upgrade to Pro for higher limits."])
def test_normal_reply_is_not_rate_limited(text):
    assert not parse.limit_reply(text)


@pytest.mark.parametrize(("text", "now", "expected"), [
    ("You can create more images when the limit resets in 23 hours and 52 minutes.",
     "2026-10-18T13:00:00-05:00", "2026-10-19T12:52:00-05:00"),
    ("Please try again in 5 minutes.", "2026-10-18T13:00:00-05:00", "2026-10-18T13:05:00-05:00"),
    ("Too many requests in 1 hour. Try again later.", "2026-10-18T13:00:00-05:00", None),  # a window, not a reset
    ("You've reached the current usage cap for GPT-6, please try again after 4:22 PM.",
     "2026-10-18T13:00:00-05:00", "2026-10-18T16:22:00-05:00"),
    ("Your limit resets at 1:53 PM", "2026-10-16T14:00:00-05:00", "2026-10-17T13:53:00-05:00"),
    ("Your limit resets at 1:53 PM", "2026-10-31T15:00:00-05:00", "2026-11-01T13:53:00-06:00"),  # CDT -> CST
    ("Your limit resets tomorrow at 2:30 AM", "2026-03-07T15:00:00-06:00", "2026-03-08T03:30:00-05:00"),
    ("Your limit resets Friday at 1:53 PM", "2026-10-16T14:00:00-05:00", "2026-10-23T13:53:00-05:00"),
    ("Your limit resets November 2, 2026", "2026-10-16T12:00:00-05:00", "2026-11-02T00:00:00-06:00"),
    ("Quota exceeded", "2026-10-16T12:00:00-05:00", None),
])
def test_limit_reset_time(text, now, expected):
    assert parse.resets_at(text, datetime.fromisoformat(now)) == expected


def test_resets_after_seconds():
    now = datetime.fromisoformat("2026-10-18T18:00:00+00:00")
    assert parse.resets_after(1800, now) == "2026-10-18T13:30:00-05:00"
    assert parse.resets_after(None, now) is None and parse.resets_after(-1, now) is None


@pytest.mark.parametrize(("answer", "is_error"), [
    ("Something went wrong while generating the response. If this issue persists please contact us.", True),
    ("Hmm...something seems to have gone wrong.", True), ("Network error", True),
    ("pong", False), ("Something went wrong with your regex: the group is unclosed.", False), ("", False),
])
def test_error_reply(answer, is_error):
    assert parse.error_reply(answer) is is_error


def test_done_waits_for_a_settled_turn_and_gives_images_a_grace_period():
    import time

    from webcli.sites.chatgpt.fetch import IMAGE_GRACE, _done
    ended = {"endTurn": True, "statuses": ["finished_successfully"], "images": []}
    working = {**ended, "statuses": ["finished_successfully", "in_progress"]}
    assert _done(ended, False, None)
    assert not _done(working, False, None) and not _done(working, True, time.monotonic() - 999)
    assert not _done({**ended, "endTurn": False}, False, None) and not _done(None, False, None)
    assert not _done(ended, True, time.monotonic())  # an image may still come
    assert _done(ended, True, time.monotonic() - IMAGE_GRACE - 1)
    assert _done({**ended, "images": [{"fileId": "f"}]}, True, time.monotonic())
