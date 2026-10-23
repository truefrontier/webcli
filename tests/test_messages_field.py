"""webcli messages 0.12.3: the field bugs of 0.12.2, offline against the scripted page in test_messages (made-up
names, 555-01xx numbers). Group sends waited for "Start group conversation" (it comes back after the picker's input
is emptied); a number finds a contact saved with "+1"; a new conversation titled by the contact's name is checked by
its Details' numbers; a sent text with a line break verifies; and read/search show each conversation's numbers."""

from __future__ import annotations

import asyncio

import pytest

from webcli.core.errors import NotFound, UsageError
from webcli.sites.messages import fetch

from test_messages import ROWS, MessagesWeb, calls, run, web  # noqa: F401 (fixtures)
from test_messages_new import CONTACTS, GROUPS, OLDER, creates, row

# Saved with "+1": the live picker finds these only when "+1..." is typed (live_match).
PLUS = [{"name": "Casey Sample", "number": "+1 202-555-0141"},
        {"name": "Dana Twonumber", "number": "+1 202-555-0151"},
        {"name": "", "number": "+1 202-555-0152", "continued": True}]


def field(**options) -> MessagesWeb:
    options.setdefault("contacts", [*CONTACTS, *PLUS])
    return MessagesWeb(older=options.pop("older", OLDER), groups=GROUPS, live_match=True, group_lag=True, **options)


# --- 1. Group send: the group button comes back a moment after the input is emptied ----------------------------------

def test_group_dry_run_waits_for_the_group_button_and_stages_both_chips(web):
    site = web(field())
    out = asyncio.run(fetch.send(["Morgan Example", "2025550141"], "hello", new_ok=True,
                                 expect=["Morgan Example", "Casey Sample"]))
    assert out["dryRun"] and out["group"] and out["staged"] and out["expectRecipientsMatch"]
    assert out["chips"] == ["Morgan Example", "Casey Sample"]
    assert [p["name"] for p in out["recipients"]] == ["Morgan Example", "Casey Sample"]
    assert site.group_mode and site.created == [] and creates(site) == [] and site.fills == [] and site.sent() == []


def test_confirmed_group_send_after_the_lag_creates_once(web):
    site = web(field())
    out = asyncio.run(fetch.send(["Morgan Example", "2025550141"], "hello", new_ok=True, confirm=True,
                                 expect=["Morgan Example", "2025550141"]))
    assert out["sent"] and out["confirmed"] and site.created == [["Morgan Example", "Casey Sample"]]
    assert creates(site) == [True] and site.sent() == ["SEND_MESSAGE"]


def test_no_group_button_at_all_is_refused(web, monkeypatch):
    site = web(field())
    monkeypatch.setattr(fetch, "GROUP_BUTTON_SECONDS", 1)
    site.group_lag, site.group_hidden = False, 10**6
    real = site.click_selector

    def hidden(selector):
        if selector == fetch.SELECTORS["startGroup"]:
            return False
        return real(selector)

    site.page.answers["/* messages:click */"] = hidden
    with pytest.raises(RuntimeError, match=r"REFUSED \(dry run\): Start chat shows no \"Start group conversation\""):
        asyncio.run(fetch.send(["Morgan Example", "2025550141"], "hello", new_ok=True))
    assert creates(site) == [] and site.sent() == []


# --- 2. A number finds a contact saved with "+1" and its older 1:1 --------------------------------------------------

def test_search_by_number_finds_a_plus_one_contacts_older_conversation(web):
    older = [*OLDER, row("207", "Filler Seven"), row("208", "Casey Sample")]
    site = web(field(older=older))
    found = asyncio.run(fetch.search("2025550141"))
    hits = [(r["kind"], r["id"]) for r in found["results"]]
    assert ("conversation", "208") in hits and ("contact", "208") in hits
    assert site.typed_log[:2] == ["2025550141", "+12025550141"]  # national digits found nobody; "+1..." did
    conversation = next(r for r in found["results"] if r["kind"] == "conversation")
    assert conversation["numbers"] == ["+12025550141"] and conversation["number"] == "+12025550141"


def test_read_by_full_name_skips_groups_in_the_newest_rows(web):
    rows = [*ROWS, row("g1", "Morgan Example, Taylor Filler"), row("g2", "Morgan Example, Jamie Filler")]
    site = web(field(rows=rows))
    found = asyncio.run(fetch.messages("Morgan Example"))
    assert found["conversation"]["id"] == "202" and site.thread == "202"


def test_a_two_number_contact_reads_the_conversation_that_uses_the_number(web):
    older = [row("301", "Dana Twonumber"), row("302", "Dana Twonumber")]
    details = {"301": [{"name": "Dana Twonumber", "number": "(202) 555-0151"}],
               "302": [{"name": "Dana Twonumber", "number": "(202) 555-0152"}]}
    web(field(older=older, details=details))
    found = asyncio.run(fetch.messages("202-555-0152"))
    assert found["conversation"]["id"] == "302" and found["conversation"]["numbers"] == ["+12025550152"]
    with pytest.raises(UsageError, match="2 conversations match 'Dana Twonumber'"):  # by name: either one
        asyncio.run(fetch.messages("Dana Twonumber"))


def test_read_by_number_refuses_a_conversation_on_the_contacts_other_number(web):
    details = {"301": [{"name": "Dana Twonumber", "number": "(202) 555-0151"}]}
    web(field(older=[row("301", "Dana Twonumber")], details=details))
    with pytest.raises(NotFound, match="No conversation matching '2025550152'") as caught:
        asyncio.run(fetch.messages("2025550152"))
    assert "(301) uses +12025550151, another of its numbers" in caught.value.hint


def test_a_number_keeps_looking_past_the_contacts_conversation_on_its_other_number(web):
    rows = [*ROWS, row("301", "Dana Twonumber")]
    older = [row("401", "Filler A"), row("402", "Filler B"), row("302", "Dana Twonumber"), row("403", "Filler C")]
    details = {"301": [{"name": "Dana Twonumber", "number": "(202) 555-0151"}],
               "302": [{"name": "Dana Twonumber", "number": "(202) 555-0152"}]}
    site = web(field(rows=rows, older=older, details=details))
    found = asyncio.run(fetch.messages("2025550152"))
    assert found["conversation"]["id"] == "302" and site.loads_more == 2
    site = web(field(rows=rows, older=older, details=details))
    found = asyncio.run(fetch.search("2025550152"))
    ones = {r["id"]: r["numbers"] for r in found["results"] if r["kind"] == "conversation"}
    assert ones == {"301": ["+12025550151"], "302": ["+12025550152"]}
    assert [(r["id"], r["number"]) for r in found["results"] if r["kind"] == "contact"] == [
        ("301", "+12025550151"), ("302", "+12025550152")]  # each of the contact's numbers: its own conversation
    out = asyncio.run(fetch.send("2025550152", "hi", expect=["2025550152"]))
    assert out["threadId"] == "302" and out["numbers"] == ["+12025550152"] and site.sent() == []


# --- 3. --new-ok to a number: the conversation shows the contact's name ---------------------------------------------

def test_new_ok_to_a_number_passes_when_the_details_show_that_number(web):
    # The picker doesn't offer the contact (saved some other way): "Send to" the number; the conversation that
    # opens is titled by the contact's name, and its Details show the number.
    site = web(field(contacts=CONTACTS, new_title="Casey Sample"))
    out = asyncio.run(fetch.send("2025550141", "hello", new_ok=True, confirm=True, expect=["2025550141"]))
    assert out["sent"] and out["created"] and site.created == [["2025550141"]] and site.sent() == ["SEND_MESSAGE"]


def test_new_ok_to_a_number_found_by_plus_one_is_the_contact(web):
    site = web(field())
    out = asyncio.run(fetch.send("2025550141", "hello", new_ok=True, confirm=True, expect=["2025550141"]))
    assert out["recipients"][0]["name"] == "Casey Sample" and site.created == [["Casey Sample"]]
    assert out["sent"] and site.sent() == ["SEND_MESSAGE"]


def test_new_ok_refuses_when_the_details_show_another_number_or_nothing(web):
    site = web(field(contacts=CONTACTS, new_title="Casey Sample",
                     details={"n1": [{"name": "Casey Sample", "number": "(202) 555-0148"}]}))
    with pytest.raises(RuntimeError, match=r"REFUSED: the conversation that opened is with \+12025550148"):
        asyncio.run(fetch.send("2025550141", "hello", new_ok=True, confirm=True, expect=["2025550141"]))
    assert site.fills == [] and site.sent() == []
    site = web(field(contacts=CONTACTS, new_title="Casey Sample", details={"n1": []}))
    with pytest.raises(RuntimeError, match="REFUSED: the conversation that opened is titled 'Casey Sample'"):
        asyncio.run(fetch.send("2025550141", "hello", new_ok=True, confirm=True, expect=["2025550141"]))
    assert site.fills == [] and site.sent() == []


def test_existing_conversation_by_number_is_checked_by_its_details(web):
    details = {"301": [{"name": "Dana Twonumber", "number": "(202) 555-0151"}]}
    site = web(field(older=[row("301", "Dana Twonumber")], details=details))
    out = asyncio.run(fetch.send("2025550151", "hi", expect=["2025550151"]))
    assert out["dryRun"] and out["threadId"] == "301" and out["numbers"] == ["+12025550151"]
    assert out["expectRecipientsMatch"] and site.fills == []
    with pytest.raises(RuntimeError, match=r"REFUSED \(dry run\): --expect-recipient \(2025550159\) doesn't name"):
        asyncio.run(fetch.send("Dana Twonumber", "hi", expect=["2025550159"]))
    site.details["301"] = []
    with pytest.raises(RuntimeError, match=r"REFUSED: couldn't read which number"):
        asyncio.run(fetch.send("2025550151", "hi", confirm=True, expect=["2025550151"]))
    assert site.sent() == [] and site.fills == []


# --- 4. A sent text with a line break verifies ----------------------------------------------------------------------

def test_a_sent_text_with_a_line_break_verifies_once(web):
    # The bubble reads back trimmed, its line break as "\n" (MESSAGES_JS; see test_messages_dom): the same text.
    site = web(field(bubble_text=lambda text: text.strip().replace("\r\n", "\n")))
    out = asyncio.run(fetch.send("Alice Smith", "Line one\r\nLine two ", confirm=True))
    assert out["sent"] is True and out["confirmed"]
    assert site.sent() == ["SEND_MESSAGE"] and len(site.delivered) == 1 and site.clicks == 1


# --- 5. read and search show the numbers -----------------------------------------------------------------------------

def test_read_shows_the_conversations_numbers(web):
    web(field())
    found = asyncio.run(fetch.messages("alice"))
    assert found["conversation"]["numbers"] == ["+12025550188"]  # the fake's default number
    found = asyncio.run(fetch.messages("+1 (555) 010-2000"))
    assert found["conversation"]["numbers"] == ["+15550102000"]
    assert found["conversation"]["participants"] == [{"name": "", "number": "+15550102000"}]


def test_search_numbers_can_be_skipped(web):
    site = web(field())
    found = asyncio.run(fetch.search("Morgan Example", numbers=False))
    assert all("numbers" not in r for r in found["results"]) and site.details_reads == 0


def test_cli_read_notes_the_numbers_and_search_can_skip_them(calls):
    seen, answers = calls
    answers["messages"] = {"conversation": {"id": "301", "name": "Dana Twonumber", "numbers": ["+12025550152"]},
                           "messages": []}
    result = run("read", "2025550152")
    assert result.exit_code == 0 and "Conversation: Dana Twonumber (301, +12025550152)" in result.stderr
    answers["search"] = {"results": [{"kind": "conversation", "id": "301", "name": "Dana Twonumber",
                                      "number": "+12025550152", "numbers": ["+12025550152"], "preview": "",
                                      "timestamp": "", "unread": False, "matched": ["contact"]}],
                         "scanned": 3, "complete": True}
    assert run("search", "Dana", "--no-numbers").exit_code == 0 and seen[-1][3] == {"numbers": False, "headed": False}
    result = run("search", "Dana")
    assert result.exit_code == 0 and seen[-1][2:] == (("Dana", None), {"headed": False}) and "+12025550152" in result.stdout
