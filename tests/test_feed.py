"""webcli feed, offline: normalizing site rows into newsfeed.items/1, per-site auth / needs_login / errors in the
sources block, the atomic write, the x import, the schedule plist (never loaded here), and push's URL checks. Child
`webcli <site>` processes are faked; nothing reaches a site; launchctl never runs."""

from __future__ import annotations

import json
import plistlib
import stat
import subprocess

import pytest
from click.testing import CliRunner

from webcli.apps import webcli as webcli_app
from webcli.feed import normalize, pull, push, schedule


def done(rc: int, rows=None, err: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], rc, json.dumps(rows) if rows is not None else "", err)


YT = {"source": "youtube", "id": "v1", "url": "https://www.youtube.com/watch?v=v1", "title": "A video",
      "author": "Example Channel", "channelUrl": "https://www.youtube.com/@example",
      "publishedAt": "2026-01-02T03:04:05Z", "snippet": None, "thumbnail": "https://i.ytimg.com/vi/v1/hq.jpg",
      "metrics": {"views": 5}, "auth": "signed_in"}
PPLX = {"source": "perplexity", "id": "p1", "url": "https://news.example.com/a", "title": "A story",
        "author": "curator", "publishedAt": "2026-01-02T03:04:05+00:00", "snippet": "Summary.", "thumbnail": None,
        "metrics": {}, "perplexityUrl": "https://www.perplexity.ai/discover/top/a-story", "topic": "top",
        "auth": "public"}
LI = {"url": "https://www.linkedin.com/feed/update/urn:li:activity:1234567/", "urn": "urn:li:activity:1234567",
      "author_name": "Example Person", "author_handle": "example-person", "author_headline": "Builder",
      "posted_at": "2026-01-02T09:04:05-06:00", "text": "First line\nSecond line", "reactions": 3, "comments": 1,
      "reposts": 0, "kind": "post", "original_url": None}


def test_normalize_each_site():
    y = normalize.item("youtube", "home", YT)
    assert (y["source"], y["native_id"], y["published_at"], y["author"]["profile_url"]) == (
        "youtube", "v1", "2026-01-02T03:04:05Z", "https://www.youtube.com/@example")
    assert y["meta"]["via"] == "webcli youtube home" and y["meta"]["auth"] == "signed_in"
    assert y["media"] == [{"type": "image", "url": "https://i.ytimg.com/vi/v1/hq.jpg"}]
    p = normalize.item("perplexity", "discover", PPLX)
    assert p["published_at"] == "2026-01-02T03:04:05Z" and p["meta"]["auth"] == "public"
    assert p["links"] == ["https://www.perplexity.ai/discover/top/a-story"] and p["meta"]["topic"] == "top"
    li = normalize.item("linkedin", "feed", LI)
    assert (li["title"], li["published_at"], li["author"]["handle"]) == (
        "First line", "2026-01-02T15:04:05Z", "example-person")
    assert set(li) == {"source", "native_id", "url", "title", "text", "author", "published_at", "metrics", "links",
                       "media", "meta"}
    assert normalize.item("youtube", "home", {"title": "no id"}) is None


def test_merge_keeps_first_and_lists_reads():
    a, b = normalize.item("youtube", "uploads", YT), normalize.item("youtube", "home", YT)
    merged = normalize.merge([a, b])
    assert len(merged) == 1 and merged[0]["meta"]["reads"] == ["uploads", "home"]


def runner(answers):
    calls = []

    def run(argv):
        calls.append(argv)
        return answers[tuple(argv[:2])]

    return run, calls


def test_pull_records_auth_needs_login_and_errors():
    run, calls = runner({
        ("youtube", "uploads"): done(0, [YT]), ("youtube", "home"): done(0, [YT]),
        ("perplexity", "discover"): done(0, [PPLX]),
        ("linkedin", "feed"): done(4, err='{"error": "Not signed in", "code": 4}'),
        ("gnews", "foryou"): done(5, err='{"error": "Google News answered with a challenge", "code": 5}'),
    })
    doc = pull.pull(["youtube", "perplexity", "linkedin", "gnews"], 7, since="24h", run=run)
    assert doc["schema"] == "newsfeed.items/1" and doc["generated_at"].endswith("Z")
    by = {e["site"]: e for e in doc["sources"]}
    assert (by["youtube"]["status"], by["youtube"]["auth"], by["youtube"]["count"]) == ("ok", "signed_in", 1)
    assert (by["perplexity"]["auth"], by["perplexity"]["needs_login"], by["perplexity"]["hint"]) == (
        "public", True, "webcli perplexity login")
    assert by["linkedin"] == {**by["linkedin"], "status": "needs_login", "needs_login": True,
                              "hint": "webcli identity login linkedin:default"}
    assert by["gnews"]["status"] == "error" and "challenge" in by["gnews"]["error"]
    assert [i["source"] for i in doc["items"]] == ["youtube", "perplexity"]
    assert ["youtube", "uploads", "--since", "24h", "--limit", "7", "--format", "json"] in calls
    assert pull.partial(doc)


def test_public_fallback_alone_is_not_partial():
    run, _ = runner({("perplexity", "discover"): done(0, [PPLX])})
    assert not pull.partial(pull.pull(["perplexity"], 5, run=run))


def test_limits_are_capped_per_site():
    run, calls = runner({("linkedin", "feed"): done(3)})
    entry, items = pull.pull_site("linkedin", 400, "48h", run)
    assert calls[0][3] == "50" and entry["status"] == "ok" and items == []


def test_x_import(tmp_path):
    entry, items = pull.pull_x(None)
    assert entry["status"] == "skipped" and items == []
    assert pull.pull_x(str(tmp_path / "missing.json"))[0]["status"] == "skipped"
    f = tmp_path / "x.json"
    f.write_text(json.dumps({"schema": "newsfeed.items/1", "items": [
        {"source": "x", "native_id": "1", "url": "https://x.com/a/status/1", "published_at": "2026-01-02T03:04:05Z"},
        {"source": "x", "native_id": "2"}]}))
    entry, items = pull.pull_x(str(f))
    assert entry["status"] == "ok" and [i["native_id"] for i in items] == ["1"]
    f.write_text("{}")
    assert pull.pull_x(str(f))[0]["status"] == "error"


def test_atomic_private_write(tmp_path):
    path = pull.write_atomic(tmp_path / "feeds" / "2026-01-02-0530.json", {"schema": "newsfeed.items/1"})
    assert json.loads(path.read_text())["schema"] == "newsfeed.items/1"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert [p.name for p in path.parent.iterdir()] == [path.name]  # no temp file left
    assert pull.latest(path.parent) == path


def test_cli_pull_writes_and_exits_5_when_partial(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBCLI_HOME", str(tmp_path))
    run, _ = runner({("perplexity", "discover"): done(0, [PPLX]), ("linkedin", "feed"): done(4)})
    orig = pull.pull  # the CLI calls pull.pull; hand it the fake child runner
    monkeypatch.setattr(pull, "pull", lambda *a, **k: orig(*a, **{**k, "run": run}))
    out = CliRunner().invoke(webcli_app.build(), ["feed", "pull", "--sites", "perplexity,linkedin", "--out",
                                                  str(tmp_path / "f.json"), "--json"])
    assert out.exit_code == 5
    summary = json.loads(out.stdout)
    assert summary["items"] == 1 and summary["partial"] is True
    assert json.loads((tmp_path / "f.json").read_text())["items"][0]["meta"]["auth"] == "public"
    assert "webcli perplexity login" in out.stderr
    assert CliRunner().invoke(webcli_app.build(), ["feed", "pull", "--sites", "bogus"]).exit_code == 2


def test_schedule_plist_and_times(tmp_path, monkeypatch):
    assert schedule.parse_times(None) == [(5, 30), (12, 30)]
    assert schedule.parse_times("12:30, 5:30,12:30") == [(5, 30), (12, 30)]
    with pytest.raises(ValueError):
        schedule.parse_times("25:00")
    data = schedule.plist([(5, 30), (12, 30)], ["/usr/local/bin/webcli", "feed", "pull", "--quiet"])
    assert data["Label"] == "ai.truefrontier.webcli.feed"
    assert data["StartCalendarInterval"] == [{"Hour": 5, "Minute": 30}, {"Hour": 12, "Minute": 30}]
    assert data["RunAtLoad"] is False and "KeepAlive" not in data
    monkeypatch.setattr(schedule, "plist_path", lambda: tmp_path / "LaunchAgents" / "x.plist")
    monkeypatch.setenv("WEBCLI_HOME", str(tmp_path / "home"))
    ran = []
    monkeypatch.setattr(schedule.subprocess, "run",
                        lambda argv, **_: ran.append(argv) or subprocess.CompletedProcess(argv, 1, "", ""))
    info = schedule.install([(5, 30)], load=False)
    assert info["installed"] and info["times"] == ["05:30"] and info["loaded"] is False
    assert all(a[:2] == ["launchctl", "print"] for a in ran)  # --no-load: never bootstraps
    assert plistlib.loads((tmp_path / "LaunchAgents" / "x.plist").read_bytes())["Label"] == schedule.LABEL
    assert schedule.remove()["installed"] is False


def test_push_checks(tmp_path, monkeypatch):
    for bad in ("http://example.com/api/feed", "https://user:pw@example.com/api/feed", "ftp://example.com/x"):
        with pytest.raises(ValueError):
            push.check_url(bad)
    assert push.check_url("http://127.0.0.1:8787/api/feed")
    f = tmp_path / "f.json"
    f.write_text("{}")
    monkeypatch.delenv("FEED_TOKEN_TEST", raising=False)
    with pytest.raises(ValueError, match="FEED_TOKEN_TEST"):
        push.push(f, "https://example.com/api/feed", "FEED_TOKEN_TEST")
    sent = {}

    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(req, timeout):
        sent.update(method=req.get_method(), auth=req.get_header("Authorization"), body=req.data)
        return Resp()

    monkeypatch.setattr(push.urllib.request, "urlopen", urlopen)
    monkeypatch.setenv("FEED_TOKEN_TEST", "synthetic-value")
    info = push.push(f, "https://example.com/api/feed", "FEED_TOKEN_TEST")
    assert info["pushed"] and sent["method"] == "PUT" and sent["auth"] == "Bearer synthetic-value"
    assert "synthetic-value" not in json.dumps(info)
    big = tmp_path / "big.json"
    big.write_bytes(b"x" * (push.MAX_BYTES + 1))
    with pytest.raises(ValueError):
        push.push(big, "https://example.com/api/feed")


def test_linkedin_image_becomes_media():
    """0.17.2: a LinkedIn row's image (LinkedIn's media CDN only) is the item's media; no image, no media."""
    img = "https://media.licdn.com/dms/image/v2/D4E22AQFake/feedshare-shrink_1280/0/1?e=1&v=beta&t=x"
    li = normalize.item("linkedin", "feed", {**LI, "image": img})
    assert li["media"] == [{"type": "image", "url": img}]
    assert normalize.item("linkedin", "feed", LI)["media"] == []
