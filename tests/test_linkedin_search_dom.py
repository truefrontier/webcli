"""webcli linkedin's search-card scripts (fetch.CARDS_READY_JS / POST_CARD_JS) on sanitized result pages
(tests/fixtures/linkedin: made-up people and posts) in headless Chromium. LinkedIn serves the same cards in two
markups: one tags its parts with data-view-name, the other (served once another command ran in the same browser)
carries none, which read as "no result cards" before 0.12.4. Skips without a headless Chromium. Nothing reaches
LinkedIn: each fixture is set as the page's content."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from webcli.sites.linkedin import fetch, posts

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "linkedin"
NOW = datetime(2026, 10, 23, 17, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def chromium():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed")
    with sync_playwright() as p:
        browser = None
        shells = sorted(Path.home().glob("Library/Caches/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell"))
        shells += sorted(Path.home().glob(".cache/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell"))
        options = [{}, *({"executable_path": str(exe)} for exe in reversed(shells)), {"channel": "chrome"}]
        for option in options:
            try:
                browser = p.chromium.launch(**option)
                break
            except Exception:
                continue
        if browser is None:
            pytest.skip("no headless Chromium")
        yield browser
        browser.close()


def read(chromium, name: str) -> tuple[bool, list[dict]]:
    page = chromium.new_page()
    try:
        page.route("**/*", lambda route: route.abort())
        page.set_content((FIXTURES / name).read_text())
        ready = page.evaluate(fetch.CARDS_READY_JS, fetch.SEARCH_FEED)
        return ready, page.evaluate(fetch.POST_CARD_JS, fetch.SEARCH_FEED)
    finally:
        page.close()


@pytest.mark.parametrize("name", ["search-untagged.html", "search-tagged.html"])
def test_search_cards_parse_in_both_markups(chromium, name):
    ready, raw = read(chromium, name)
    assert ready
    rows = posts.cards(raw, now=NOW)
    assert [r["author_name"] for r in rows] == ["Jane Doe", "Example Co", "John Roe"]
    jane, company, john = rows
    assert jane["author_handle"] == "jane-doe-example"
    assert jane["author_headline"] == "Head of Operations at Example Co"
    assert jane["text"].startswith("Our team rolled out an AI adoption plan")
    assert not jane["text"].endswith("more")
    assert (jane["reactions"], jane["comments"], jane["reposts"]) == (12, 3, 1)
    assert jane["kind"] == "post"
    assert company["author_handle"] == "https://www.linkedin.com/company/example-co/"
    assert company["author_headline"] is None
    assert (company["reactions"], company["comments"], company["reposts"]) == (1204, 48, None)
    assert company["kind"] == "article"
    assert john["author_headline"] == "Founder, Example Studio"
    assert john["text"].startswith("Thanks Jane Doe")  # a mention in the text is not the author
    # "Just now" and "with 2 others • Just now" are dated, so --sort latest can order them
    posted = {r["author_name"]: datetime.fromisoformat(r["posted_at"]) for r in rows}
    assert posted["Jane Doe"] == posted["John Roe"]
    assert abs(posted["Jane Doe"] - NOW) < timedelta(minutes=1)
    assert posted["Example Co"] == NOW - timedelta(hours=2)
    assert [r["author_name"] for r in posts.newest_first(rows)][-1] == "Example Co"


def test_both_markups_read_the_same(chromium):
    tagged = posts.cards(read(chromium, "search-tagged.html")[1], now=NOW)
    untagged = posts.cards(read(chromium, "search-untagged.html")[1], now=NOW)
    assert tagged == untagged


def test_fixtures_hold_only_made_up_names():
    for path in FIXTURES.glob("*.html"):
        text = path.read_text()
        for handle in set(re.findall(r"/(?:in|company)/([\w-]+)", text)):
            assert handle in {"jane-doe-example", "john-roe-example", "example-co"}, f"{path.name}: {handle}"
