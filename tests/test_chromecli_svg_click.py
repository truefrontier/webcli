"""0.17.1: chromecli click on non-HTML targets (an <svg> icon, an SVG <path>, a bare <svg>), in headless Chromium on
tests/fixtures/chromecli/svg.html. SVGElement has no .click(): the click resolves to the closest clickable HTML
ancestor, else the nearest HTML ancestor with a dispatched pointer/mouse/click sequence at the icon's center. The
element description (what gates, Jev, grants and the confirm window see) is the resolved element."""

from __future__ import annotations

import functools
import http.server
import json
import shutil
import threading
from pathlib import Path

import pytest

from test_chromecli_dom import gate

REPO = Path(__file__).resolve().parents[1]
EXT = REPO / "extensions" / "chromecli"
FIXTURES = REPO / "tests" / "fixtures" / "chromecli"


@pytest.fixture(scope="module")
def browser_page():
    if not shutil.which("node"):
        pytest.skip("node not installed")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(FIXTURES))
    handler.log_message = lambda *a: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
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
            srv.shutdown()
            pytest.skip("no headless Chromium")
        pg = browser.new_page()
        url = f"http://127.0.0.1:{srv.server_address[1]}/svg.html"
        src = (EXT / "page/actions.js").read_text().replace("export function", "function") + "; window.__pa = pageAction;"
        yield pg, url, src
        browser.close()
    srv.shutdown()


@pytest.fixture
def page(browser_page):
    pg, url, src = browser_page
    pg.goto(url)
    pg.evaluate(src)
    return pg


def act(pg, **spec):
    return pg.evaluate("s => window.__pa(s)", spec)


def click(pg, selector):
    d = act(pg, mode="describe", op="click", target={"selector": selector})
    assert d["ok"], d
    done = act(pg, mode="do", op="click", target={"selector": selector}, fingerprint=d["fingerprint"])
    return d, done, pg.evaluate("window.log")


def test_svg_icon_inside_a_button_clicks_the_button_once(page):
    d, done, log = click(page, "svg#add-user-btn-icon")
    assert done["ok"] and done["dispatched"], done
    assert d["element"]["tag"] == "button" and d["element"]["id"] == "add-user-btn"
    assert d["element"]["name"] == "Add user"
    assert d["resolved"] == {"via": "clickable_ancestor",
                             "from": {"tag": "svg", "id": "add-user-btn-icon", "role": None, "name": None}}
    assert done["resolved"] == d["resolved"]
    assert [x for x in log if x.startswith("btn:click")] and len([x for x in log if x.startswith("btn:click")]) == 1
    assert any(x.startswith("icon:pointerdown:add-user-btn-icon:") for x in log)  # the icon saw its own pointerdown
    cx, cy = page.evaluate("(() => { const r = document.getElementById('add-user-btn-icon').getBoundingClientRect();"
                           " return [Math.round(r.left + r.width / 2), Math.round(r.top + r.height / 2)]; })()")
    assert f"icon:pointerdown:add-user-btn-icon:{cx},{cy}" in log


def test_svg_path_resolves_to_the_same_button(page):
    d, done, log = click(page, "#add-user-btn-path")
    assert done["ok"] and d["element"]["id"] == "add-user-btn"
    assert d["resolved"]["via"] == "clickable_ancestor" and d["resolved"]["from"]["tag"] == "path"
    assert len([x for x in log if x.startswith("btn:click")]) == 1


def test_bare_svg_dispatches_the_sequence_at_its_center(page):
    d, done, log = click(page, "#bare-svg")
    assert done["ok"], done
    assert d["resolved"]["via"] == "nearest_html" and d["resolved"]["from"] == {
        "tag": "svg", "id": "bare-svg", "role": None, "name": None}
    assert d["element"]["tag"] == "div" and d["element"]["id"] == "bare-wrap"
    cx, cy = page.evaluate("(() => { const r = document.getElementById('bare-svg').getBoundingClientRect();"
                           " return [Math.round(r.left + r.width / 2), Math.round(r.top + r.height / 2)]; })()")
    assert f"svg:click:bare-svg:{cx},{cy}" in log  # the SVG's own listener, at its center
    assert f"wrap:click:bare-svg:{cx},{cy}" in log  # bubbled to the HTML ancestor, once
    assert len([x for x in log if x.startswith("wrap:click")]) == 1


def test_svg_inside_a_link_uses_the_html_anchor(page):
    d, done, _ = click(page, "#link-icon")
    assert done["ok"] and d["element"]["tag"] == "a" and d["element"]["role"] == "link"
    assert page.evaluate("location.hash") == "#linked"


def test_gates_and_disabled_apply_to_the_resolved_element(page):
    d = act(page, mode="describe", op="click", target={"selector": "#delete-icon"})
    assert d["element"]["name"] == "Delete account"
    assert gate("click", d["element"]) == ["submit_word:delete"]
    off = act(page, mode="describe", op="click", target={"selector": "#off-icon"})
    assert off["ok"] is False and off["code"] == "element_not_found" and "disabled" in off["message"]
    assert off["resolved"]["from"]["id"] == "off-icon"
    done = act(page, mode="do", op="click", target={"selector": "#off-icon"})
    assert done["ok"] is False and not [x for x in page.evaluate("window.log") if x.startswith("off:")]


def test_fingerprint_guard_and_grant_selector_hits(page):
    d = act(page, mode="describe", op="click", target={"selector": "#add-user-btn-icon"})
    insp = act(page, mode="inspect", op="click", target={"selector": "#add-user-btn-icon"},
               selectors=["#add-user-btn-icon", "#add-user-btn", "#delete-btn"])
    assert insp["fingerprint"] == d["fingerprint"]
    assert insp["selectorHits"] == {"#add-user-btn-icon": True, "#add-user-btn": True, "#delete-btn": False}
    stale = act(page, mode="do", op="click", target={"selector": "#add-user-btn-icon"}, fingerprint="other")
    assert stale["ok"] is False and stale["code"] == "element_not_found"
    assert not [x for x in page.evaluate("window.log") if x.startswith("btn:")]


def test_html_targets_and_other_ops_are_unchanged(page):
    d, done, log = click(page, "#plain-btn")
    assert "resolved" not in d and "resolved" not in done and done["ok"]
    assert len([x for x in log if x.startswith("plain:click")]) == 1
    typed = act(page, mode="describe", op="fill", target={"selector": "#bare-svg"})
    assert typed["ok"] is False and typed["code"] == "bad_target"  # no resolution outside click
