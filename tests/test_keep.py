"""webcli keep, offline: the fail-closed guard, the changes parser, the notes sync read off a scripted page, and the
commands over a faked daemon."""

from __future__ import annotations

import asyncio
import json

import pytest
from click.testing import CliRunner

from webcli.apps import webcli as webcli_app
from webcli.core import daemon, guard
from webcli.core.errors import AuthRequired
from webcli.sites.keep import fetch, parse
from webcli.sites.keep.guard import POLICY, request_allowed

from google_fakes import Response, ScriptedPage, install

CHANGES = "https://www.googleapis.com/notes/v1/changes"
READ = json.dumps({"nodes": [], "clientTimestamp": "2026-10-19T00:00:00Z", "requestHeader": {}})


def node(id_, title="", text="", *, type_="NOTE", parent="root", archived=False, edited="2026-10-01T00:00:00Z"):
    return {"kind": "notes#node", "id": id_, "serverId": f"srv-{id_}", "type": type_, "parentId": parent,
            "title": title, "indexableText": text, "isArchived": archived, "isPinned": False, "color": "DEFAULT",
            "timestamps": {"created": "2026-01-01T00:00:00Z", "userEdited": edited}, "labelIds": [{"labelId": "l1"}]}


# The guard.

@pytest.mark.parametrize(("method", "url", "body"), [
    ("GET", "https://keep.google.com/u/0/", None),
    ("GET", "https://www.gstatic.com/og/_/js/x.js", None),
    ("POST", CHANGES, READ),
    ("POST", "https://notes-pa.clients6.google.com/notes/v1/changes", READ),
    ("OPTIONS", CHANGES, None),
    ("POST", "https://accounts.google.com/RotateCookies", None),
])
def test_listed_reads_pass(method, url, body):
    assert request_allowed(method, url, body)


@pytest.mark.parametrize(("method", "url", "body"), [
    ("POST", CHANGES, json.dumps({"nodes": [node("n1", "edited")]})),  # uploads a note edit
    ("POST", CHANGES, json.dumps({"nodes": [], "userInfo": {"labels": [{"name": "x"}]}})),  # a label change
    ("POST", CHANGES, "not json"),
    ("POST", CHANGES, None),
    ("POST", CHANGES, "[]"),
    ("PUT", CHANGES, READ),
    ("POST", "http://www.googleapis.com/notes/v1/changes", READ),
    ("POST", "https://evil.example/notes/v1/changes", READ),
    ("POST", "https://www.googleapis.com/notes/v1/changes/extra", READ),
    ("POST", "https://keep.google.com/notes/v1/changes", READ),  # not an API host
    ("POST", "https://notes-pa.clients6.google.com/v1/notes:batchDelete", "{}"),
    ("POST", "https://play.google.com/log?format=json", "{}"),
    ("POST", "https://signaler-pa.clients6.google.com/punctual/multi-watch/channel", "{}"),
    ("GET", "https://www.google-analytics.com/collect", None),
    ("GET", "https://accounts.google.com/Logout", None),
    ("DELETE", "https://keep.google.com/u/0/", None),
])
def test_writes_unknown_posts_and_other_hosts_are_blocked(method, url, body):
    assert not request_allowed(method, url, body)


def test_no_command_running_means_nothing_passes():
    class Request:
        method, url, post_data = "GET", "https://keep.google.com/u/0/", None

    guard.state.begin(POLICY)
    assert guard.check(Request(), own_page=True)
    assert not guard.check(Request(), own_page=False)  # another page
    guard.state.end()
    assert not guard.check(Request(), own_page=True)


# The parser.

def test_note_rows_keep_top_level_notes_and_lists_only():
    assert parse.note(node("n1", "Groceries", "milk")) == {
        "id": "n1", "server_id": "srv-n1", "type": "NOTE", "title": "Groceries", "text": "milk", "pinned": False,
        "archived": False, "color": "DEFAULT", "updated": "2026-10-01T00:00:00Z", "created": "2026-01-01T00:00:00Z",
        "labels": ["l1"]}
    assert parse.note(node("l1", type_="LIST"))["type"] == "LIST"
    assert parse.note(node("i1", type_="LIST_ITEM", parent="l1")) is None
    assert parse.note(node("c1", parent="l1")) is None
    assert parse.note({"kind": "notes#blob"}) is None and parse.note("x") is None


def test_read_only_sync():
    assert parse.read_only_sync(READ) and parse.read_only_sync("{}")
    assert not parse.read_only_sync(json.dumps({"nodes": [{"id": "n1"}]}))
    assert not parse.read_only_sync(json.dumps({"userInfo": {"labels": [{"mainId": "x"}]}}))
    assert not parse.read_only_sync(json.dumps({"userInfo": {"labels": []}, "nodes": []}))  # any userInfo: fail closed
    assert not parse.read_only_sync("[]") and not parse.read_only_sync("") and not parse.read_only_sync(None)


def test_display_title_find_and_text():
    rows = [parse.note(node("abc123", "", "first line\nsecond")), parse.note(node("xyz789", "Title", "body"))]
    assert parse.display_title(rows[0]) == "first line" and parse.display_title({"text": ""}) == "(untitled)"
    assert parse.find(rows, "srv-xyz789")["id"] == "xyz789"
    assert parse.find(rows, "c12")["id"] == "abc123" and parse.find(rows, "nope") is None
    assert parse.text(rows[1]) == "Title\n\nbody\n" and parse.text(rows[0]) == "first line\nsecond\n"


# The notes sync, read off a scripted page.

def keep_page(*answers, redirect=None):
    responses = [Response(CHANGES, answer) for answer in answers]
    responses.insert(0, Response("https://keep.google.com/u/0/some.js", {"nodes": [node("ignored")]}, method="GET"))
    redirects = {fetch.APP_URL: redirect} if redirect else {}
    return ScriptedPage(redirects=redirects, responses={fetch.APP_URL: responses})


def test_notes_collects_every_sync_page_newest_first(monkeypatch):
    page = install(monkeypatch, keep_page(
        {"nodes": [node("old", "Old", edited="2026-01-01T00:00:00Z")], "truncated": True},
        {"nodes": [node("new", "New", edited="2026-10-01T00:00:00Z"), node("arch", "A", archived=True),
                   node("item", parent="old")], "truncated": False},
    ))
    result = asyncio.run(fetch.notes())
    assert [n["id"] for n in result["notes"]] == ["new", "arch", "old"] and result["complete"] is True
    assert page.policy is POLICY and page.gotos == [fetch.APP_URL]


def test_a_sync_that_never_finishes_is_incomplete(monkeypatch):
    monkeypatch.setattr(fetch, "SYNC_SECONDS", 0.01)
    install(monkeypatch, keep_page({"nodes": [node("n1", "One")], "truncated": True}))
    result = asyncio.run(fetch.notes())
    assert [n["id"] for n in result["notes"]] == ["n1"] and result["complete"] is False


def test_no_sync_at_all_names_what_the_guard_blocked(monkeypatch):
    monkeypatch.setattr(fetch, "SYNC_SECONDS", 0.01)
    install(monkeypatch, keep_page())
    guard.state.blocked.append("POST notes-pa.clients6.google.com/v2/sync")
    with pytest.raises(RuntimeError, match=r"no notes sync.*The guard blocked: POST notes-pa\.clients6"):
        asyncio.run(fetch.notes())


def test_signed_out_is_auth_required(monkeypatch):
    install(monkeypatch, keep_page(redirect="https://accounts.google.com/ServiceLogin?continue=keep"))
    with pytest.raises(AuthRequired, match="accounts.google.com"):
        asyncio.run(fetch.notes())
    install(monkeypatch, keep_page(redirect="https://workspace.google.com/products/keep/"))
    assert asyncio.run(fetch.status()) == {"signedIn": False, "email": None}


# The commands, over a faked daemon.

NOTES = [parse.note(node("n1", "Picnic plan", "bring blanket", edited="2026-10-02T00:00:00Z")),
         parse.note(node("n2", "", "Archived picnic idea", archived=True)),
         parse.note(node("n3", "Taxes", "receipts"))]


@pytest.fixture
def keep(monkeypatch):
    calls = []

    async def call(ident, site, op, *args, **kwargs):
        calls.append((ident.id, site, op, args, kwargs))
        return {"notes": NOTES, "complete": True} if op == "notes" else {"signedIn": True, "email": None}

    monkeypatch.setattr(daemon, "call", call)
    return calls


def run(*args):
    return CliRunner().invoke(webcli_app.build(), ["keep", *args])


def test_list_leaves_archived_out_unless_asked(keep):
    assert [n["id"] for n in json.loads(run("list").stdout)] == ["n1", "n3"]
    assert run("list", "--archived", "--id-only").stdout == "n1\nn2\nn3\n"
    assert json.loads(run("list", "--compact", "--limit", "1").stdout) == [
        {"id": "n1", "type": "NOTE", "title": "Picnic plan", "updated": "2026-10-02T00:00:00Z", "pinned": False,
         "archived": False}]
    assert keep[0][:3] == ("google:default", "keep", "notes") and keep[0][4] == {"headed": False}


def test_search_matches_title_text_and_id_and_exits_3_when_nothing_does(keep):
    assert [n["id"] for n in json.loads(run("search", "PICNIC").stdout)] == ["n1"]
    assert [n["id"] for n in json.loads(run("search", "picnic", "--archived").stdout)] == ["n1", "n2"]
    result = run("search", "nothing like this")
    assert result.exit_code == 3 and json.loads(result.stderr)["code"] == 3


def test_info_and_get_find_archived_notes_too(keep, tmp_path):
    assert json.loads(run("info", "srv-n2").stdout)["id"] == "n2"
    assert run("info", "n3", "--format", "text").stdout == "Taxes\n\nreceipts\n"
    assert run("get", "n1").stdout == "Picnic plan\n\nbring blanket\n"
    out = tmp_path / "note.txt"
    result = run("get", "n1", "-o", str(out))
    assert result.exit_code == 0 and out.read_text() == "Picnic plan\n\nbring blanket\n"
    missing = run("info", "zzz")
    assert missing.exit_code == 3 and "webcli keep list" in json.loads(missing.stderr)["hint"]


def test_an_incomplete_sync_warns_on_stderr(monkeypatch):
    async def call(*_args, **_kwargs):
        return {"notes": NOTES, "complete": False}

    monkeypatch.setattr(daemon, "call", call)
    result = run("list")
    assert result.exit_code == 0 and "may be incomplete" in result.stderr
    assert "may be incomplete" not in run("list", "-q").stderr


def test_status_exits_4_when_signed_out(monkeypatch):
    async def call(*_args, **_kwargs):
        return {"signedIn": False, "email": None}

    monkeypatch.setattr(daemon, "call", call)
    result = run("status")
    assert result.exit_code == 4 and json.loads(result.stdout) == {"signedIn": False, "email": None}
