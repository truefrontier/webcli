"""webcli youtube, offline: the fail-closed guard (ytcli's table), the passive YouTube sign-in, the history sources and
their fallback read off a scripted page, and the commands over a faked daemon. The parsers are in
test_youtube_parse.py and test_youtube_myactivity.py."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from webcli.apps import webcli as webcli_app
from webcli.core import daemon
from webcli.core.errors import AuthRequired, UsageError
from webcli.sites.youtube import fetch
from webcli.sites.youtube.endpoints import MYACTIVITY_URL, SIGN_IN_URL
from webcli.sites.youtube.guard import WriteScope, request_allowed, write_policy

from google_fakes import ScriptedPage, install

FIXTURES = Path(__file__).parent / "fixtures" / "youtube"
GATE = (FIXTURES / "myactivity_gate.txt").read_text()
DOM = (FIXTURES / "myactivity_dom.html").read_text()


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# The guard: ytcli's table, plus Google's cookie rotation (gemini shares google:default and allows it).

@pytest.mark.parametrize(("method", "url", "allowed"), [
    ("GET", "https://www.youtube.com/", True),
    ("GET", "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg", True),
    ("GET", "https://yt3.ggpht.com/avatar", True),
    ("GET", "https://myactivity.google.com/product/youtube", True),
    ("GET", SIGN_IN_URL, True),
    ("POST", "https://www.youtube.com/youtubei/v1/browse?key=k&prettyPrint=false", True),
    ("POST", "https://www.youtube.com/youtubei/v1/account/account_menu?key=k", True),
    ("POST", "https://www.youtube.com/youtubei/v1/account/accounts_list?key=k", True),
    ("POST", "https://myactivity.google.com/_/FootprintsMyactivityUi/data/batchexecute?rpcids=x", True),
    ("POST", "https://accounts.google.com/RotateCookies", True),
    ("POST", "https://www.youtube.com/youtubei/v1/browse/edit_playlist", False),
    ("POST", "https://www.youtube.com/youtubei/v1/playlist/create?key=k", False),
    ("POST", "https://www.youtube.com/youtubei/v1/browseX", False),
    ("POST", "https://www.youtube.com/youtubei/v1/like/like", False),
    ("POST", "https://www.youtube.com/youtubei/v1/subscription/subscribe", False),
    ("POST", "https://www.youtube.com/youtubei/v1/history/pause_watch_history", False),
    ("POST", "https://www.youtube.com/youtubei/v1/feedback", False),
    ("POST", "https://www.youtube.com/youtubei/v1/log_event", False),
    ("POST", "https://www.youtube.com/youtubei/v1/player", False),
    ("POST", "https://youtube.com.evil.example/youtubei/v1/browse", False),
    ("POST", "http://www.youtube.com/youtubei/v1/browse", False),
    ("POST", "https://www.youtube.com:8443/youtubei/v1/browse", False),
    ("POST", "https://myactivity.google.com/_/X/data/batchexecute/extra", False),
    ("POST", "https://myactivity.google.com/_//data/batchexecute", False),
    ("PUT", "https://www.youtube.com/youtubei/v1/browse", False),
    ("DELETE", "https://www.youtube.com/", False),
    ("OPTIONS", "https://www.youtube.com/youtubei/v1/browse", False),
    ("GET", "https://www.youtube.com/logout", False),
    ("GET", "https://googleads.g.doubleclick.net/pagead/id", False),
    ("GET", "https://rr1---sn-4g5edndk.googlevideo.com/videoplayback", False),
])
def test_request_allowed(method, url, allowed):
    assert request_allowed(method, url) is allowed


# The one write: playlist/create, bound to its scope.

CREATE = "https://www.youtube.com/youtubei/v1/playlist/create?key=k&prettyPrint=false"
IDS = ("_0d4Hm5Hs6g", "ABQLEtCyb1g")


def create_json(**override) -> str:
    return json.dumps({"context": {"client": {}}, "title": "Pieces", "privacyStatus": "UNLISTED",
                       "videoIds": list(IDS), **override})


def armed() -> WriteScope:
    return WriteScope("Pieces", "UNLISTED", IDS, armed=True)


def test_the_scope_passes_exactly_one_matching_create():
    scope = armed()
    assert request_allowed("POST", CREATE, create_json(), scope=scope)
    assert scope.used and not scope.armed
    scope.armed = True
    assert not request_allowed("POST", CREATE, create_json(), scope=scope)  # used up: no second


@pytest.mark.parametrize(("url", "body"), [
    (CREATE, create_json(title="Other")),
    (CREATE, create_json(privacyStatus="PUBLIC")),
    (CREATE, create_json(videoIds=list(reversed(IDS)))),
    (CREATE, create_json(videoIds=[*IDS, "jNQXAC9IVRw"])),
    (CREATE, create_json(description="extra")),
    (CREATE, '{"context": {}, "title": "Other", "title": "Pieces", "privacyStatus": "UNLISTED", '
             '"videoIds": ["_0d4Hm5Hs6g", "ABQLEtCyb1g"]}'),
    (CREATE, '{"context": {"n": NaN}, "title": "Pieces", "privacyStatus": "UNLISTED", '
             '"videoIds": ["_0d4Hm5Hs6g", "ABQLEtCyb1g"]}'),
    (CREATE, "not json"),
    (CREATE, None),
    ("https://www.youtube.com/youtubei/v1/playlist/delete?key=k", create_json()),
    ("https://www.youtube.com/youtubei/v1/browse/edit_playlist?key=k", create_json()),
    ("https://www.youtube.com:443/youtubei/v1/playlist/create", create_json()),
    ("http://www.youtube.com/youtubei/v1/playlist/create", create_json()),
    ("https://youtube.com.evil.example/youtubei/v1/playlist/create", create_json()),
])
def test_the_scope_refuses_any_other_write(url, body):
    scope = armed()
    assert not request_allowed("POST", url, body, scope=scope) and not scope.used


def test_the_scope_counts_only_while_armed_and_only_with_send():
    idle = WriteScope("Pieces", "UNLISTED", IDS)
    assert not request_allowed("POST", CREATE, create_json(), scope=idle)
    request = SimpleNamespace(method="POST", url=CREATE, post_data=create_json())
    scope = armed()
    assert not write_policy(scope).allowed(request, SimpleNamespace(send=False)) and not scope.used
    assert write_policy(scope).allowed(request, SimpleNamespace(send=True)) and scope.used


def playlist_page(*, playlist_id="PLnew", shown=IDS, create_status=200) -> ScriptedPage:
    """A signed-in page whose playlist/create goes through the real policy, as the page route would."""
    page = youtube_page(signed_in_already=True)
    reads = page.answers["SAPISIDHASH"]

    def yt_call(arg):
        endpoint, body = arg
        if endpoint == "playlist/create":
            request = SimpleNamespace(method="POST", url=CREATE, post_data=json.dumps({"context": {}, **body}))
            if not page.policy.allowed(request, SimpleNamespace(send=True)):
                raise RuntimeError("net::ERR_FAILED")  # the guard aborted it
            return {"status": create_status, "json": {"playlistId": playlist_id} if create_status == 200 else None}
        if endpoint == "browse" and body.get("browseId", "").startswith("VL"):
            return {"status": 200, "json": {"contents": {"playlistVideoListRenderer": {
                "contents": [{"playlistVideoRenderer": {"videoId": v}} for v in shown]}}}}
        return reads(arg)

    page.answers["SAPISIDHASH"] = yt_call
    return page


def test_create_playlist_creates_once_and_reads_it_back(monkeypatch):
    install(monkeypatch, playlist_page())
    out = asyncio.run(fetch.create_playlist("Pieces", list(IDS), "unlisted", confirm=True))
    assert out == {"created": True, "confirmed": True, "playlistId": "PLnew",
                   "url": "https://www.youtube.com/playlist?list=PLnew", "title": "Pieces", "privacy": "unlisted",
                   "videoIds": list(IDS), "shownCount": 2, "account": "@testviewer"}


def test_create_playlist_unverified_and_refused(monkeypatch):
    install(monkeypatch, playlist_page(shown=IDS[:1]))
    out = asyncio.run(fetch.create_playlist("Pieces", list(IDS), "unlisted", confirm=True))
    assert (out["created"], out["confirmed"], out["shownCount"]) == (True, False, 1)
    install(monkeypatch, playlist_page(create_status=500))
    out = asyncio.run(fetch.create_playlist("Pieces", list(IDS), "unlisted", confirm=True))
    assert (out["created"], out["confirmed"], out["playlistId"]) == (True, False, None)  # went out: never "nothing"
    broken = install(monkeypatch, playlist_page())
    create = broken.answers["SAPISIDHASH"]

    def read_back_fails(arg):
        if arg[0] == "browse" and arg[1].get("browseId", "").startswith("VL"):
            raise RuntimeError("Target page, context or browser has been closed")
        return create(arg)

    broken.answers["SAPISIDHASH"] = read_back_fails
    out = asyncio.run(fetch.create_playlist("Pieces", list(IDS), "unlisted", confirm=True))
    assert (out["created"], out["confirmed"], out["playlistId"], out["shownCount"]) == (True, False, "PLnew", None)
    with pytest.raises(UsageError):
        asyncio.run(fetch.create_playlist("Pieces", list(IDS), "unlisted"))
    with pytest.raises(UsageError):
        asyncio.run(fetch.create_playlist("Pieces", [], "unlisted", confirm=True))


# Sign-in and status.

def youtube_page(*, logged_in_after_sign_in: bool = True, signed_in_already: bool = False, **extra) -> ScriptedPage:
    page = ScriptedPage(redirects={SIGN_IN_URL: "https://www.youtube.com/"}, **extra)

    def logged_in(_arg) -> bool:
        return signed_in_already or (logged_in_after_sign_in and SIGN_IN_URL in page.gotos)

    def yt_call(arg):
        endpoint, body = arg
        if endpoint == "browse":
            name = "fehistory_continuation.json" if body.get("continuation") else "fehistory_initial.json"
            return {"status": 200, "json": load(name)}
        return {"status": 200, "json": load("account_menu.json")}

    page.answers.update({"LOGGED_IN": logged_in, "SAPISIDHASH": yt_call})
    return page


def test_status_follows_youtubes_passive_sign_in_once(monkeypatch):
    page = install(monkeypatch, youtube_page())
    assert asyncio.run(fetch.status()) == {"signedIn": True, "accountName": "Test Viewer",
                                           "email": "viewer@example.com", "handle": "@testviewer"}
    assert page.gotos == ["https://www.youtube.com/", SIGN_IN_URL]


def test_already_signed_in_skips_the_sign_in_hop_and_signed_out_stays_out(monkeypatch):
    page = install(monkeypatch, youtube_page(signed_in_already=True))
    asyncio.run(fetch.status())
    assert page.gotos == ["https://www.youtube.com/"]
    install(monkeypatch, youtube_page(logged_in_after_sign_in=False))
    assert asyncio.run(fetch.status()) == fetch.SIGNED_OUT
    stuck = install(monkeypatch, youtube_page())
    stuck.redirects[SIGN_IN_URL] = "https://accounts.google.com/v3/signin/accountchooser"  # needs a person
    assert asyncio.run(fetch.status()) == fetch.SIGNED_OUT


# History.

def test_youtube_source_pages_through_continuations(monkeypatch):
    install(monkeypatch, youtube_page(signed_in_already=True))
    result = asyncio.run(fetch.history("youtube", None, None))
    assert [r["videoId"] for r in result["rows"]] == [
        "dQw4w9WgXcQ", "jNQXAC9IVRw", "aaaaaaaaaaa", "bbbbbbbbbbb", "ddddddddddd", "eeeeeeeeeee", "fffffffffff",
        "ggggggggggg", "hhhhhhhhhhh"]
    assert {r["source"] for r in result["rows"]} == {"youtube-history"} and result["notes"] == []
    assert len(asyncio.run(fetch.history("youtube", 3, None))["rows"]) == 3


def test_auto_falls_back_to_youtube_behind_the_verification_gate(monkeypatch):
    install(monkeypatch, youtube_page(signed_in_already=True, html="<html></html>", text=GATE))
    result = asyncio.run(fetch.history("auto", 2, None))
    assert [r["source"] for r in result["rows"]] == ["youtube-history"] * 2
    assert "verify it's you" in result["notes"][0] and "Using YouTube history" in result["notes"][0]
    install(monkeypatch, youtube_page(signed_in_already=True, html="<html></html>", text=GATE))
    with pytest.raises(AuthRequired, match="Manage My Activity verification"):
        asyncio.run(fetch.history("myactivity", 2, None))


def test_auto_prefers_my_activity_and_falls_back_when_it_is_signed_out(monkeypatch):
    install(monkeypatch, youtube_page(signed_in_already=True, html=DOM, text="Today"))
    result = asyncio.run(fetch.history("auto", 2, None))
    assert [(r["videoId"], r["source"], r["watchedAtPrecision"]) for r in result["rows"]] == [
        ("dQw4w9WgXcQ", "myactivity", "exact"), ("aaaaaaaaaaa", "myactivity", "exact")]
    page = install(monkeypatch, youtube_page(signed_in_already=True))
    page.redirects[MYACTIVITY_URL] = "https://accounts.google.com/ServiceLogin"
    result = asyncio.run(fetch.history("auto", 1, None))
    assert result["rows"][0]["source"] == "youtube-history"
    assert result["notes"] == ["My Activity unavailable (Not signed in to My Activity.); using YouTube history (day "
                               "precision)."]


def test_since_stops_at_the_cutoff(monkeypatch):
    install(monkeypatch, youtube_page(signed_in_already=True))
    rows = asyncio.run(fetch.history("youtube", None, "2000-01-01T00:00:00+00:00"))["rows"]
    assert len(rows) == 9  # nothing that old: every page
    assert asyncio.run(fetch.history("youtube", None, "2999-01-01T00:00:00+00:00"))["rows"] == []


def test_signed_out_youtube_history_is_auth_required(monkeypatch):
    install(monkeypatch, youtube_page(logged_in_after_sign_in=False))
    with pytest.raises(AuthRequired, match="Not signed in to YouTube"):
        asyncio.run(fetch.history("youtube", 5, None))


# The commands, over a faked daemon.

ROW = {"title": "Me at the zoo", "channel": "jawed", "channelUrl": "https://www.youtube.com/@jawed",
       "videoId": "jNQXAC9IVRw", "url": "https://www.youtube.com/watch?v=jNQXAC9IVRw",
       "watchedAt": "2026-10-14T22:46:40-05:00", "watchedAtPrecision": "exact", "durationSec": None,
       "isShort": False, "source": "myactivity"}


class Calls(list):
    answer: dict


@pytest.fixture
def calls(monkeypatch):
    seen = Calls()
    answer = {"rows": [ROW], "notes": ["My Activity unavailable (x); using YouTube history (day precision)."]}

    async def call(ident, site, op, *args, **kwargs):
        seen.append((op, args))
        return answer

    monkeypatch.setattr(daemon, "call", call)
    seen.answer = answer
    return seen


def run(*args):
    return CliRunner().invoke(webcli_app.build(), ["youtube", *args])


def test_history_passes_limit_since_and_source_and_prints_why_it_fell_back(calls):
    result = run("history")
    assert result.exit_code == 0 and json.loads(result.stdout) == [ROW]
    assert calls[-1] == ("history", ("auto", 50, None)) and "using YouTube history" in result.stderr
    run("history", "--since", "2026-10-01T00:00:00Z", "--source", "youtube")
    op, (source, limit, since) = calls[-1]
    assert (op, source, limit) == ("history", "youtube", None)  # --since: no cap
    assert datetime.fromisoformat(since) == datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert "using YouTube history" not in run("history", "-q").stderr
    assert json.loads(run("history", "--compact").stdout) == [
        {"watchedAt": ROW["watchedAt"], "title": ROW["title"], "channel": "jawed", "videoId": "jNQXAC9IVRw"}]


def test_no_history_exits_3(calls):
    calls.answer["rows"] = []
    result = run("history", "--since", "2026-10-01")
    error = json.loads(result.stderr.splitlines()[-1])  # after the fallback note
    assert result.exit_code == 3 and error["code"] == 3 and error["error"].startswith("No watch history found since")


def test_playlist_create_is_a_dry_run_without_confirm(calls):
    result = run("playlist", "create", "--title", "Pieces", "_0d4Hm5Hs6g", "https://youtu.be/ABQLEtCyb1g?si=x")
    assert result.exit_code == 0 and not calls
    assert json.loads(result.stdout) == {"dryRun": True, "action": "create playlist", "title": "Pieces",
                                         "privacy": "private", "videoIds": list(IDS), "videoCount": 2,
                                         "hint": "Nothing was written. Re-run with --confirm to write."}
    assert run("playlist", "create", "--title", "Pieces", "nope").exit_code == 2
    assert run("playlist", "create", "--title", "Pieces", *IDS, IDS[0]).exit_code == 2
    assert run("playlist", "create", "--title", " ", *IDS).exit_code == 2
    assert run("playlist", "create", "--title", "Pieces", "--dry-run", "--confirm", *IDS).exit_code == 2
    assert not calls


def test_playlist_create_confirm_exits_0_or_8(calls):
    calls.answer.clear()
    calls.answer.update({"created": True, "confirmed": True, "playlistId": "PLnew", "url": "u", "title": "Pieces",
                         "privacy": "unlisted", "videoIds": list(IDS), "shownCount": 2, "account": "@testviewer"})
    result = run("playlist", "create", "--title", "Pieces", "--privacy", "unlisted", "--confirm", *IDS)
    assert result.exit_code == 0 and json.loads(result.stdout)["playlistId"] == "PLnew"
    assert calls[-1] == ("create_playlist", ("Pieces", list(IDS), "unlisted"))
    calls.answer["confirmed"] = False
    result = run("playlist", "create", "--title", "Pieces", "--yes", *IDS)
    assert result.exit_code == 8 and json.loads(result.stderr.splitlines()[-1])["error"] == "sent_unverified"
