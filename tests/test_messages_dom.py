"""webcli messages' page scripts (fetch.py) on sanitized Google Messages DOM fixtures (tests/fixtures/messages: made-up
names, 555-01xx numbers) in headless Chromium: the Start chat picker (contacts, continuation numbers, group results,
chips), the conversation header, "Load more", and the "open in another window" banner. Skips without a headless
Chromium. Nothing reaches Google: each fixture is set as the page's content."""

from __future__ import annotations

from pathlib import Path

import pytest

from webcli.sites.messages import fetch, parse
from webcli.sites.messages.endpoints import SELECTORS

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "messages"


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
        for exe in [None, *reversed(shells)]:
            try:
                browser = p.chromium.launch(**({"executable_path": str(exe)} if exe else {}))
                break
            except Exception:
                continue
        if browser is None:
            pytest.skip("no headless Chromium")
        yield browser
        browser.close()


@pytest.fixture
def load(chromium):
    pages = []

    def open_fixture(name: str):
        page = chromium.new_page()
        page.set_content((FIXTURES / name).read_text())
        pages.append(page)
        return page

    yield open_fixture
    for page in pages:
        page.close()


def test_fixtures_hold_no_real_numbers():
    for path in FIXTURES.glob("*.html"):
        for found in parse.NUMBER_IN.findall(path.read_text()):
            number = parse.normalize_number(found)
            if number:  # a phone number: only the fictional 555-01xx range
                assert number[-7:-2] == "55501", f"{path.name}: {found} isn't a 555-01xx number"


def test_picker_reads_contacts_continuations_groups_and_chips(load):
    page = load("app.html")
    snap = page.evaluate(fetch.PICKER_JS, SELECTORS)
    assert snap["open"] and snap["input"] == "Placeholder" and snap["loading"] is False
    assert snap["chips"] == ["Casey Placeholder", "2025550142"]  # from the aria-label, not the icon's text
    look = parse.picker(snap)
    assert look["contacts"] == [
        {"name": "Casey Placeholder", "numbers": [{"number": "+12025550123", "shown": "(202) 555-0123"},
                                                  {"number": "+12025550124", "shown": "(202) 555-0124"}]},
        {"name": "Jordan Placeholder", "numbers": [{"number": "+12025550177", "shown": "+1 202-555-0177"}]}]
    assert look["groups"] == [{"id": "grp9", "name": "Casey Placeholder, Robin Sample", "preview": "",
                               "timestamp": "", "unread": False}]
    # The ambiguous name stays ambiguous; a full name with one number resolves; the chips are exactly two people.
    with pytest.raises(parse.UsageError, match="2 contacts match 'Placeholder'"):
        parse.recipient("Placeholder", look["contacts"])
    with pytest.raises(parse.UsageError, match="has 2 numbers"):
        parse.recipient("Casey Placeholder", look["contacts"])
    jordan = parse.recipient("jordan placeholder", look["contacts"])
    assert jordan == {"query": "jordan placeholder", "name": "Jordan Placeholder", "number": "+12025550177"}
    assert parse.pick(snap, jordan) == ("row", 2)
    casey = {"name": "Casey Placeholder", "number": "+12025550123"}
    assert parse.same_set(snap["chips"], [casey, {"name": "", "number": "+12025550142"}])
    assert not parse.same_set(snap["chips"], [casey, jordan])


def test_pick_clicks_the_contact_row(load):
    page = load("app.html")
    assert page.evaluate(fetch.PICK_JS, [SELECTORS, "row", 2]) is True
    assert page.evaluate("window.__picked") == "jordan"


def test_header_and_list_and_load_more(load):
    page = load("app.html")
    header = page.evaluate(fetch.HEADER_JS, SELECTORS["header"])
    assert parse.header_parts(header) == ["Casey Placeholder", "Robin Sample"]
    rows = parse.conversations(page.evaluate(fetch.CONVERSATIONS_JS, SELECTORS))
    assert [(r["id"], r["name"]) for r in rows] == [("aaa1", "Robin Sample"), ("aaa2", "(202) 555-0101")]
    assert parse.resolve(rows, "202.555.0101")["id"] == "aaa2"
    assert parse.resolve(rows, "+1 (202) 555-0101")["id"] == "aaa2"
    assert page.evaluate(fetch.CLICK_JS, SELECTORS["loadMore"]) is True
    assert len(page.evaluate(fetch.CONVERSATIONS_JS, SELECTORS)) == 4
    state = page.evaluate(fetch.STATE_JS, SELECTORS)
    assert state["items"] == 4 and not state["useHere"] and not state["elsewhere"]
    assert parse.state(state) == "paired"


@pytest.mark.parametrize("name", ["elsewhere.html", "elsewhere-text-only.html"])
def test_open_elsewhere_is_detected_and_never_clicked(load, name):
    page = load(name)
    state = page.evaluate(fetch.STATE_JS, SELECTORS)
    assert parse.state(state) == "use_here"
    assert page.evaluate("window.__useHere || 0") == 0
