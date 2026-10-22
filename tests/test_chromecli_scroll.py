"""chromecli 0.10.2: scroll and screenshot --full-page.

Page side (headless Chromium on tests/fixtures/chromecli/scroll.html, like test_chromecli_shadow.py; skipped without
Playwright's Chromium, never the user's Chrome): auto-detecting the inner scroll container over the window, the
--container override, --to top/bottom, --by page/px/--up, atEnd, --into, open shadow roots, the target path, and
smooth-scroll pages still moving at once.

CLI side (a real chromecli-host with a fake extension that keeps a scroll position): the scroll command's requests
and output, screenshot --full-page's files / count / atEnd / --pages, the scroll position restored afterwards (also
after an error), the exit codes for Off / denied / paused tabs, and the audit log. The extension's own checks
(Read-tab allowed, Off / denied / paused refused, no prompt) are in extensions/chromecli/test/scroll.test.mjs."""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import pytest

from test_chromecli import err, ext, home, run  # noqa: F401
from test_chromecli_dom import EXT, FIXTURES  # noqa: F401

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 32).decode()


# ---------------------------------------------------------------- page side
@pytest.fixture(scope="module")
def spage():
    import functools
    import http.server
    import shutil
    import threading

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
        # An IIFE: Playwright would call a bare function source as the page function (and scroll the page).
        body = (EXT / "page/scroll.js").read_text().replace("export async function", "async function")
        src = f"(() => {{ {body}\n window.__scroll = pageScroll; }})()"
        url = f"http://127.0.0.1:{srv.server_address[1]}/scroll.html"

        def fresh():
            pg = browser.new_page(viewport={"width": 1000, "height": 700})
            pg.goto(url)
            pg.evaluate(src)
            return pg

        yield fresh
        browser.close()
    srv.shutdown()


def sc(pg, **spec):
    r = pg.evaluate("s => window.__scroll(s)", spec)
    assert r.get("ok") is True, r
    return r


def test_auto_detect_picks_the_inner_viewer_not_the_window(spage):
    pg = spage()
    r = sc(pg, measure=True)
    assert r["target"]["kind"] == "element" and r["target"]["auto"] is True
    assert r["target"]["description"].startswith("div#viewer"), r["target"]
    assert r["scrollTop"] == 0 and r["atTop"] is True and r["atEnd"] is False and r["moved"] == 0
    assert r["scrollHeight"] > 4.5 * r["clientHeight"]  # about 5 viewports of content
    assert r["url"].endswith("/scroll.html") and r["title"] == "chromecli fixture: scroll"


def test_by_page_px_and_up_move_the_inner_viewer_instantly(spage):
    pg = spage()
    r = sc(pg)  # default: one page down (clientHeight - 40), even with scroll-behavior: smooth
    ch = r["clientHeight"]
    assert r["moved"] == ch - 40 and r["scrollTop"] == ch - 40
    assert pg.evaluate("() => window.scrollY") == 0  # the window didn't move
    r = sc(pg, by=100)
    assert r["moved"] == 100
    r = sc(pg, by="page", dir=-1)
    assert r["moved"] == -(ch - 40) and r["scrollTop"] == 100


def test_to_bottom_and_top_and_at_end(spage):
    pg = spage()
    r = sc(pg, to="bottom")
    assert r["atEnd"] is True and r["atTop"] is False
    assert r["scrollTop"] + r["clientHeight"] >= r["scrollHeight"] - 2
    r = sc(pg, by="page")  # nothing left
    assert r["moved"] == 0 and r["atEnd"] is True
    r = sc(pg, to="top")
    assert r["scrollTop"] == 0 and r["atTop"] is True and r["atEnd"] is False
    r = sc(pg, to={"top": 333})
    assert r["scrollTop"] == 333


def test_container_override_scrolls_the_window(spage):
    pg = spage()
    r = sc(pg, container="html", to="bottom")
    assert r["target"]["kind"] == "document" and r["target"]["auto"] is False
    assert r["target"]["description"] == "document (window scroll)"
    assert pg.evaluate("() => window.scrollY") > 0 and r["atEnd"] is True
    assert pg.evaluate("() => document.getElementById('viewer').scrollTop") == 0
    r = sc(pg, container="#small", by=20)
    assert r["target"]["description"].startswith("div#small") and r["moved"] == 20
    bad = pg.evaluate("() => window.__scroll({container: '#nope'})")
    assert bad["ok"] is False and bad["code"] == "element_not_found"
    bad = pg.evaluate("() => window.__scroll({container: '[[['})")
    assert bad["ok"] is False and bad["code"] == "bad_target"


def test_small_scroll_box_does_not_beat_a_scrolling_window(spage):
    pg = spage()
    pg.evaluate("() => { document.getElementById('viewer').style.display = 'none'; }")
    r = sc(pg, measure=True)
    assert r["target"]["kind"] == "document" and r["target"]["auto"] is True


def test_path_from_a_reply_targets_the_same_element(spage):
    pg = spage()
    first = sc(pg, measure=True)
    r = sc(pg, path=first["target"]["path"], by=250)
    assert r["target"]["description"] == first["target"]["description"] and r["scrollTop"] == 250
    r = sc(pg, path="document", by=50)
    assert r["target"]["kind"] == "document" and pg.evaluate("() => window.scrollY") == 50
    gone = pg.evaluate("() => window.__scroll({path: [[0, 1, 99]]})")
    assert gone["ok"] is False and gone["code"] == "element_not_found"


def test_into_scrolls_an_element_into_view_inside_the_viewer(spage):
    pg = spage()
    r = sc(pg, into="#sheet-7")
    assert r["target"]["description"].startswith("div#viewer") and r["scrollTop"] > 0
    top = pg.evaluate("() => document.getElementById('sheet-7').getBoundingClientRect().top - "
                      "document.getElementById('viewer').getBoundingClientRect().top")
    assert abs(top) < 12, top


def test_open_shadow_root_scroller_is_found(spage):
    pg = spage()
    pg.evaluate("() => { document.getElementById('viewer').style.display = 'none'; "
                "document.getElementById('shadow-host').hidden = false; }")
    r = sc(pg, measure=True)
    assert r["target"]["inShadow"] is True and r["target"]["description"].startswith("div#pane.pane"), r["target"]
    r = sc(pg, path=r["target"]["path"], to="bottom")
    assert r["atEnd"] is True
    assert pg.evaluate("() => document.getElementById('shadow-host').shadowRoot.getElementById('pane').scrollTop") > 0
    r = sc(pg, container="#pane", to="top")  # --container searches open shadow roots too
    assert r["target"]["inShadow"] is True and r["scrollTop"] == 0


# ---------------------------------------------------------------- CLI side
class Page:
    """A fake scroll target in the fake extension: 3000px of content in a 700px viewer, starting at 1234."""

    def __init__(self, top=1234, height=3000, client=700, fail_on_part=None, refuse=None):
        self.top, self.height, self.client = top, height, client
        self.fail_on_part, self.refuse = fail_on_part, refuse
        self.calls: list[tuple[str, dict]] = []

    def state(self, before):
        return {"tab": 7, "origin": "http://127.0.0.1:8765", "share": "read", "op": "scroll",
                "url": "http://127.0.0.1:8765/scroll.html", "title": "scroll",
                "target": {"description": "div#viewer", "kind": "element", "auto": True, "inShadow": False,
                           "path": [[0, 1, 1]]},
                "scrollTop": self.top, "scrollLeft": 0, "scrollHeight": self.height, "clientHeight": self.client,
                "atTop": self.top <= 1, "atEnd": self.top + self.client >= self.height - 2, "moved": self.top - before,
                "devicePixelRatio": 2}

    def __call__(self, req):
        op, a = req["op"], req["args"]
        self.calls.append((op, a))
        if self.refuse:
            return {"ok": False, "error": {"code": self.refuse, "message": f"refused: {self.refuse}"}}
        if op == "page.scroll":
            before, mx = self.top, self.height - self.client
            to = a.get("to")
            if to == "top":
                self.top = 0
            elif to == "bottom":
                self.top = mx
            elif isinstance(to, dict):
                self.top = min(mx, max(0, int(to["top"])))
            elif a.get("by") is not None:
                step = self.client - 40 if a["by"] == "page" else int(a["by"])
                self.top = min(mx, max(0, self.top + step * a.get("dir", 1)))
            return {"ok": True, "result": self.state(before)}
        if op == "tabs.screenshot":
            if self.fail_on_part and a.get("part") == self.fail_on_part:
                return {"ok": False, "error": {"code": "not_visible", "message": "Tab 7 isn't the active tab.",
                                               "tab": 7}}
            return {"ok": True, "result": {"tab": 7, "url": "http://127.0.0.1:8765/scroll.html", "title": "scroll",
                                           "origin": "http://127.0.0.1:8765", "share": "read", "format": "png",
                                           "data": PNG}}
        return {"ok": True, "result": {}}


def out(r) -> dict:
    assert r.exit_code == 0, (r.exit_code, r.output, r.stderr)
    return json.loads(r.stdout)


def test_scroll_command_requests_and_output(ext, home):
    page = Page()
    ext(page).start()
    res = out(run("scroll", "7"))
    assert page.calls[-1] == ("page.scroll", {"tab": 7, "settleMs": 150, "by": "page"})
    assert res["scrollTop"] == 1234 + 660 and res["moved"] == 660 and res["clientHeight"] == 700
    assert res["target"] == {"description": "div#viewer", "kind": "element", "auto": True, "inShadow": False}
    assert set(res) >= {"tab", "origin", "share", "target", "scrollTop", "scrollHeight", "clientHeight", "atEnd"}
    out(run("scroll", "7", "--by", "300px", "--up"))
    assert page.calls[-1][1] == {"tab": 7, "settleMs": 150, "by": 300, "dir": -1}
    res = out(run("scroll", "7", "--to", "bottom", "--container", "#viewer"))
    assert page.calls[-1][1] == {"tab": 7, "settleMs": 150, "container": "#viewer", "to": "bottom"}
    assert res["atEnd"] is True
    res = out(run("scroll", "7", "--to", "top"))
    assert res["atTop"] is True and res["scrollTop"] == 0
    out(run("scroll", "--active", "--into", "#sheet-7"))
    assert page.calls[-1][1] == {"active": True, "settleMs": 150, "into": "#sheet-7"}


@pytest.mark.parametrize("argv", [["scroll", "7", "--to", "top", "--by", "page"], ["scroll", "7", "--up", "--to", "top"],
                                  ["scroll", "7", "--by", "lots"], ["scroll"], ["screenshot", "7", "--pages", "3"],
                                  ["screenshot", "7", "--out", "/tmp/x"], ["screenshot", "7", "--full-page", "-o", "a.png"],
                                  ["screenshot", "7", "--full-page", "--pages", "51"]])
def test_usage_errors_exit_2(ext, home, argv):
    page = Page()
    ext(page).start()
    r = run(*argv)
    assert r.exit_code == 2, (argv, r.output)
    assert not page.calls


@pytest.mark.parametrize("code", ["not_shared", "origin_blocked", "paused", "policy_pending"])
def test_off_denied_paused_tabs_exit_4(ext, home, code):
    ext(Page(refuse=code)).start()
    r = run("scroll", "7")
    assert r.exit_code == 4 and err(r)["error"] == code
    r = run("screenshot", "7", "--full-page")
    assert r.exit_code == 4 and err(r)["error"] == code


def test_full_page_files_count_at_end_and_restore(ext, home):
    page = Page()
    ext(page).start()
    res = out(run("screenshot", "7", "--full-page"))
    # 3000px in a 700px viewer, 660px steps: tops 0, 660, 1320, 1980, 2300 (the end)
    assert res["count"] == 5 and res["atEnd"] is True and res["fullPage"] is True
    assert [p["scrollTop"] for p in res["pages"]] == [0, 660, 1320, 1980, 2300]
    assert [Path(f).name for f in res["files"]] == [f"page-0{i}.png" for i in range(1, 6)]
    assert all(Path(f).read_bytes().startswith(b"\x89PNG") for f in res["files"])
    assert re.search(r"/chromecli/screenshots/tab7-\d{8}-\d{6}-full$", res["dir"])
    assert res["restored"] is True and res["restoredTo"] == {"top": 1234, "left": 0} and page.top == 1234
    assert res["target"] == {"description": "div#viewer", "kind": "element", "auto": True, "inShadow": False}
    assert res["overlapPx"] == 40 and res["devicePixelRatio"] == 2 and "data" not in json.dumps(res["pages"])
    ops = [(op, a.get("measure"), a.get("to"), a.get("by"), a.get("part")) for op, a in page.calls]
    assert ops[0] == ("page.scroll", True, None, None, None)
    assert ops[1] == ("page.scroll", None, "top", None, None)
    assert ops[-1] == ("page.scroll", None, {"top": 1234, "left": 0}, None, None)
    assert [a.get("part") for op, a in page.calls if op == "tabs.screenshot"] == [1, 2, 3, 4, 5]
    # every scroll after the first goes to the same element (the path), with time to render
    assert all(a["path"] == [[0, 1, 1]] for op, a in page.calls[1:] if op == "page.scroll")
    assert all(a["settleMs"] == 300 for op, a in page.calls[1:-1] if op == "page.scroll")


def test_full_page_pages_cap_out_dir_container_and_jpeg(ext, home, tmp_path):
    page = Page(top=0)
    ext(page).start()
    res = out(run("screenshot", "7", "--full-page", "--pages", "2", "--out", str(tmp_path / "run1"),
                  "--container", "#viewer", "--jpeg"))
    assert res["count"] == 2 and res["atEnd"] is False and res["format"] == "jpeg"
    assert [Path(f).name for f in res["files"]] == ["page-01.jpg", "page-02.jpg"]
    assert Path(res["dir"]) == (tmp_path / "run1").resolve()
    assert page.calls[0][1] == {"tab": 7, "measure": True, "container": "#viewer"}
    assert page.top == 0 and res["restored"] is True


def test_full_page_short_page_is_one_shot(ext, home):
    page = Page(top=0, height=650)
    ext(page).start()
    res = out(run("screenshot", "7", "--full-page"))
    assert res["count"] == 1 and res["atEnd"] is True


def test_full_page_restores_the_scroll_position_after_an_error(ext, home):
    page = Page(fail_on_part=3)
    ext(page).start()
    r = run("screenshot", "7", "--full-page", "--out", str(home / "fp"))
    assert r.exit_code == 4, r.output
    e = err(r)
    assert e["error"] == "not_visible" and e["restored"] is True and e["restoredTo"] == {"top": 1234, "left": 0}
    assert e["partial"] == {"count": 2, "files": ["page-01.png", "page-02.png"]}
    assert all((home / "fp" / f).exists() for f in e["partial"]["files"])
    assert page.top == 1234
    assert page.calls[-1][0] == "page.scroll" and page.calls[-1][1]["to"] == {"top": 1234, "left": 0}


def test_scroll_and_full_page_are_in_the_audit_log(ext, home):
    ext(Page()).start()
    out(run("scroll", "7", "--into", "#sheet-7"))
    out(run("screenshot", "7", "--full-page", "--pages", "2"))
    log = (home / "logs" / "chromecli.log").read_text()
    assert re.search(r"op=page\.scroll tab=7 into=#sheet-7 .*outcome=ok exit=0", log), log
    assert re.search(r"op=page\.scroll tab=7 measure=1 ", log)
    assert re.search(r"op=page\.scroll tab=7 to=top path=1 ", log)
    assert re.search(r"op=tabs\.screenshot tab=7 full_page_part=2 .*outcome=ok", log)
    assert "iVBOR" not in log and PNG[:20] not in log


def test_agent_help_mentions_scroll_and_full_page(home):
    r = run("agent-help")
    assert r.exit_code == 0
    assert "scroll" in r.output and "--full-page" in r.output
