"""webcli youtube, offline: the fail-closed guard (ytcli's table), the passive YouTube sign-in, the history sources and
their fallback read off a scripted page, and the commands over a faked daemon. The parsers are in
test_youtube_parse.py and test_youtube_myactivity.py."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from webcli.apps import webcli as webcli_app
from webcli.core import daemon
from webcli.core.errors import AuthRequired
from webcli.sites.youtube import fetch
from webcli.sites.youtube.endpoints import MYACTIVITY_URL, SIGN_IN_URL
from webcli.sites.youtube.guard import request_allowed

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
