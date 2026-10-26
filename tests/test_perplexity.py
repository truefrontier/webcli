"""webcli perplexity, offline: the guard (GETs of Discover's paths only), the card parser, topic resolution, the
paged feed read and its challenge handling on a fake page, and the commands over a faked daemon. Synthetic data;
nothing reaches Perplexity; no Chrome starts."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from click.testing import CliRunner

from webcli.apps import webcli as webcli_app
from webcli.core import browser, daemon
from webcli.core.errors import NotFound, UsageError
from webcli.sites import registry
from webcli.sites.perplexity import fetch, parse
from webcli.sites.perplexity.guard import request_allowed

TECH = "00000000-0000-4000-8000-00000000aaaa"
TOPICS = {"all_topics": [{"id": TECH, "slug": "tech", "name": "Tech & Science"},
                         {"id": "00000000-0000-4000-8000-00000000bbbb", "slug": "finance", "name": "Business"}]}


def card(n: int, **extra) -> dict:
    return {"uuid": f"id-{n}", "slug": f"example-story-{n}-AbCdEf", "title": f"Example story {n}",
            "summary": f"Summary {n}.", "published_timestamp": "2026-01-02T03:04:05.123456",
            "author_username": "curator", "web_results_preview": {"total_count": 3, "first_urls": []},
            "bullet_summary_web_results_preload": [{"url": f"https://news.example.com/a/{n}", "name": "A"}],
            "featured_images": [{"url": "https://img.example.com/src", "thumbnail": f"https://img.example.com/t/{n}"}],
            "social_info": {"view_count": 7, "like_count": 2, "fork_count": 1}, **extra}


# The guard.

@pytest.mark.parametrize(("method", "url", "ok"), [
    ("GET", "https://www.perplexity.ai/discover", True),
    ("GET", "https://www.perplexity.ai/rest/discover/feed?limit=20&offset=0&topic=top", True),
    ("HEAD", "https://www.perplexity.ai/rest/discover/topics?version=2.18", True),
    ("GET", "https://www.perplexity.ai/api/auth/session", True),
    ("POST", "https://www.perplexity.ai/rest/discover/feed", False),
    ("POST", "https://www.perplexity.ai/rest/sse/perplexity_ask", False),  # a query
    ("GET", "https://www.perplexity.ai/rest/pins", False),
    ("GET", "https://www.perplexity.ai/rest/user/info", False),
    ("GET", "https://www.perplexity.ai/discover/top/example-story-1", False),  # not needed, not allowed
    ("GET", "https://www.perplexity.ai/_next/static/chunks/app.js", False),  # scripts never run
    ("POST", "https://www.perplexity.ai/cdn-cgi/challenge-platform/h/b/jsd/r/1", False),
    ("GET", "https://perplexity.ai/discover", False),
    ("GET", "http://www.perplexity.ai/discover", False),
    ("GET", "https://www.perplexity.ai:8443/discover", False),
    ("GET", "https://user@www.perplexity.ai/discover", False),
    ("GET", "https://www.perplexity.ai.example.com/discover", False),
    ("DELETE", "https://www.perplexity.ai/discover", False),
])
def test_guard_passes_only_discover_gets(method, url, ok):
    assert request_allowed(method, url) is ok


# Parsing.

def test_row_maps_a_card():
    row = parse.row(card(1), "top")
    assert row == {
        "source": "perplexity", "id": "id-1", "url": "https://news.example.com/a/1", "title": "Example story 1",
        "author": "curator", "publishedAt": "2026-01-02T03:04:05+00:00", "snippet": "Summary 1.",
        "thumbnail": "https://img.example.com/t/1", "metrics": {"views": 7, "likes": 2, "forks": 1, "sources": 3},
        "perplexityUrl": "https://www.perplexity.ai/discover/top/example-story-1-AbCdEf", "topic": "top"}


def test_row_falls_back_and_skips_junk():
    bare = parse.row({"uuid": "x", "short_title": "Short", "featured_images": [{"url": "https://e.example/s"}],
                      "published_timestamp": "not a date", "social_info": {"view_count": True}}, "tech")
    assert (bare["title"], bare["url"], bare["publishedAt"], bare["metrics"]["views"], bare["perplexityUrl"]) == (
        "Short", "https://e.example/s", None, None, None)
    assert parse.row({"title": "no id"}, "top") is None
    assert parse.row({"uuid": "no title"}, "top") is None
    assert parse.feed_rows({"items": [card(1), "junk", {}]}, "top") == [parse.row(card(1), "top")]
    assert parse.row(card(2, published_timestamp="2026-01-02T03:04:05-05:00"), "top")["publishedAt"] == (
        "2026-01-02T08:04:05+00:00")


def test_topics_resolve_by_slug_and_science_reads_tech():
    assert parse.topic_slug("science") == "tech" and parse.topic_slug("arts") == "arts"
    assert parse.topic_id(TOPICS, "top") == "top"
    assert parse.topic_id(TOPICS, "tech") == TECH
    assert parse.topic_id(TOPICS, "sports") is None
    assert parse.topic_id({}, "tech") is None


# The feed read on a fake page.

class FakePage:
    def __init__(self, feeds=None, *, status=200, title="Perplexity Discover", answers=None, user=None):
        self.feeds, self.status, self.title, self.answers = feeds or {}, status, title, answers or {}
        self.user = user
        self.gotos, self.gets = [], []

    async def goto(self, url, **_):
        self.gotos.append(url)
        return SimpleNamespace(status=self.status)

    async def evaluate(self, script, arg=None):
        if script == fetch.TITLE_JS:
            return self.title
        self.gets.append(arg)
        parts = urlsplit(arg)
        if parts.path in self.answers:
            return self.answers[parts.path]
        if parts.path.endswith("/topics"):
            return {"status": 200, "json": TOPICS, "type": "application/json"}
        if parts.path.endswith("/session"):
            return {"status": 200, "json": {"user": self.user} if self.user else {}, "type": "application/json"}
        q = parse_qs(parts.query)
        items = self.feeds.get((q["topic"][0], int(q["offset"][0])), [])
        return {"status": 200, "json": {"status": "success", "items": items, "next_token": "n" if items else None},
                "type": "application/json"}


@pytest.fixture
def page(monkeypatch):
    holder = {}

    @asynccontextmanager
    async def site_page(policy, **_):
        holder["policy"] = policy
        yield holder["page"]

    async def no_sleep(_):
        return None

    monkeypatch.setattr(browser, "site_page", site_page)
    monkeypatch.setattr(fetch.asyncio, "sleep", no_sleep)

    def make(*args, **kwargs):
        holder["page"] = FakePage(*args, **kwargs)
        return holder["page"]

    return make


def test_discover_pages_paced_dedupes_and_caps(page):
    p = page({("top", 0): [card(i) for i in range(20)], ("top", 20): [card(i) for i in range(18, 40)]})
    rows = asyncio.run(fetch.discover("top", 30))
    assert [r["id"] for r in rows] == [f"id-{i}" for i in range(30)]
    assert {r["auth"] for r in rows} == {"public"}  # signed out: the public feed, marked
    assert p.gotos == ["https://www.perplexity.ai/discover"]
    assert urlsplit(p.gets[0]).path == "/api/auth/session"  # signed in first: the session is checked before reading
    feeds = p.gets[1:]
    assert [parse_qs(urlsplit(g).query)["offset"][0] for g in feeds] == ["0", "20"]
    assert all("version=2.18" in g and "limit=20" in g for g in feeds)


def test_discover_signed_in_marks_rows(page):
    page({("top", 0): [card(1)]}, user={"name": "Example Person", "email": "person@example.com"})
    assert [r["auth"] for r in asyncio.run(fetch.discover("top", 1))] == ["signed_in"]


def test_discover_resolves_topic_uuid_and_stops_when_empty(page):
    p = page({(TECH, 0): [card(1), card(2)]})
    rows = asyncio.run(fetch.discover("science", 10))
    assert [r["topic"] for r in rows] == ["tech", "tech"]
    assert [urlsplit(g).path for g in p.gets[:2]] == ["/api/auth/session", "/rest/discover/topics"]
    assert parse_qs(urlsplit(p.gets[2]).query)["topic"] == [TECH]
    assert len(p.gets) == 4  # session, topics, page 1, page 2 (empty): stop


def test_discover_unknown_topic_and_bad_choice(page):
    page({}, answers={"/rest/discover/topics": {"status": 200, "json": {"all_topics": []}, "type": "json"}})
    with pytest.raises(NotFound):
        asyncio.run(fetch.discover("sports", 5))
    with pytest.raises(UsageError):
        asyncio.run(fetch.discover("politics", 5))


@pytest.mark.parametrize("kwargs", [{"status": 403}, {"status": 503}, {"title": "Just a moment..."}])
def test_a_challenged_document_stops(page, kwargs):
    p = page({}, **kwargs)
    with pytest.raises(fetch.Blocked, match="does not work around"):
        asyncio.run(fetch.discover("top", 5))
    assert p.gets == []


@pytest.mark.parametrize("answer", [{"status": 429, "json": None, "type": "text/html"},
                                    {"status": 200, "json": None, "type": "text/html"}])
def test_a_challenged_feed_stops(page, answer):
    page({}, answers={"/rest/discover/feed": answer})
    with pytest.raises(fetch.Blocked):
        asyncio.run(fetch.discover("top", 5))


def test_status_signed_out(page):
    page({})
    assert asyncio.run(fetch.status()) == {"signedIn": False, "auth": "public", "name": None, "email": None,
                                           "discover": True, "topics": ["top", "tech", "finance"]}


def test_status_signed_in(page):
    page({}, user={"name": "Example Person", "email": "person@example.com"})
    out = asyncio.run(fetch.status())
    assert (out["signedIn"], out["auth"], out["name"]) == (True, "signed_in", "Example Person")


# Registration and the commands.

def test_registered_with_its_own_login():
    site = registry()["perplexity"]
    assert (site.provider, site.ops) == ("perplexity", ("status", "discover"))
    assert callable(site.login)  # `webcli perplexity login` / `identity login perplexity:default` (a person signs in)
    assert {"status", "discover", "login", "agent-help"} <= set(site.cli().commands)


@pytest.fixture
def calls(monkeypatch):
    seen, answers = [], {}

    async def call(ident, site, op, *args, **kwargs):
        seen.append((ident.id, op, args))
        return answers[op]

    monkeypatch.setattr(daemon, "call", call)
    return seen, answers


def run(*args):
    return CliRunner().invoke(webcli_app.build(), ["perplexity", *args])


def test_commands(calls):
    seen, answers = calls
    answers["discover"] = [{**parse.row(card(1), "tech"), "auth": "public"}]
    out = run("discover", "--topic", "science", "--limit", "5", "--json")
    assert out.exit_code == 0 and json.loads(out.stdout) == answers["discover"]
    assert "webcli perplexity login" in out.stderr  # a public read says how a person signs in
    assert seen[-1] == ("perplexity:default", "discover", ("science", 5))
    assert json.loads(run("discover", "--compact", "--json").stdout)[0] == {
        "publishedAt": "2026-01-02T03:04:05+00:00", "title": "Example story 1", "url": "https://news.example.com/a/1",
        "perplexityUrl": "https://www.perplexity.ai/discover/tech/example-story-1-AbCdEf"}
    assert seen[-1][2] == ("top", 30)
    answers["discover"] = []
    assert run("discover").exit_code == 3
    assert run("discover", "--topic", "politics").exit_code == 2
    assert run("discover", "--limit", "500").exit_code == 2
    answers["status"] = {"signedIn": False, "discover": True, "topics": ["top"]}
    out = run("status", "--json")
    assert out.exit_code == 0 and json.loads(out.stdout)["discover"] is True
