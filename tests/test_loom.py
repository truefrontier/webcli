"""webcli loom, offline: the guard (Loom's hosts only; GraphQL only when every operation is a query), the GraphQL
reader it uses, the library / video / transcript flows on a scripted tab, and the commands over a faked daemon.
Read-only; nothing reaches Loom; no Chrome starts.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from webcli.apps import webcli as webcli_app
from webcli.core import browser, daemon, guard
from webcli.core.errors import AuthRequired, NotFound
from webcli.sites.loom import fetch, parse
from webcli.sites.loom.guard import POLICY, definitions, read_only_document, request_allowed

from conftest import make_profile
from google_fakes import Response, ScriptedPage, install

VID = "0123456789abcdef0123456789abcdef"
GRAPHQL = "https://www.loom.com/graphql"
LIBRARY = "https://www.loom.com/looms/videos"
TRANSCRIPT = f"https://cdn.loom.com/mediametadata/transcription/{VID}-3.json?Policy=x&Signature=y&Key-Pair-Id=z"


def body(query: str, name: str = "Op") -> str:
    return json.dumps({"operationName": name, "query": query, "variables": {}})


# The GraphQL reader and the guard.

@pytest.mark.parametrize(("doc", "kinds"), [
    ("query A { a }", ["query"]),
    ("{ a }", ["{"]),
    ("query A($x: In = {a: 1}) { f(s: \"}\") } # mutation { x }\n fragment F on T { y }", ["query", "fragment"]),
    ('query A { f(s: """ mutation { x } \\""" """) }', ["query"]),
    ("mutation M { delete }", ["mutation"]),
    ("query A { a } mutation B { b }", ["query", "mutation"]),
    ("subscription S { s }", ["subscription"]),
    ("query A { a } #\r mutation B { b }", ["query", "mutation"]),  # a comment ends at \r too
    ("query A { a } # c\r\n mutation B { b }", ["query", "mutation"]),
    ('query A { f(s: "x\r") } mutation B { b }', None),  # a string can't span a line
    ("query A { a", None), ("query A { a } }", None), ('query A { f(s: "open) }', None), ("query A(", None),
])
def test_definitions_reads_each_top_level_operation(doc, kinds):
    assert definitions(doc) == kinds


def test_the_bundled_queries_are_read_only_documents():
    for name in ("GetLoomsForLibrary", "GetVideoCardDetails"):
        assert read_only_document(parse.query(name)["query"]), name
    assert not read_only_document("fragment F on T { x }")  # defines no operation
    assert not read_only_document(None) and not read_only_document("")


@pytest.mark.parametrize(("method", "url", "data", "ok"), [
    ("GET", LIBRARY, None, True),
    ("GET", f"https://www.loom.com/share/{VID}", None, True),
    ("GET", TRANSCRIPT, None, True),
    ("GET", "https://cdn.loom.com/assets/app.js", None, True),
    ("POST", GRAPHQL, body("query GetLoomsForLibrary { getLooms { __typename } }"), True),
    ("POST", GRAPHQL, json.dumps([json.loads(body("query A { a }")), json.loads(body("{ b }"))]), True),
    ("POST", GRAPHQL, body("mutation RecordView($id: ID!) { recordView(id: $id) }"), False),
    ("POST", GRAPHQL, json.dumps([json.loads(body("query A { a }")), json.loads(body("mutation B { b }"))]), False),
    ("POST", GRAPHQL, json.dumps({"operationName": "A", "extensions": {"persistedQuery": {"sha256Hash": "ab"}}}),
     False),  # a persisted query sent without its text can't be checked
    ("POST", GRAPHQL, "not json", False),
    ("POST", GRAPHQL, None, False),
    ("POST", "https://cdn.loom.com/graphql", body("query A { a }"), False),
    ("GET", GRAPHQL + "?query=" + "mutation%20M%20%7B%20x%20%7D", None, False),
    ("GET", GRAPHQL + "?query=" + "query%20A%20%7B%20x%20%7D", None, True),
    ("GET", GRAPHQL + "?extensions=%7B%7D", None, False),
    ("POST", "https://www.loom.com/api/campaigns/sessions/x/transcoded-url", "{}", False),
    ("POST", "https://www.loom.com/v1/metrics", "{}", False),
    ("PUT", f"https://www.loom.com/v1/videos/{VID}", "{}", False),
    ("DELETE", f"https://www.loom.com/v1/videos/{VID}", None, False),
    ("GET", "https://www.loom.com/logout", None, False),
    ("GET", "http://www.loom.com/looms/videos", None, False),
    ("GET", "https://www.loom.com:8443/looms/videos", None, False),
    ("GET", "https://loom.com.evil.example/x", None, False),
    ("GET", "https://accounts.google.com/o/oauth2", None, False),
    ("GET", "https://www.google-analytics.com/collect", None, False),
])
def test_the_policy(method, url, data, ok):
    assert request_allowed(method, url, data) is ok


def test_a_blocked_graphql_post_is_named_by_its_operation_never_its_body():
    request = SimpleNamespace(method="POST", url=GRAPHQL + "?token=abc0123456789secretvalue",
                              post_data=body("mutation RecordView { x }", "RecordView"))
    guard.state.begin(POLICY)
    assert guard.check(request, own_page=True) is False
    assert guard.state.blocked == ["POST www.loom.com/graphql op=RecordView"]


# The flows, on a scripted tab.

def library_page(*pages: tuple[list[dict], str | None]) -> dict:
    out = []
    for rows, cursor in pages:
        out.append({"status": 200, "body": {"data": {"getLooms": {"videos": {
            "edges": [{"node": {"id": r, "name": f"Video {r[:4]}", "visibility": "owner"}} for r in rows],
            "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor}}}}}})
    return out


def test_videos_follows_the_cursor_to_the_limit(monkeypatch):
    answers = library_page(([VID, "1" * 32], "c1"), (["2" * 32, "3" * 32], "c2"), (["4" * 32], None))
    seen = []

    def graphql(arg):
        seen.append(arg[2]["variables"]["cursor"])
        return answers.pop(0)

    page = install(monkeypatch, ScriptedPage(answers={"res.json()": graphql}))
    rows = asyncio.run(fetch.videos(3))
    assert [r["id"] for r in rows] == [VID, "1" * 32, "2" * 32] and seen == [None, "c1"]
    assert page.gotos == [LIBRARY] and page.policy is POLICY
    assert rows[0] == {"id": VID, "title": "Video 0123", "visibility": "owner"}


def test_signed_out_is_auth_required_and_status_says_so(monkeypatch):
    install(monkeypatch, ScriptedPage(redirects={LIBRARY: "https://www.loom.com/login?redirect=%2Flooms"}))
    with pytest.raises(AuthRequired):
        asyncio.run(fetch.videos())
    assert asyncio.run(fetch.status()) == {"signedIn": False, "reason": "Loom's session is missing or expired. (Loom "
                                                                        "sent Chrome to www.loom.com/login.)"}


UNAUTHENTICATED = {"status": 200, "body": {"data": {"getLooms": None}, "errors": [
    {"message": "Not authenticated", "extensions": {"code": "UNAUTHENTICATED"}}]}}


def test_status_is_what_list_needs_the_library_page_and_the_library_query(monkeypatch):
    queries = []

    def answer(arg):
        queries.append(arg[2]["operationName"])
        return library_page(([], None))[0]  # an empty library is still signed in

    page = install(monkeypatch, ScriptedPage(answers={"res.json()": answer}))
    assert asyncio.run(fetch.status()) == {"signedIn": True}
    assert page.gotos == [LIBRARY] and queries == ["GetLoomsForLibrary"] and page.policy is POLICY
    install(monkeypatch, ScriptedPage(answers={"res.json()": UNAUTHENTICATED}))  # the page loads, the API refuses
    out = asyncio.run(fetch.status())
    assert out["signedIn"] is False and "UNAUTHENTICATED" in out["reason"]
    with pytest.raises(AuthRequired):  # exit 4 for the reads too, not 5
        asyncio.run(fetch.videos())


@pytest.mark.parametrize("code", ["FORBIDDEN", "INTERNAL_SERVER_ERROR", None])
def test_other_graphql_errors_stay_site_errors(monkeypatch, code):
    error = {"message": "x", **({"extensions": {"code": code}} if code else {})}
    install(monkeypatch, ScriptedPage(answers={"res.json()": {"status": 200, "body": {"errors": [error]}}}))
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(fetch.videos())
    assert not isinstance(caught.value, AuthRequired)


def test_video_card_and_a_missing_video(monkeypatch):
    card = {"status": 200, "body": {"data": {"video": {"__typename": "RegularUserVideo", "id": VID, "name": "Demo",
                                                       "createdAt": "2026-10-01T12:00:00Z", "playable_duration": 65.4,
                                                       "views": {"total": 7}, "owner": {"first_name": "Kevin",
                                                                                        "last_name": "Kirchner"},
                                                       "complete": True, "archived": False}}}}
    install(monkeypatch, ScriptedPage(answers={"res.json()": card}))
    assert asyncio.run(fetch.video(VID)) == {"id": VID, "title": "Demo", "created_at": "2026-10-01T12:00:00Z",
                                             "duration_seconds": 65.4, "views": 7, "owner": "Kevin Kirchner",
                                             "complete": True, "archived": False,
                                             "url": f"https://www.loom.com/share/{VID}"}
    install(monkeypatch, ScriptedPage(answers={"res.json()": {
        "status": 200, "body": {"data": {"video": {"__typename": "GenericError"}}}}}))
    with pytest.raises(NotFound):
        asyncio.run(fetch.video(VID))
    install(monkeypatch, ScriptedPage(answers={"res.json()": {"status": 401, "body": None}}))
    with pytest.raises(AuthRequired):
        asyncio.run(fetch.video(VID))


def test_transcript_reads_the_phrase_file_the_share_page_fetches(monkeypatch):
    share = f"https://www.loom.com/share/{VID}"
    phrases = {"phrases": [{"ts": 0, "value": " Hello "}, {"ts": 65.2, "value": "Bye"}, {"ts": 70, "value": ""}]}
    page = install(monkeypatch, ScriptedPage(responses={share: [
        Response("https://cdn.loom.com/mediametadata/transcription/other-1.json", {"phrases": []}, "GET"),
        Response(TRANSCRIPT, phrases, "GET")]}))
    rows = asyncio.run(fetch.transcript(VID))
    assert rows == [{"ts": 0, "text": "Hello"}, {"ts": 65.2, "text": "Bye"}] and page.gotos == [share]
    assert parse.transcript_text(rows) == "[0m 0s] Hello\n[1m 5s] Bye"


def test_no_transcript_in_time_is_not_found(monkeypatch):
    monkeypatch.setattr(fetch, "TRANSCRIPT_SECONDS", 0.01)
    install(monkeypatch, ScriptedPage())
    with pytest.raises(NotFound, match="No official transcript"):
        asyncio.run(fetch.transcript(VID))


@pytest.mark.parametrize(("url", "signed_in"), [
    ("https://www.loom.com/looms/videos", True), (f"https://www.loom.com/share/{VID}", False),  # public: signed out too
    ("https://www.loom.com/login", False), ("https://www.loom.com/signup?x=1", False),
    ("https://accounts.google.com/looms", False), ("https://www.loom.com/", False),
])
def test_login_is_done_once_a_tab_shows_the_library(url, signed_in):
    assert parse.signed_in_url(url) is signed_in


class Window:
    """login's visible Chrome, one tab. Each poll reads `pages` once; `script` gives, per poll, the URL the person is on
    and what the library query answers there, the last entry repeating."""

    def __init__(self, script: list) -> None:
        self.script, self.polls, self.queries = script, 0, 0
        self.tab = ScriptedPage(answers={"res.json()": self.answer})
        self.tab.context, self.tab.is_closed = self, lambda: False
        self.now = None

    def answer(self, _arg):
        self.queries += 1
        return self.now

    @property
    def pages(self) -> list:
        self.tab.url, self.now = self.script[min(self.polls, len(self.script) - 1)]
        self.polls += 1
        return [self.tab]


@pytest.fixture
def window(monkeypatch):
    """window(script, after) -> the Window login opens; `after` is the fresh hidden Chrome status() reads afterwards
    (its ScriptedPage, as installed)."""
    async def no_sleep(_seconds):
        pass

    monkeypatch.setattr(fetch.asyncio, "sleep", no_sleep)

    def use(script: list, after: ScriptedPage) -> Window:
        win = Window(script)

        @asynccontextmanager
        async def open_page(policy, **options):
            win.opened = (policy, options)
            yield win.tab

        monkeypatch.setattr(browser, "open_page", open_page)
        win.after = install(monkeypatch, after)
        return win

    return use


SIGNED_IN = library_page(([VID], None))[0]


def test_login_waits_for_the_api_to_answer_signed_in_then_checks_a_fresh_hidden_chrome(window):
    win = window([("https://www.loom.com/login", None), ("https://id.atlassian.com/login?x=1", None),
                  (LIBRARY, UNAUTHENTICATED), (LIBRARY, {"status": 401, "body": None}), (LIBRARY, SIGNED_IN)],
                 ScriptedPage(answers={"res.json()": SIGNED_IN}))
    states = []
    assert asyncio.run(fetch.login(60, states.append)) == {"signedIn": True}
    assert win.opened == (None, {"mode": "visible"}) and states == ["signing in"]
    assert win.polls == 5 and win.queries == 3  # the URL alone was never enough
    assert win.tab.gotos == ["https://www.loom.com/login"]  # the window is never navigated
    assert win.after.gotos == [LIBRARY] and win.after.policy is POLICY  # then a hidden, guarded Chrome


def test_a_public_share_page_is_not_sign_in(window):
    win = window([(f"https://www.loom.com/share/{VID}", SIGNED_IN)], ScriptedPage(answers={"res.json()": SIGNED_IN}))
    with pytest.raises(AuthRequired, match="Timed out after 0.05s waiting for Loom sign-in"):
        asyncio.run(fetch.login(0.05))
    assert win.queries == 0 and win.after.gotos == []


def test_login_fails_when_a_fresh_hidden_chrome_is_signed_out(window):
    window([(LIBRARY, SIGNED_IN)],
           ScriptedPage(redirects={LIBRARY: "https://www.loom.com/login?redirect=%2Flooms"}))
    with pytest.raises(AuthRequired, match="fresh hidden Chrome on the same profile is signed out: .*www.loom.com/login"):
        asyncio.run(fetch.login(60))


def test_login_stops_when_the_window_closes(window, monkeypatch):
    win = window([(LIBRARY, SIGNED_IN)], ScriptedPage())
    monkeypatch.setattr(Window, "pages", property(lambda _self: []))
    with pytest.raises(AuthRequired, match="closed before sign-in finished"):
        asyncio.run(fetch.login(60))
    assert win.after.gotos == []


# The commands, over a faked daemon.

@pytest.fixture
def calls(monkeypatch):
    make_profile("loom:default")
    seen, answers = [], {}

    async def call(ident, site, op, *args, **kwargs):
        seen.append((op, args, kwargs))
        return answers[op]

    monkeypatch.setattr(daemon, "call", call)
    return seen, answers


def run(*args):
    return CliRunner().invoke(webcli_app.build(), ["loom", *args])


def test_list_info_and_ids(calls):
    seen, answers = calls
    answers["videos"] = [{"id": VID, "title": "Demo", "visibility": "owner"}]
    assert run("list", "--id-only").stdout == f"{VID}\n"
    assert json.loads(run("list", "--limit", "5").stdout) == answers["videos"]
    answers["video"] = {"id": VID, "title": "Demo", "created_at": None, "duration_seconds": 65, "views": 1,
                        "owner": "K", "complete": True, "archived": False, "url": "u"}
    assert json.loads(run("info", f"https://www.loom.com/share/{VID}?t=3", "--compact").stdout) == {
        "id": VID, "title": "Demo", "created_at": None, "duration_seconds": 65, "views": 1}
    assert seen == [("videos", (None,), {"headed": False}), ("videos", (5,), {"headed": False}),
                    ("video", (VID,), {"headed": False})]


def test_transcript_is_text_even_when_piped_json_on_request_and_writes_a_file(calls, tmp_path):
    _, answers = calls
    answers["transcript"] = [{"ts": 0, "text": "Hello"}, {"ts": 65.2, "text": "Bye"}]
    assert run("transcript", VID).stdout == "[0m 0s] Hello\n[1m 5s] Bye\n"
    assert json.loads(run("transcript", VID, "--format", "json").stdout) == answers["transcript"]
    out = tmp_path / "t.txt"
    assert run("transcript", VID, "-o", str(out)).exit_code == 0
    assert out.read_text() == "[0m 0s] Hello\n[1m 5s] Bye\n"
    answers["transcript"] = []
    assert run("transcript", VID).exit_code == 3
