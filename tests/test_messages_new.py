"""webcli messages 0.12: phone-number normalizing, the deeper lookup (Start chat's contact search and paced "Load
more"), ambiguity, `send --new-ok` (new 1:1 and group conversations: dry runs, the recipient checks, the guard's one
GET_OR_CREATE_CONVERSATION), and the one-session lock. All offline against the scripted page in test_messages; every
name and number is made up (555-01xx)."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time

import pytest

from webcli.core import browser, daemon, guard
from webcli.core.errors import NotFound, ProfileBusy, UsageError
from webcli.sites.messages import fetch, parse, session
from webcli.sites.messages.guard import SendScope, request_allowed

from test_messages import A, ROWS, SEND, MessagesWeb, calls, pblite, run, web  # noqa: F401 (fixtures)


def row(id_: str, name: str, preview: str = "hi") -> dict:
    return {"href": f"/web/conversations/{id_}", "name": name, "preview": preview, "timestamp": "Jan 2",
            "unread": False}


OLDER = [row("201", "Taylor Filler"), row("202", "Morgan Example"), row("203", "Riley Example"),
         row("204", "(202) 555-0150"), row("205", "Jamie Filler"), row("206", "Drew Filler")]
CONTACTS = [
    {"name": "Morgan Example", "number": "(202) 555-0111"},
    {"name": "Riley Example", "number": "(202) 555-0112"},
    {"name": "", "number": "(202) 555-0113 Work", "continued": True},  # Riley's second number
    {"name": "Avery Sample", "number": "+1 202-555-0131"},
    {"name": "Avery Other", "number": "+1 202-555-0132"},
    {"name": "Quinn Nophone", "number": "(202) 555-0190"},
]
GROUPS = [{"href": "/web/conversations/g77", "name": "Quinn Nophone, Taylor Filler"}]


def deep(**options) -> MessagesWeb:
    return MessagesWeb(older=OLDER, contacts=CONTACTS, groups=GROUPS, **options)


def creates(site: MessagesWeb) -> list[bool]:
    return [ok for action, ok in site.wire if action == "GET_OR_CREATE_CONVERSATION"]


# --- Phone numbers -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("text", "e164"), [
    ("2025550143", "+12025550143"), ("(202) 555-0143", "+12025550143"), ("202.555.0143", "+12025550143"),
    ("+1 202 555 0143", "+12025550143"), ("1-202-555-0143", "+12025550143"), ("tel:+12025550143", "+12025550143"),
    ("+44 20 7946 0958", "+442079460958"), ("011 44 20 7946 0958", "+442079460958"),
    ("555-0143", None), ("12345", None), ("Morgan", None), ("+1 202 555 01x3", None), ("", None)])
def test_numbers_normalize_to_e164_with_a_us_default(text, e164):
    assert parse.normalize_number(text) == e164
    assert parse.is_number(text) is (e164 is not None)


def test_same_number_and_what_the_picker_is_typed():
    assert parse.same_number("(202) 555-0143", "+1 202.555.0143")
    assert not parse.same_number("(202) 555-0143", "(202) 555-0144")
    assert parse.national("+12025550143") == "2025550143" and parse.national("+442079460958") == "+442079460958"


# --- Parsing the picker and resolving --------------------------------------------------------------------------

def test_continuation_rows_are_more_numbers_of_the_contact_above():
    people = parse.contacts(CONTACTS)
    riley = next(c for c in people if c["name"] == "Riley Example")
    assert [n["number"] for n in riley["numbers"]] == ["+12025550112", "+12025550113"]
    assert len(people) == 5


def test_resolve_deep_tiers_and_ambiguity():
    rows = parse.conversations([*ROWS, *OLDER])
    people = parse.contacts(CONTACTS)
    groups = parse.groups(GROUPS)
    assert parse.resolve_deep(rows, "morgan example", people)["id"] == "202"  # exact name
    assert parse.resolve_deep(rows, "2025550111", people)["id"] == "202"  # the contact who has the number
    assert parse.resolve_deep(rows, "+1 (202) 555-0150", people)["id"] == "204"  # shown by its number
    assert parse.resolve_deep(rows, "Quinn", people, groups)["id"] == "g77"  # the group search's result
    with pytest.raises(UsageError, match="2 conversations match 'Example'") as caught:
        parse.resolve_deep(rows, "Example", people)
    assert "Morgan Example (202)" in str(caught.value) and "Riley Example (203)" in str(caught.value)
    # Two contacts fit "Avery": one conversation is enough to read, never to send.
    rows_one = [*rows, *parse.conversations([row("301", "Avery Sample")])]
    assert parse.resolve_deep(rows_one, "Avery", people)["id"] == "301"
    with pytest.raises(UsageError, match="2 contacts match 'Avery'"):
        parse.resolve_deep(rows_one, "Avery", people, strict=True)
    assert parse.resolve_deep(rows_one, "Avery Sample", people, strict=True)["id"] == "301"


def test_recipients_resolve_to_one_person_or_refuse():
    people = parse.contacts(CONTACTS)
    assert parse.recipient("(202) 555-0111", people) == {"query": "(202) 555-0111", "name": "Morgan Example",
                                                         "number": "+12025550111"}
    assert parse.recipient("2025550199", people)["name"] == ""  # a number no contact has: itself
    with pytest.raises(UsageError, match="2 contacts match 'Avery'"):
        parse.recipient("Avery", people)
    with pytest.raises(UsageError, match="has 2 numbers"):
        parse.recipient("Riley", people)
    with pytest.raises(NotFound):
        parse.recipient("Nobody Here", people)


def test_expect_recipients_must_match_exactly():
    morgan = {"name": "Morgan Example", "number": "+12025550111"}
    stranger = {"name": "", "number": "+12025550199"}
    assert parse.expect_matches(["morgan  example", "202-555-0199"], [morgan, stranger])
    assert parse.expect_matches(["+12025550111"], [morgan])
    assert not parse.expect_matches(["Morgan Example"], [morgan, stranger])  # one short
    assert not parse.expect_matches(["Morgan", "2025550199"], [morgan, stranger])  # not the full name
    assert not parse.expect_matches(["Morgan Example", "Morgan Example"], [morgan, stranger])


# --- The deeper lookup --------------------------------------------------------------------------------------------

def test_search_finds_older_conversations_through_contacts_and_load_more(web):
    site = web(deep())
    found = asyncio.run(fetch.search("2025550112"))  # Riley's number: the picker names Riley, then the list loads
    hits = [(r["kind"], r["id"], r["matched"]) for r in found["results"]]
    assert ("conversation", "203", ["contact"]) in hits and ("contact", "203", ["contact"]) in hits
    assert found["scanned"] == 8 and site.loads_more == 2 and found["complete"] is False  # stopped once found
    assert site.typed_log == ["2025550112"]
    assert {action for action, ok in site.wire if ok} <= {"LIST_CONVERSATIONS"}  # reads only
    assert site.path == ""  # the page left the app before the session lock went


def test_search_by_number_finds_a_conversation_shown_by_it(web):
    site = web(deep())
    found = asyncio.run(fetch.search("202.555.0150"))
    assert [(r["id"], r["number"]) for r in found["results"]] == [("204", "+12025550150")]
    assert site.loads_more == 2


def test_search_with_no_contact_scans_to_the_cap(web):
    site = web(deep())
    found = asyncio.run(fetch.search("zebra", pages=1))
    assert found == {"results": [], "scanned": 6, "complete": False} and site.loads_more == 1
    site = web(deep())
    found = asyncio.run(fetch.search("Filler"))
    assert found["complete"] is True and {r["id"] for r in found["results"]} == {"201", "205", "206", "g77"}


def test_read_finds_an_older_conversation_and_a_group(web):
    site = web(deep())
    assert asyncio.run(fetch.messages("Morgan Example"))["conversation"]["id"] == "202"
    assert site.thread == "202" and ("MESSAGE_READ", False) in site.wire
    site = web(deep())
    assert asyncio.run(fetch.messages("Quinn"))["conversation"]["id"] == "g77"  # only in the group results
    assert site.thread == "g77"


def test_ambiguous_deep_match_exits_2_listing_candidates(web):
    web(deep())
    with pytest.raises(UsageError, match="2 conversations match 'Example'"):
        asyncio.run(fetch.messages("Example"))


def test_not_found_says_when_the_contact_has_no_conversation(web):
    web(deep())
    with pytest.raises(NotFound) as caught:
        asyncio.run(fetch.messages("Avery Sample"))
    assert "is a contact with no conversation" in caught.value.hint and "--new-ok" in caught.value.hint


# --- send: existing, and never a new conversation without --new-ok -------------------------------------------------

def test_send_finds_an_older_conversation_but_never_an_ambiguous_one(web):
    site = web(deep(rows=[*ROWS, row("301", "Avery Sample")]))
    with pytest.raises(UsageError, match="2 contacts match 'Avery'"):
        asyncio.run(fetch.send("Avery", "hi", confirm=True))
    assert site.sent() == [] and site.fills == []
    site = web(deep())
    out = asyncio.run(fetch.send("2025550111", "hi"))
    assert out == {"dryRun": True, "to": "Morgan Example", "threadId": "202", "text": "hi"} and site.fills == []


def test_no_new_conversation_without_new_ok(web):
    site = web(deep())
    with pytest.raises(NotFound):
        asyncio.run(fetch.send("2025550199", "hi", confirm=True))
    with pytest.raises(UsageError, match="only with --new-ok"):
        asyncio.run(fetch.send(["2025550111", "2025550199"], "hi", confirm=True))
    assert site.created == [] and creates(site) == [] and site.sent() == []


def test_without_new_ok_the_guard_never_lets_a_conversation_be_created():
    create = pblite(A["GET_OR_CREATE_CONVERSATION"])
    plain = SendScope()
    plain.arm(10)
    assert not request_allowed("POST", SEND, create, scope=plain)
    with pytest.raises(RuntimeError):
        plain.arm_create(10)
    scope = SendScope(new_ok=True)
    assert not request_allowed("POST", SEND, create, scope=scope)  # not armed
    scope.arm_create(10)
    assert request_allowed("POST", SEND, create, scope=scope) and scope.created
    assert not request_allowed("POST", SEND, create, scope=scope)  # one only


# --- send --new-ok -----------------------------------------------------------------------------------------------

def test_new_one_to_one_dry_run_resolves_without_typing_a_message(web):
    site = web(deep())
    out = asyncio.run(fetch.send("(202) 555-0199", "hello", new_ok=True))
    assert out["dryRun"] and out["new"] and not out["group"] and out["threadId"] is None
    assert out["recipients"] == [{"query": "(202) 555-0199", "name": "", "number": "+12025550199"}]
    assert out["expectRecipients"] is None and out["expectRecipientsMatch"] is None
    assert site.fills == [] and site.created == [] and creates(site) == []  # no compose box, nothing created


def test_new_group_dry_run_lists_each_recipient(web):
    site = web(deep())
    out = asyncio.run(fetch.send(["Avery Sample", "+1 202 555 0199"], "hello", new_ok=True,
                                 expect=["Avery Sample", "2025550199"]))
    assert out["group"] and [p["number"] for p in out["recipients"]] == ["+12025550131", "+12025550199"]
    assert out["recipients"][0]["name"] == "Avery Sample" and out["expectRecipientsMatch"] is True
    assert site.fills == [] and creates(site) == []


def test_new_ok_with_an_existing_conversation_sends_there(web):
    web(deep())
    out = asyncio.run(fetch.send("2025550111", "hi", new_ok=True))
    assert out["threadId"] == "202" and "new" not in out


def test_ambiguous_recipient_is_refused(web):
    site = web(deep())
    with pytest.raises(UsageError, match="2 contacts match 'Avery'"):
        asyncio.run(fetch.send(["Avery", "2025550199"], "hi", new_ok=True, confirm=True, expect=["x", "y"]))
    with pytest.raises(UsageError, match="has 2 numbers"):
        asyncio.run(fetch.send(["Riley", "2025550199"], "hi", new_ok=True))
    assert creates(site) == [] and site.sent() == []


def test_recipient_mismatch_is_refused_dry_run_and_confirmed(web):
    site = web(deep())
    with pytest.raises(RuntimeError, match=r"REFUSED \(dry run\): --expect-recipient"):
        asyncio.run(fetch.send("2025550199", "hi", new_ok=True, expect=["2025550198"]))
    with pytest.raises(RuntimeError, match=r"^REFUSED: --expect-recipient"):
        asyncio.run(fetch.send(["Avery Sample", "2025550199"], "hi", new_ok=True, confirm=True,
                               expect=["Avery Sample"]))
    with pytest.raises(UsageError, match="needs --expect-recipient"):
        asyncio.run(fetch.send("2025550199", "hi", new_ok=True, confirm=True))
    assert creates(site) == [] and site.sent() == [] and site.fills == []


def test_confirmed_new_one_to_one_creates_once_checks_the_title_and_verifies(web):
    site = web(deep())
    out = asyncio.run(fetch.send("2025550199", "hello", new_ok=True, confirm=True, expect=["+1 202-555-0199"]))
    assert out["sent"] and out["confirmed"] and out["created"] and out["threadId"] == "n1" and out["titleChecked"]
    assert site.created == [["2025550199"]] and creates(site) == [True]
    assert site.delivered == [("n1", "hello")] and site.sent() == ["SEND_MESSAGE"]


def test_confirmed_new_group_picks_exactly_the_recipients(web):
    site = web(deep())
    out = asyncio.run(fetch.send(["Morgan Example", "2025550199"], "hello", new_ok=True, confirm=True,
                                 expect=["Morgan Example", "202 555 0199"]))
    assert out["group"] and out["confirmed"] and site.created == [["Morgan Example", "2025550199"]]
    assert creates(site) == [True] and site.sent() == ["SEND_MESSAGE"]


def test_a_new_conversation_titled_otherwise_is_refused_before_typing(web):
    site = web(deep(new_title="Someone Else"))
    with pytest.raises(RuntimeError, match="REFUSED: the conversation that opened is titled 'Someone Else'"):
        asyncio.run(fetch.send("2025550199", "hello", new_ok=True, confirm=True, expect=["2025550199"]))
    assert site.fills == [] and site.sent() == [] and site.delivered == []


# --- CLI ------------------------------------------------------------------------------------------------------------

def test_cli_send_new_syntax_and_exit_codes(calls):
    seen, answers = calls

    def answer(query, text, *, confirm, headed, new_ok=False, expect=None, pages=None):
        if expect and expect != ["Ok Person"]:
            raise RuntimeError("REFUSED (dry run): --expect-recipient (x) doesn't name exactly the recipients.")
        people = [query] if isinstance(query, str) else query
        return {"dryRun": not confirm, "new": True, "group": len(people) > 1, "threadId": None, "text": text,
                "recipients": [{"query": q, "name": "", "number": q} for q in people], "to": ", ".join(people),
                "expectRecipients": expect, "expectRecipientsMatch": None if expect is None else True}

    answers["send"] = answer
    result = run("send", "--to", "+12025550199", "--to", "Ok Person", "--text", "hi", "--new-ok")
    assert result.exit_code == 0 and json.loads(result.stdout)["group"] is True
    assert seen[-1][2:] == ((["+12025550199", "Ok Person"], "hi"),
                            {"confirm": False, "new_ok": True, "headed": False})
    assert run("send", "--to", "a", "--to", "b", "--text", "hi").exit_code == 2  # no --new-ok
    missing = run("send", "--to", "a", "--text", "hi", "--new-ok", "--confirm")
    assert missing.exit_code == 2 and "missing_expect" in missing.stderr
    assert run("send", "--to", "a", "--text", "hi", "--new-ok", "--expect-recipient", "x").exit_code == 5
    assert len(seen) == 2  # the usage errors never reached the daemon
    run("send", "--to", "a", "--text", "hi", "--new-ok", "--expect-recipients", "Ok Person", "--pages", "3")
    assert seen[-1][3] == {"confirm": False, "new_ok": True, "expect": ["Ok Person"], "pages": 3, "headed": False}


def test_busy_and_open_elsewhere_errors_cross_the_daemon_with_their_name():
    busy = ProfileBusy("Google Messages is busy.", error="messages_busy")
    payload = daemon.error_payload(busy)
    assert payload["error"] == "messages_busy"
    back = daemon.rebuild(payload)
    assert isinstance(back, ProfileBusy) and back.error == "messages_busy"


def test_cli_busy_exits_5_with_the_error_name(calls):
    _, answers = calls

    def busy(*_a, **_k):
        raise ProfileBusy("Google Messages is busy: another webcli messages command has it open.",
                          error="messages_busy")

    answers["conversations"] = busy
    result = run("list")
    assert result.exit_code == 5 and "messages_busy" in result.stderr


# --- One session at a time ------------------------------------------------------------------------------------------

def test_the_session_lock_is_exclusive_and_waits_then_says_busy():
    async def scenario():
        async with session.hold(wait=1):
            started = time.monotonic()
            with pytest.raises(ProfileBusy, match="Google Messages is busy") as caught:
                async with session.hold(wait=0.3):
                    pass
            assert caught.value.error == "messages_busy" and time.monotonic() - started >= 0.25
        async with session.hold(wait=0):  # free again
            pass

    asyncio.run(scenario())


def test_a_waiting_command_gets_the_session_when_it_frees():
    order = []

    async def first():
        async with session.hold(wait=1):
            order.append("first")
            await asyncio.sleep(0.4)
        order.append("first done")

    async def second():
        await asyncio.sleep(0.1)
        async with session.hold(wait=3):
            order.append("second")

    async def both():
        await asyncio.gather(first(), second())

    asyncio.run(both())
    assert order == ["first", "first done", "second"]


def test_another_process_holding_the_session_makes_commands_busy(web, monkeypatch):
    holder = subprocess.Popen([sys.executable, "-c", (
        "import fcntl, sys, time\n"
        f"f = open({str(session.lock_path())!r}, 'a+'); fcntl.flock(f, fcntl.LOCK_EX); f.write('4242\\n'); f.flush()\n"
        "print('held', flush=True); time.sleep(30)\n")], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        site = web(MessagesWeb())
        monkeypatch.setattr(browser, "lock_wait", 0.3)
        with pytest.raises(ProfileBusy, match=r"\(pid 4242\)") as caught:
            asyncio.run(fetch.conversations())
        assert caught.value.error == "messages_busy" and site.wire == []  # never opened Messages
    finally:
        holder.kill()
        holder.wait()
    assert len(asyncio.run(fetch.conversations())) == 4
