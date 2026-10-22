"""chromecli 0.10.0: open shadow roots (web components shaped like Shopify Polaris, tests/fixtures/chromecli/shadow.html)
in headless Chromium: read (text, md, --elements), find snapshot, --text / --selector / --ref targets, fill read-back,
click, wait probe, gates on shadow fields, the grant inspect mode, and script_error carrying the page message.
Skips when Playwright's Chromium isn't available (never the user's Chrome)."""

from __future__ import annotations

import json

import pytest

from test_chromecli_dom import EXT, FIXTURES  # noqa: F401
from test_chromecli_dom import gate


@pytest.fixture(scope="module")
def spage():
    import functools
    import http.server
    import shutil
    import threading
    from pathlib import Path

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
        src = "".join((EXT / f).read_text().replace("export function", "function")
                      for f in ("page/actions.js", "page/extract.js", "page/snapshot.js"))
        src += "; window.__pa = pageAction; window.__ex = extractPage; window.__snap = pageSnapshot; window.__probe = pageProbe;"
        url = f"http://127.0.0.1:{srv.server_address[1]}/shadow.html"

        def fresh():
            pg = browser.new_page()
            pg.goto(url)
            pg.wait_for_function("() => document.querySelector('s-button').shadowRoot")
            pg.evaluate(src)
            return pg

        yield fresh
        browser.close()
    srv.shutdown()


def act(pg, **spec):
    return pg.evaluate("s => window.__pa(s)", spec)


def test_light_dom_query_really_cant_see_the_controls(spage):
    pg = spage()
    assert pg.evaluate("() => document.querySelectorAll('input, button').length") == 0  # the 0.9 failure mode


def test_read_text_and_md_include_shadow_content(spage):
    pg = spage()
    text = pg.evaluate("() => window.__ex({as: 'text', maxChars: 100000})")
    assert text.get("ok") is not False and text["url"].endswith("/shadow.html") and text["title"].startswith("Shadow admin")
    for s in ("Store settings", "Danger zone", "Store name", "Order total $44.99", "Delete store"):
        assert s in text["content"], (s, text["content"])
    md = pg.evaluate("() => window.__ex({as: 'md', maxChars: 100000})")
    assert "Store settings" in md["content"] and "Delete store" in md["content"]


def test_elements_and_snapshot_pierce_shadow_roots_and_refs_work(spage):
    pg = spage()
    res = pg.evaluate("() => window.__ex({as: 'text', maxChars: 1000, elements: true})")
    names = {e["name"]: e for e in res["elements"]}
    for n in ("Store name", "Contact email", "Password", "Plan", "Delete store", "Cancel"):
        assert n in names, (n, list(names))
    assert names["Store name"].get("inShadow") is True
    snap = pg.evaluate("() => window.__snap({max: 300})")
    assert {"Store name", "Delete store"} <= {e["name"] for e in snap["elements"]}
    ref = names["Delete store"]["ref"]
    d = act(pg, mode="describe", op="click", target={"ref": ref})
    assert d["ok"] and d["element"]["name"] == "Delete store" and d["element"]["tag"] == "button"


def test_text_and_selector_targets_fill_and_click_inside_shadow(spage):
    pg = spage()
    d = act(pg, mode="describe", op="fill", target={"text": "Store name"})
    assert d["ok"], d
    assert gate("fill", d["element"]) == []
    r = act(pg, mode="do", op="fill", target={"text": "Store name"}, value="as-described-review",
            fingerprint=d["fingerprint"])
    assert r["readBack"] is True and "as-described-review" not in json.dumps(r)
    assert pg.evaluate("() => document.getElementById('store-name').value") == "as-described-review"
    # a selector naming the web-component host means its one inner control
    d = act(pg, mode="describe", op="click", target={"selector": "#delete-store"})
    assert d["ok"] and d["element"]["tag"] == "button" and d["element"]["name"] == "Delete store"
    assert gate("click", d["element"]) == ["submit_word:delete"]
    r = act(pg, mode="do", op="click", target={"text": "Delete store", "role": "button"})
    assert r["dispatched"]
    assert pg.inner_text("#light-status") == "clicked delete-store"
    r = act(pg, mode="do", op="select", target={"text": "Plan"}, value="Plus")
    assert r["readBack"] is True and r["selected"] == {"value": "plus", "label": "Plus"}


def test_shadow_password_field_is_still_gated(spage):
    pg = spage()
    d = act(pg, mode="describe", op="fill", target={"text": "Password"})
    assert d["ok"] and d["element"]["type"] == "password"
    assert "password_field" in gate("fill", d["element"])


def test_wait_probe_finds_shadow_text_and_selectors(spage):
    pg = spage()
    assert pg.evaluate("() => window.__probe({text: 'Order total $44.99'})")["found"] is True
    assert pg.evaluate("() => window.__probe({selector: 'input[name=storeName]'})")["found"] is True
    assert pg.evaluate("() => window.__probe({text: 'nowhere to be found'})")["found"] is False


def test_grant_inspect_mode_reads_amounts_and_selector_hits(spage):
    pg = spage()
    r = act(pg, mode="inspect", op="click", target={"text": "Delete store"}, selectors=["#delete-store", "#cancel"],
            amountSelectors=["s-text"], autoAmount=True)
    assert r["ok"] and r["selectorHits"] == {"#delete-store": True, "#cancel": False}
    assert any("$44.99" in t for t in r["amountTexts"]["s-text"])
    assert any("$44.99" in t for t in r["amountTexts"]["_auto"])


def test_script_error_carries_the_page_message(spage):
    pg = spage()
    pg.evaluate("() => { Element.prototype.getBoundingClientRect = () => { throw new TypeError('boom in page'); }; }")
    r = act(pg, mode="describe", op="click", target={"text": "Cancel"})
    assert r["ok"] is False and r["code"] == "script_error" and "TypeError: boom in page" in r["message"]
