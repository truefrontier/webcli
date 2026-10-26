"""webcli gnews, offline: the guard (GETs only), the story parser, sections, signed-in-first with the public
fallback, and challenge handling on a fake page, plus the commands over a faked daemon. Synthetic data; nothing
reaches Google; no Chrome starts."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from webcli.apps import webcli as webcli_app
from webcli.core import browser, daemon
from webcli.core.errors import NotFound, UsageError
from webcli.sites import registry
from webcli.sites.gnews import fetch, parse
from webcli.sites.gnews.guard import POLICY


def story(n: int, **extra) -> dict:
    return {"href": f"./read/TOKEN{n}?hl=en-US", "title": f"Example headline {n}",
            "lines": ["Example Publisher", "More", f"Example headline {n}", "2 hours ago", "By Example Writer"],
            "time": "2026-01-02T03:04:05Z", "img": f"/api/attachments/IMG{n}", **extra}


# The guard: GETs to Google News; no RPC POST (follow, hide, save).

@pytest.mark.parametrize(("method", "url", "ok"), [
    ("GET", "https://news.google.com/foryou", True),
    ("GET", "https://news.google.com/", True),
    ("POST", "https://news.google.com/_/DotsSplashUi/data/batchexecute?rpcids=XaDMu", False),
    ("POST", "https://news.google.com/foryou", False),
    ("GET", "https://evil.example.com/foryou", False),
])
def test_guard_passes_gets_only(method, url, ok):
    req = SimpleNamespace(method=method, url=url, resource_type="document", headers={}, post_data=None)
    assert bool(POLICY.allowed(req, {})) is ok


def test_row_maps_a_story_card():
    r = parse.row(story(1), "foryou")
    assert r == {"source": "google_news", "id": "TOKEN1", "url": "https://news.google.com/read/TOKEN1",
                 "title": "Example headline 1", "author": "Example Writer", "publisher": "Example Publisher",
                 "publishedAt": "2026-01-02T03:04:05Z", "snippet": None,
                 "thumbnail": "https://news.google.com/api/attachments/IMG1", "metrics": {},
                 "newsUrl": "https://news.google.com/read/TOKEN1", "section": "foryou"}


def test_rows_dedupe_cap_and_skip_junk():
    raws = [story(1), story(1), {"href": "./topics/x", "title": "Nav"}, {"href": "./read/T", "title": ""},
            story(2, img="data:image/png;base64,AAA"), story(3)]
    out = parse.rows(raws, "foryou", 2)
    assert [r["id"] for r in out] == ["TOKEN1", "TOKEN2"] and out[1]["thumbnail"] is None


def test_section_href_stays_on_news_google_com():
    navs = [["Technology", "./topics/ABC?hl=en"], ["World", "https://evil.example.com/topics/x"]]
    assert parse.section_href(navs, "Technology") == "https://news.google.com/topics/ABC?hl=en"
    assert parse.section_href(navs, "World") is None and parse.section_href(navs, "Sports") is None


class FakePage:
    def __init__(self, *, signed_out=False, landed=None, status=200, challenge=False, navs=None):
        self.signed_out, self.landed, self.status, self.challenge = signed_out, landed, status, challenge
        self.navs, self.gotos, self.url = navs or [], [], ""

    async def goto(self, url, **_):
        self.gotos.append(url)
        self.url = self.landed or url
        return SimpleNamespace(status=self.status)

    async def evaluate(self, script, arg=None):
        if script == fetch.CHALLENGE_JS:
            return self.challenge
        if script == fetch.SIGNED_OUT_JS:
            return self.signed_out
        if script == fetch.NAV_JS:
            return self.navs
        if script == fetch.STORIES_JS:
            return [story(1), story(2)]
        return None

    async def wait_for_timeout(self, _ms):
        return None


@pytest.fixture
def page(monkeypatch):
    holder = {}

    @asynccontextmanager
    async def site_page(policy, **_):
        yield holder["page"]

    monkeypatch.setattr(browser, "site_page", site_page)

    def make(**kwargs):
        holder["page"] = FakePage(**kwargs)
        return holder["page"]

    return make


def test_foryou_signed_in_first(page):
    p = page()
    rows = asyncio.run(fetch.foryou("foryou", 5))
    assert [r["auth"] for r in rows] == ["signed_in", "signed_in"]
    assert p.gotos == ["https://news.google.com/foryou"]


def test_foryou_signed_out_falls_back_to_the_public_home(page):
    p = page(signed_out=True)
    rows = asyncio.run(fetch.foryou("foryou", 5))
    assert {r["auth"] for r in rows} == {"public"}
    assert p.gotos == ["https://news.google.com/foryou", "https://news.google.com/"]


def test_section_reads_its_nav_link(page):
    p = page(navs=[["Technology", "./topics/TECH"]])
    rows = asyncio.run(fetch.foryou("technology", 5))
    assert p.gotos[-1] == "https://news.google.com/topics/TECH" and rows[0]["section"] == "technology"
    with pytest.raises(NotFound):
        page(navs=[])
        asyncio.run(fetch.foryou("sports", 5))
    with pytest.raises(UsageError):
        asyncio.run(fetch.foryou("politics", 5))


@pytest.mark.parametrize("kwargs", [{"status": 429}, {"status": 403}, {"challenge": True},
                                    {"landed": "https://www.google.com/sorry/index"}])
def test_a_challenge_stops(page, kwargs):
    page(**kwargs)
    with pytest.raises(fetch.Blocked, match="does not work around"):
        asyncio.run(fetch.foryou("foryou", 5))


def test_status(page):
    page(signed_out=True)
    assert asyncio.run(fetch.status()) == {"signedIn": False, "email": None}


def test_registered_as_google():
    site = registry()["gnews"]
    assert (site.provider, site.ops) == ("google", ("status", "foryou"))


@pytest.fixture
def calls(monkeypatch):
    seen, answers = [], {}

    async def call(ident, site, op, *args, **kwargs):
        seen.append((ident.id, op, args))
        return answers[op]

    monkeypatch.setattr(daemon, "call", call)
    return seen, answers


def test_commands(calls):
    seen, answers = calls
    run = lambda *a: CliRunner().invoke(webcli_app.build(), ["gnews", *a])  # noqa: E731
    answers["foryou"] = [{**parse.row(story(1), "foryou"), "auth": "signed_in"}]
    out = run("foryou", "--limit", "5", "--json")
    assert out.exit_code == 0 and json.loads(out.stdout) == answers["foryou"]
    assert seen[-1] == ("google:default", "foryou", ("foryou", 5))
    answers["foryou"] = [{**parse.row(story(1), "foryou"), "auth": "public"}]
    out = run("foryou", "--json")
    assert out.exit_code == 0 and "public" in out.stderr
    answers["foryou"] = []
    assert run("foryou").exit_code == 3
    assert run("foryou", "--section", "politics").exit_code == 2
