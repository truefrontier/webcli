"""chromecli page/actions.js + gate.js on the real fixture DOM (tests/fixtures/chromecli/form.html) in headless
Chromium: element descriptions, gates, fill/type/select read-back, refs from extract.js, submit, fingerprint guard.
Skips when Playwright's Chromium or node isn't available. Runs the injected functions in the page's main world (the
extension runs them in its isolated world; the DOM APIs are the same)."""

from __future__ import annotations

import functools
import http.server
import json
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
EXT = REPO / "extensions" / "chromecli"
FIXTURES = REPO / "tests" / "fixtures" / "chromecli"


@pytest.fixture(scope="module")
def page():
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
        # Playwright's own Chromium, else any cached headless shell (never the user's Chrome).
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
        pg.goto(f"http://127.0.0.1:{srv.server_address[1]}/form.html")
        src = "".join((EXT / f).read_text().replace("export function", "function") for f in ("page/actions.js", "page/extract.js"))
        pg.evaluate(src + "; window.__pa = pageAction; window.__ex = extractPage;")
        yield pg
        browser.close()
    srv.shutdown()


def act(pg, **spec):
    return pg.evaluate("s => window.__pa(s)", spec)


def gate(op, el, key=None):
    js = (f"import({json.dumps((EXT / 'gate.js').as_uri())}).then(m => console.log(JSON.stringify("
          f"m.gateReasons({json.dumps(op)}, {json.dumps(el)}, {{key: {json.dumps(key)}}}))))")
    return json.loads(subprocess.run(["node", "--input-type=module", "-e", js], capture_output=True, text=True,
                                     check=True).stdout)


CASES = [
    ("fill", {"selector": "#email"}, None, []),
    ("fill", {"selector": "#password"}, None, ["password_field", "credential_field"]),
    ("select", {"selector": "#country"}, None, []),
    ("click", {"text": "Next", "role": "link"}, None, []),
    ("click", {"text": "Place order"}, None, ["submit_button", "submit_word:order"]),
    ("click", {"text": "Delete account", "role": "button"}, None, ["submit_word:delete"]),
    ("press", {"selector": "#email"}, "Enter", ["enter_in_form"]),
    ("press", {"selector": "#email"}, "Escape", []),
]


@pytest.mark.parametrize(("op", "target", "key", "reasons"), CASES)
def test_gates_on_the_fixture(page, op, target, key, reasons):
    d = act(page, mode="describe", op=op, target=target, key=key)
    assert d["ok"], d
    assert gate(op, d["element"], key) == reasons


def test_targets_and_errors(page):
    assert act(page, mode="describe", op="click", target={"selector": "button"})["code"] == "ambiguous_target"
    assert act(page, mode="describe", op="click", target={"selector": "##"})["code"] == "bad_target"
    assert act(page, mode="describe", op="click", target={"text": "Nope"})["code"] == "element_not_found"
    assert act(page, mode="describe", op="click", target={"ref": "e99"})["code"] == "element_not_found"
    assert act(page, mode="describe", op="select", target={"selector": "#email"})["code"] == "bad_target"


def test_fill_type_select_read_back_and_no_values_returned(page):
    r = act(page, mode="do", op="fill", target={"selector": "#email"}, value="kevin@example.com")
    assert r["readBack"] is True and "kevin@example.com" not in json.dumps(r)
    r = act(page, mode="do", op="type", target={"selector": "#email"}, value="x")
    assert r["readBack"] is True and page.input_value("#email") == "kevin@example.comx"
    r = act(page, mode="do", op="select", target={"selector": "#country"}, value="Canada")
    assert r["readBack"] is True and r["selected"] == {"value": "CA", "label": "Canada"}
    assert "kevin" not in json.dumps(act(page, mode="describe", op="fill", target={"selector": "#email"}))


def test_refs_submit_and_fingerprint(page):
    refs = {e["name"]: e["ref"] for e in page.evaluate("() => window.__ex({elements: true})")["elements"]}
    assert act(page, mode="describe", op="click", target={"ref": refs["Place order"]})["element"]["name"] == "Place order"
    r = act(page, mode="do", op="click", target={"ref": refs["Place order"]})
    assert r["dispatched"] and page.inner_text("#status") == "submitted"
    page.evaluate("document.getElementById('status').textContent = ''")
    assert act(page, mode="do", op="press", target={"selector": "#email"}, key="Enter")["effect"] == "submitted_form"
    assert page.inner_text("#status") == "submitted"
    d = act(page, mode="describe", op="click", target={"selector": "#delete"})
    page.evaluate("document.getElementById('delete').textContent = 'Delete everything'")
    r = act(page, mode="do", op="click", target={"selector": "#delete"}, fingerprint=d["fingerprint"])
    assert r["code"] == "element_not_found" and "changed" in r["message"]
