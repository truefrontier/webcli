"""Offline tests for the wire-format parsers, against authored fixtures shaped like real responses.

The fixtures follow the index paths that gemini_webapi (master, 2026-10-08) and Leechael/gemini-web-cli
(fixtures verified against boq_assistant-bard-web-server_20260910.05_p2) read. Only read_chat_image.txt is a
live capture (sanitized).
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlencode

import pytest

from webcli.sites.gemini import parse

FIX = Path(__file__).parent / "fixtures" / "gemini"


def body(name: str) -> str:
    return (FIX / name).read_text()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1a2b3c4d5e6f7a8b", "1a2b3c4d5e6f7a8b"),
        ("c_1a2b3c4d5e6f7a8b", "1a2b3c4d5e6f7a8b"),
        ("  c_1a2b3c4d5e6f7a8b\n", "1a2b3c4d5e6f7a8b"),
        ("f1x7u0e0000000a1", "f1x7u0e0000000a1"),  # ids aren't always hex; case is kept
        ("https://gemini.google.com/app/1a2b3c4d5e6f7a8b", "1a2b3c4d5e6f7a8b"),
        ("https://gemini.google.com/u/1/app/1a2b3c4d5e6f7a8b?hl=en#x", "1a2b3c4d5e6f7a8b"),
    ],
)
def test_clean_chat_id(value, expected):
    assert parse.clean_chat_id(value) == expected


@pytest.mark.parametrize("bad", ["", "../app", "1a2b/../x", "abc", "c_", "a-b-c-d-e-f", "%2e%2e%2fapp",
                                 "https://evil.example/app/1a2b3c4d", "C_1A2B3C4D5E6F7A8B"])
def test_clean_chat_id_rejects_non_ids(bad):
    with pytest.raises(ValueError, match="not a Gemini chat id"):
        parse.clean_chat_id(bad)


def test_url_chat_id():
    assert parse.url_chat_id("https://gemini.google.com/app/7e6d5c4b3a291807") == "7e6d5c4b3a291807"
    assert parse.url_chat_id("https://gemini.google.com/u/1/app/7e6d5c4b3a291807?hl=en") == "7e6d5c4b3a291807"
    assert parse.url_chat_id("https://gemini.google.com/app/c_7e6d5c4b3a291807") == "7e6d5c4b3a291807"
    for page in ("https://gemini.google.com/app", "https://gemini.google.com/app/", "https://gemini.google.com/gems",
                 "https://gemini.google.com/app/a-b", "https://evil.example/app/7e6d5c4b3a291807", "about:blank", ""):
        assert parse.url_chat_id(page) is None, page


def test_envelopes_skip_lengths_and_bookkeeping():
    kinds = [e[0] for e in parse.envelopes(body("list_chats_page1.txt"))]
    assert kinds == ["wrb.fr", "di", "af.httprm", "e"]
    assert list(parse.envelopes("")) == [] and list(parse.envelopes(")]}'\n\nnot json")) == []
    stream = body("stream_generate.txt")
    cut = stream[: stream.index("Two plus two")]  # a stream cut off mid-frame still yields what arrived
    assert [e[0] for e in parse.envelopes(cut)] == ["wrb.fr", "wrb.fr"]


def test_rpc_payload_and_errors():
    assert parse.rpc_payload(body("list_chats_page1.txt"), "MaZiqc")[1] == "cursor_page_2"
    with pytest.raises(parse.RpcError, match="no hNvQHb result"):
        parse.rpc_payload(body("list_chats_page1.txt"), "hNvQHb")
    with pytest.raises(parse.RpcError) as missing:
        parse.rpc_payload(body("rpc_error.txt"), "hNvQHb")
    assert missing.value.code == 5
    with pytest.raises(parse.RpcError) as denied:
        parse.rpc_payload(body("rpc_denied.txt"), "MaZiqc")
    assert denied.value.code == 7
    with pytest.raises(parse.RpcError, match="rejected"):
        parse.rpc_payload(')]}\'\n\n20\n[["er",null,null,null,null,400]]', "MaZiqc")


def test_chats_rows_and_cursor():
    rows, cursor = parse.chats(parse.rpc_payload(body("list_chats_page1.txt"), "MaZiqc"))
    assert cursor == "cursor_page_2"
    assert rows[0] == {
        "id": "1a2b3c4d5e6f7a8b",
        "title": "Trip ideas for Kyoto",
        "updated": "2025-10-14T14:00:00Z",
        "pinned": True,
        "url": "https://gemini.google.com/app/1a2b3c4d5e6f7a8b",
    }
    assert [(r["id"], r["title"], r["pinned"]) for r in rows[1:]] == [
        ("0f1e2d3c4b5a6978", "Fix flaky pytest", False),
        ("9a8b7c6d5e4f3a2b", None, False),  # untitled
    ]
    rows, cursor = parse.chats(parse.rpc_payload(body("list_chats_page2.txt"), "MaZiqc"))
    assert (len(rows), cursor) == (1, None)
    assert parse.chats(None) == ([], None) and parse.chats([]) == ([], None)
    with pytest.raises(ValueError):
        parse.chats({"unexpected": True})


def test_chat_messages_oldest_first_with_cleanup_and_citations():
    messages, cursor = parse.chat_messages(parse.rpc_payload(body("read_chat.txt"), "hNvQHb"))
    assert cursor is None
    assert [(m["role"], m["text"]) for m in messages] == [
        ("user", "Plan 3 days in Kyoto"),
        ("model", "**Day 1:** Fushimi Inari & Gion"),  # card placeholder replaced by its text
        ("user", "And in winter?"),
        ("model", "Winter in Kyoto is quiet[^1][^2].\n\n"  # image placeholder stripped, [cite: N] footnoted
                  "[^1]: Kyoto City Official Travel Guide, https://kyoto.travel/en/\n"
                  "[^2]: Kyoto | JNTO, https://www.japan.travel/en/destinations/kansai/kyoto/"),
    ]
    assert messages[0]["at"] == "2025-10-14T11:06:40Z"
    assert messages[3]["citations"] == [
        {"n": 1, "label": "kyoto.travel", "title": "Kyoto City Official Travel Guide", "url": "https://kyoto.travel/en/"},
        {"n": 2, "label": "japan.travel", "title": "Kyoto | JNTO",
         "url": "https://www.japan.travel/en/destinations/kansai/kyoto/"},
    ]
    assert "citations" not in messages[1]
    _, cursor = parse.chat_messages(parse.rpc_payload(body("read_chat_paged.txt"), "hNvQHb"))
    assert cursor == "cursor_older"


def test_an_image_turn_from_a_live_transcript():
    """read_chat_image.txt is a live hNvQHb capture of an Argon image reply (2026-10-17), ids and URLs replaced."""
    payload = parse.rpc_payload(body("read_chat_image.txt"), "hNvQHb")
    turn = {"chatId": "c_0000000000000001", "responseId": "r_0000000000000002", "candidateId": "rc_0000000000000003",
            "answer": "", "refs": [], "canvas": None,
            "images": [{"url": "https://lh3.googleusercontent.com/gg/FAKE-TOKEN-4",
                        "imageId": "http://googleusercontent.com/image_generation_content/0_784"}]}
    assert parse.reply_turn(payload) == turn
    assert parse.reply_turn(payload, "r_0000000000000002") == turn
    assert parse.reply_turn(payload, "r_ffffffffffffffff") is None  # not this response
    assert parse.reply_turn(None) is None and parse.reply_turn([]) is None
    assert parse.full_size_payload(turn, turn["images"][0]["imageId"]) == [
        [[None, None, None, [None, None, None, None, None, ""]],
         ["http://googleusercontent.com/image_generation_content/0_784", 0], None, [19, ""],
         None, None, None, None, None, ""],
        ["r_0000000000000002", "rc_0000000000000003", "c_0000000000000001", None, ""], 1, 0, 1]
    messages, _ = parse.chat_messages(payload)
    assert messages[1] == {"role": "model", "text": "", "at": messages[1]["at"], "images": 1}


def test_a_text_turn_has_no_images():
    payload = parse.rpc_payload(body("read_chat.txt"), "hNvQHb")
    assert parse.reply_turn(payload)["images"] == []
    assert parse.generated_images(None) == [] and parse.generated_images([]) == []


@pytest.mark.parametrize("placeholder", ["http://googleusercontent.com/image_generation_content/0_784",
                                         "http://googleusercontent.com/image_generation_content/3",
                                         "https://googleusercontent.com/card_content/12"])
def test_clean_text_drops_every_placeholder_shape(placeholder):
    assert parse.clean_text(f"Here you go:\n\n{placeholder}\n\nEnjoy.") == "Here you go:\n\nEnjoy."
    assert parse.clean_text(f"\n\n{placeholder}\n\n\n") == ""


def test_generate_takes_the_final_update():
    result = parse.generate(body("stream_generate.txt"))
    kyoto = {"label": "kyoto.travel", "title": "Kyoto City Official Travel Guide", "url": "https://kyoto.travel/en/"}
    assert result == {
        "chatId": "7e6d5c4b3a291807",
        "answer": "Two plus two is **4** [cite: 1].\n\n```python\nprint(2 + 2)\n```",
        "refs": [(22, 31, [kyoto])],  # the marker "[cite: 1]"; footnote() eats the space before it
        "images": 0,
        "done": True,
        "error": None,
    }


IMAGE_SLOT = "http://googleusercontent.com/image_generation_content/0"


def test_generate_counts_the_images_a_streamed_answer_made():
    stream = body("stream_generate.txt").replace(r'print(2 + 2)\\n```\"]', rf'print(2 + 2)\\n```\\n\\n{IMAGE_SLOT} {IMAGE_SLOT}\"]')
    assert IMAGE_SLOT in stream
    result = parse.generate(stream)  # the same placeholder twice is one image
    assert (result["answer"], result["images"]) == ("Two plus two is **4** [cite: 1].\n\n```python\nprint(2 + 2)\n```", 1)


def test_generate_partial_and_error_streams():
    stream = body("stream_generate.txt")
    partial = parse.generate(stream[: stream.index("Two plus two")])  # cut inside the second update
    assert (partial["answer"], partial["done"]) == ("Two plus", False)
    assert parse.generate(body("stream_generate_error.txt"))["error"] == 1037
    assert parse.generate(")]}'\n\n20\n" + '[["er",null,null,null,null,400]]')["error"] == 400
    assert parse.generate("") == {"chatId": None, "answer": None, "refs": [], "images": 0, "done": False,
                                  "error": None}


def test_freq_rpcids():
    def form(freq):
        return urlencode({"f.req": json.dumps(freq), "at": "x"})

    assert parse.freq_rpcids(form([[["MaZiqc", "[]", None, "generic"], ["hNvQHb", "[]", None, "1"]]])) == [
        "MaZiqc", "hNvQHb"]
    assert parse.freq_rpcids(form([[["MaZiqc", "[]"], [42, "[]"]]])) == []  # anything odd -> nothing
    assert parse.freq_rpcids(form({"MaZiqc": 1})) == []
    assert parse.freq_rpcids("f.req=%5B%5B") == [] and parse.freq_rpcids(None) == []


def test_iso_time():
    assert parse.iso_time([1760450400, 999999999]) == "2025-10-14T14:00:00Z"
    assert parse.iso_time(1760450400) == "2025-10-14T14:00:00Z"
    assert [parse.iso_time(v) for v in (None, [], 0, True, "1760450400")] == [None] * 5


@pytest.mark.parametrize("answer", [
    "I encountered an error doing what you asked. Could you try again?",  # seen during the 2026-10-08 outage
    "I seem to be encountering an error. Can I try something else for you?",
    "Right now: I seem to be encountering an error. Can I try something else for you?",
    "An error occurred – please try again (1076)",
    "I'm having a hard time fulfilling your request. Can I help you with something else instead?",
    "I’m having a hard time fulfilling your request. Please try again later.",
    "Something went wrong (13)",
    "Something went wrong. (13)",
    "Something went wrong 1155",
    "Something went wrong",
    "  something   went wrong!\n",
    "Sorry, something went wrong. Please try your request again.",
    "I’m sorry, something went wrong on my end. Please try again in a few minutes.",
    "Something went wrong. Try again later.",
    "Something went wrong and the content wasn't generated.",
    "Something went wrong and an AI response wasn't generated.",
    "Oops! Something went wrong. Please refresh the page.",
    "An internal error has occurred.",
    "An unexpected error occurred. Please try again.",
    "Sorry couldn't connect to server. Please try again.",
])
def test_error_reply_matches_geminis_error_messages(answer):
    assert parse.error_reply(answer) is True


@pytest.mark.parametrize("answer", [
    # refusals are answers
    "I can't help with that.",
    "Sorry, I can't help with that.",
    "I can’t help you with that.",
    "I won't write that.",
    "I'm unable to help you. As a language model, I don't have the information needed to process your request.",
    "I'm not able to help with that, as I'm only a language model.",
    # answers that talk about errors
    "Something went wrong with your regex: the + is greedy.",
    "Something went wrong. Try running it with sudo.",
    "Something went wrong. Your function returns before the loop ends.",
    "Something went wrong and the build failed.",
    "Here's why something went wrong: the cache was stale.",
    "An error occurred because the path is wrong.",
    "I encountered an error when running your code: NameError: x is not defined.",
    "Error: missing semicolon on line 3.",
    "Something went wrong. " + "The fix is to pin the dependency. " * 3,
    # not error messages at all
    "Try again.",
    "Please try again later.",
    "4",
    "",
    "   ",
    None,
])
def test_error_reply_leaves_refusals_and_real_answers_alone(answer):
    assert parse.error_reply(answer) is False


@pytest.mark.parametrize('text', [
    "Your limit resets at 1:53 PM", "You've reached your limit", "You’ve reached your limit for Pro.",
    "Usage limit reached", "usage limit", "Quota exceeded", "quota", "Daily quota exhausted",
    "Sorry, you've used up your daily quota. Try again tomorrow.",
    "You've reached your limit. " + "Upgrade for more. " * 40,
])
def test_limit_classification(text):
    assert parse.limit_notice(text)
    assert parse.limit_reply(text)


@pytest.mark.parametrize('text', [None, '', 'Here is your answer.', 'Something went wrong.',
    'The usage limit for this model is described in the documentation.',
    'Upgrade to Google AI Pro for higher limits.', 'Explain what a quota is.'])
def test_normal_reply_is_not_rate_limited(text):
    assert not parse.limit_reply(text)


@pytest.mark.parametrize(('text', 'now', 'expected'), [
    ('Your limit resets at 1:53 PM', '2026-10-16T12:00:00-05:00', '2026-10-16T13:53:00-05:00'),
    ('Your limit resets at 1:53 PM', '2026-10-16T14:00:00-05:00', '2026-10-17T13:53:00-05:00'),
    ('Your limit resets at 1:53 PM', '2026-10-16T13:53:00-05:00', '2026-10-17T13:53:00-05:00'),
    ('Your limit resets at 1:53 PM', '2026-01-16T12:00:00-06:00', '2026-01-16T13:53:00-06:00'),
    ('Your limit resets at 1:53 PM', '2026-10-31T15:00:00-05:00', '2026-11-01T13:53:00-06:00'),
    ('Your limit resets at 1:53 PM', '2026-03-07T15:00:00-06:00', '2026-03-08T13:53:00-05:00'),
    ('Your limit resets tomorrow at 2:30 AM', '2026-03-07T15:00:00-06:00', '2026-03-08T03:30:00-05:00'),
    ('Try again at 12:00 AM', '2026-10-16T17:00:00+00:00', '2026-10-17T00:00:00-05:00'),
    ('Your limit resets Saturday at 13:53', '2026-10-16T12:00:00-05:00', '2026-10-17T13:53:00-05:00'),
    ('Your limit resets October 17, 2026 at 1:53 PM', '2026-10-16T12:00:00-05:00', '2026-10-17T13:53:00-05:00'),
    ('Your limit resets 17 October 2026 at 1:53 PM', '2026-10-16T12:00:00-05:00', '2026-10-17T13:53:00-05:00'),
    ('Your limit resets 10/17/2026 at 1:53 PM', '2026-10-16T12:00:00-05:00', '2026-10-17T13:53:00-05:00'),
    ('Your limit resets November 2, 2026', '2026-10-16T12:00:00-05:00', '2026-11-02T00:00:00-06:00'),
    ('Your limit resets at 1:53 AM', '2026-11-01T01:55:00-05:00', '2026-11-01T01:53:00-06:00'),
    ('Your limit resets at 1:53 AM', '2026-11-01T01:30:00-06:00', '2026-11-01T01:53:00-06:00'),
    ('Your limit resets at 1:53 AM', '2026-11-01T01:55:00-06:00', '2026-11-02T01:53:00-06:00'),
    ('Your limit resets Friday at 1:53 PM', '2026-10-16T14:00:00-05:00', '2026-10-23T13:53:00-05:00'),
    ('Quota exceeded', '2026-10-16T12:00:00-05:00', None),
    ('Your limit resets February 30 at 1:53 PM', '2026-10-16T12:00:00-05:00', None),
    ('Your limit resets at 13 PM', '2026-10-16T12:00:00-05:00', None),
])
def test_limit_reset_time(text, now, expected):
    from datetime import datetime
    assert parse.resets_at(text, datetime.fromisoformat(now)) == expected


CANVAS_MD = "# Rain Probe\n\nHeavy rain drummed on the **tin** roof.\n\nThe cabin hummed along."
CANVAS_ANSWER = ('I will now generate the "Rain Probe" document for you.\n\n'
                 "I've put together those two short sentences in the document. Would you like to expand on this "
                 "imagery or adjust the tone?")


def test_a_canvas_turn_from_a_live_transcript():
    """read_chat_canvas.txt is a live hNvQHb capture of a Canvas reply (2026-10-18): ids faked, doc text synthetic."""
    payload = parse.rpc_payload(body("read_chat_canvas.txt"), "hNvQHb")
    turn = parse.reply_turn(payload, "r_0000000000000002")
    assert turn["canvas"] == {"title": "Rain Probe", "markdown": CANVAS_MD}
    assert turn["answer"] == CANVAS_ANSWER  # the doc's inline chip placeholder is gone
    assert turn["images"] == []
    assert parse.reply_turn(parse.rpc_payload(body("read_chat.txt"), "hNvQHb"))["canvas"] is None


def test_canvas_reads_the_markdown_copy_when_the_main_field_is_empty():
    doc = ["FAKE", None, "Notes", None, None, None, None, None, None, "notes.md", 1, None, None, ["# Notes"]]
    assert parse.canvas([None] * 30 + [[doc]]) == {"title": "Notes", "markdown": "# Notes"}
    assert parse.canvas([None] * 30 + [[["FAKE", None, "Empty"]]]) is None
    assert parse.canvas(None) is None and parse.canvas([]) is None


FILES_FOOTNOTED = (
    "* **`outline.md`**: A lighthouse keeper finds a map in the lamp room[^1]. She sails out to follow it and comes "
    "back changed[^1].\n* **`cast.md`**: Mara keeps the light and Teo fixes boats[^2]. Both want the map for "
    "different reasons[^2].\n* **`notes.md`**: Tone notes and a list of storms[^3]. Keep the sea quiet until the "
    "end[^3].\n\n[^1]: outline.md\n[^2]: cast.md\n[^3]: notes.md")
FILES_CITATIONS = [{"n": 1, "label": "MD", "file": "outline.md"}, {"n": 2, "label": "MD", "file": "cast.md"},
                   {"n": 3, "label": "MD", "file": "notes.md"}]


def test_attached_files_cited_in_a_live_transcript():
    """read_chat_cited_files.txt: a live hNvQHb capture (2026-10-18) of three --file attachments, each cited twice
    with [cite: N] (one "MD + 1" chip per list item on the page); every string synthetic, ids fake."""
    payload = parse.rpc_payload(body("read_chat_cited_files.txt"), "hNvQHb")
    messages, _ = parse.chat_messages(payload)
    assert (messages[1]["text"], messages[1]["citations"]) == (FILES_FOOTNOTED, FILES_CITATIONS)
    turn = parse.reply_turn(payload, "r_00000000000000a2")
    assert turn["answer"].count("[cite: 1]") == 2 and len(turn["refs"]) == 6
    bare, cites = parse.footnote(turn["answer"], turn["refs"], footnotes=False)
    assert bare == ("* **`outline.md`**: A lighthouse keeper finds a map in the lamp room. She sails out to follow it "
                    "and comes back changed.\n* **`cast.md`**: Mara keeps the light and Teo fixes boats. Both want "
                    "the map for different reasons.\n* **`notes.md`**: Tone notes and a list of storms. Keep the sea "
                    "quiet until the end.")
    assert cites == FILES_CITATIONS  # listed either way


WEB_FOOTNOTED = (
    "Example City has **120,000** residents (city proper).[^1] The metro area holds about 250,000 people.[^1][^2]"
    "\n\n### Sources\n\n| Area | People |\n|---|---|\n| City | 120,000 |\n\n"
    "> Older counts included people who had moved abroad.[^3][^2]\n\n"
    "[^1]: Example City - Wikipedia, https://en.wikipedia.org/wiki/Example_City\n"
    "[^2]: Population on 1 January 2026, https://stats.example.org/population/2026\n"
    "[^3]: City guide, https://www.example.net/guide/city")
WEB_CITATIONS = [
    {"n": 1, "label": "Wikipedia", "title": "Example City - Wikipedia", "url": "https://en.wikipedia.org/wiki/Example_City"},
    {"n": 2, "label": "Example Statistics", "title": "Population on 1 January 2026",
     "url": "https://stats.example.org/population/2026"},
    {"n": 3, "label": "example.net", "title": "City guide", "url": "https://www.example.net/guide/city"},
]


def test_web_sources_grounding_a_live_transcript():
    """read_chat_cited_web.txt: the shape of a live web-grounded hNvQHb answer (2026-10-18), synthetic sources: no
    markers in the markdown; field 2 lists each grounded sentence with its sources. The second sentence has two
    (a "+ 1" chip), the third a source with no site name (labelled by its host), the fourth isn't in the answer."""
    messages, _ = parse.chat_messages(parse.rpc_payload(body("read_chat_cited_web.txt"), "hNvQHb"))
    assert messages[1]["text"] == WEB_FOOTNOTED
    assert messages[1]["citations"] == WEB_CITATIONS  # the same source keeps its number: Wikipedia is [^1] twice
    turn = parse.reply_turn(parse.rpc_payload(body("read_chat_cited_web.txt"), "hNvQHb"))
    assert parse.footnote(turn["answer"], turn["refs"], footnotes=False) == (turn["answer"], WEB_CITATIONS)


def cited(*sources):
    """A candidate whose field 43 of [12] maps [cite: 1], [cite: 2]... to web sources (title, url), as live."""
    groups = [[[f"[cite: {n}]", 0], [[None, None, None, [["fav", url, title]]]]] for n, (title, url) in
              enumerate(sources, 1)]
    return [None] * 12 + [[{"44": groups}]]


@pytest.mark.parametrize("text, marked, bare", [
    ("Winter is quiet [cite: 1, 2]. Spring [cite: 1] is busy [cite: 9].",
     "Winter is quiet[^1][^2]. Spring[^1] is busy.", "Winter is quiet. Spring is busy."),  # 9: no such source
    ("[cite: 1] Lead.\n  [cite: 2] Indented.", "[^1] Lead.\n  [^2] Indented.", "Lead.\n  Indented."),
    ("a [cite: 2] [cite: 1] b", "a[^1][^2] b", "a b"),
    ("x [cite: 1]y (see [cite: 2])", "x[^1] y (see[^2])", "x y (see)"),
    ('the "Final Upgrade"[cite: 1].', 'the "Final Upgrade"[^1].', 'the "Final Upgrade".'),
    ("Same [cite: 1] [cite: 1] twice [cite: 7] .", "Same[^1] twice.", "Same twice."),  # 7: no source
])
def test_cite_markers_become_footnote_marks_or_vanish_cleanly(text, marked, bare):
    candidate = cited(("A", "https://a.example/"), ("B", "https://b.example/"))
    refs = parse.cite_refs(text, candidate)
    shown, cites = parse.footnote(text, refs)
    first = text.index("[cite: 1]") < text.index("[cite: 2]") if "[cite: 2]" in text else True
    block = "[^1]: A, https://a.example/\n[^2]: B, https://b.example/" if first else \
        "[^1]: B, https://b.example/\n[^2]: A, https://a.example/"
    assert shown == marked + "\n\n" + (block if "[^2]" in marked else block.split("\n")[0])
    assert parse.footnote(text, refs, footnotes=False) == (bare, cites)


def test_an_answer_without_sources_is_unchanged():
    assert parse.footnote("Just text.  Keep  spacing.", []) == ("Just text.  Keep  spacing.", [])
    assert parse.cite_refs("No cites here.", [None] * 40) == []


def test_chips_without_a_transcript_say_what_they_can():
    chips = [{"title": "Wikipedia", "aria": ""},  # no aria-label: the visible title
             {"title": "PDF", "aria": "View source details for citations from PDF: a.pdf, Example News and "
                                      "MD: c.md. Press Enter to open sources dialog."}]
    text, refs = parse.chip_refs(f"One.{parse.CHIP} Two. {parse.CHIP}\nThree.{parse.CHIP}", chips)
    assert text == "One. Two. \nThree."
    shown, cites = parse.footnote(text, refs)
    assert shown == "One.[^1] Two.[^2][^3][^4]\nThree.\n\n[^1]: Wikipedia\n[^2]: a.pdf\n[^3]: Example News\n[^4]: c.md"
    assert cites[1:] == [{"n": 2, "label": "PDF", "file": "a.pdf"},
                         {"n": 3, "label": "Example News", "title": None, "url": None},
                         {"n": 4, "label": "MD", "file": "c.md"}]


def test_coincident_refs_mark_a_source_once_and_sentences_skip_markers():
    a = {"label": "MD", "file": "a.md"}
    assert parse.footnote("ab", [(1, 1, [a]), (1, 1, [a])])[0] == "a[^1]b\n\n[^1]: a.md"
    web = {"label": "Wikipedia", "title": "T", "url": "https://w.example/"}
    candidate = [None, None, [None, [[["cite"], [1], [["https://w.example/", "T", None, None, None, None, "Wikipedia"]]]]]]
    assert parse.cite_refs("A [cite: 1] cite", candidate)[-1] == (16, 16, [web])  # the visible "cite", not the marker's
