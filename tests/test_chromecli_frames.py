"""chromecli 0.11.1: iframes, closed shadow roots, the skipped list, and error reasons.

Page side (headless Chromium on tests/fixtures/chromecli/frames.html, never the user's Chrome): a page whose window
doesn't scroll, with the document in a same-origin iframe that scrolls its own document (like signNow), and a small
cross-origin iframe on a second 127.0.0.1 port. page/scroll.js and page/extract.js run in each frame the way the
extension runs them (once per frame); frames.js (node) picks the scroll target across frames from the real surveys
and builds the skipped list. Also: the --container reasons (no_match / multiple_matches / not_scrollable) and the
closed shadow host and canvas regions in diag.

CLI side (a real chromecli-host with a fake extension): screenshot --full-page sends the target's frameId on every
scroll, surfaces skipped; error replies keep their reason / count / sizes; read's text output lists skipped items;
the audit log has frame=N. The service worker side (which frames are read, refs f<id>:eN, frame_ref, the bare
script_error fix) is in extensions/chromecli/test/frames.test.mjs."""

from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess

import pytest

from test_chromecli import err, ext, home, run  # noqa: F401
from test_chromecli_dom import EXT, FIXTURES  # noqa: F401

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 32).decode()


# ---------------------------------------------------------------- page side
@pytest.fixture(scope="module")
def fpage():
    import functools
    import http.server
    import threading
    from pathlib import Path

    if not shutil.which("node"):
        pytest.skip("node not installed")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed")
    servers = []
    for _ in range(2):  # the page's origin, and a second port for the cross-origin iframe
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(FIXTURES))
        handler.log_message = lambda *a: None
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
    port, other = (s.server_address[1] for s in servers)
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
            for s in servers:
                s.shutdown()
            pytest.skip("no headless Chromium")
        body = "".join((EXT / f).read_text().replace("export async function", "async function").replace("export function", "function")
                       for f in ("page/scroll.js", "page/extract.js"))
        src = f"(() => {{ {body}\n window.__scroll = pageScroll; window.__extract = extractPage; }})()"
        url = f"http://127.0.0.1:{port}/frames.html?other={other}"

        def fresh():
            pg = browser.new_page(viewport={"width": 1000, "height": 700})
            pg.goto(url)
            pg.wait_for_load_state("load")
            frames = {"top": pg.main_frame}
            for f in pg.frames:
                if f.url.endswith("/frames-inner.html"):
                    frames["inner"] = f
                elif f.url.endswith("/frames-other.html"):
                    frames["other"] = f
            assert set(frames) == {"top", "inner", "other"}, [f.url for f in pg.frames]
            for f in frames.values():
                f.evaluate(src)
            return pg, frames

        fresh.port, fresh.other = port, other
        yield fresh
        browser.close()
    for s in servers:
        s.shutdown()


def node(script: str, data) -> dict:
    """Run frames.js helpers in node on JSON input."""
    code = (f"import * as F from {json.dumps((EXT / 'frames.js').as_uri())};\n"
            "const data = JSON.parse(await new Promise((r) => { let s = ''; process.stdin.on('data', (c) => s += c); "
            "process.stdin.on('end', () => r(s)); }));\n" + script)
    p = subprocess.run(["node", "--input-type=module", "-e", code], input=json.dumps(data), capture_output=True,
                       text=True, timeout=30)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def test_each_frame_reports_its_iframes_and_what_it_cannot_read(fpage):
    pg, fr = fpage()
    top = fr["top"].evaluate("() => window.__extract({as: 'text', elements: true})")
    assert top.get("ok") is not False, top
    assert "sca-true-frontier" in top["content"] and "Page 1 of 8" not in top["content"]
    d = top["diag"]
    assert d["viewport"] == {"w": 1000, "h": 700}
    by_url = {i["url"].split("/")[-1]: i for i in d["iframes"]}
    assert by_url["frames-inner.html"]["origin"] == f"http://127.0.0.1:{fpage.port}"
    assert by_url["frames-inner.html"]["rect"] == {"x": 0, "y": 50, "w": 1000, "h": 600}
    assert by_url["frames-other.html"]["origin"] == f"http://127.0.0.1:{fpage.other}"
    assert by_url["frames-other.html"]["rect"]["h"] == 40
    # No chrome.dom in a plain page: the closed shadow root can't be opened, so it's reported (the extension opens it).
    closed = [r for r in d["regions"] if r["kind"] == "closed_shadow_host"]
    assert closed and closed[0]["description"].startswith("x-closed#closed-host") and closed[0]["reason"] == "closed_shadow_root"
    inner = fr["inner"].evaluate("() => window.__extract({as: 'text', elements: true})")
    assert "Page 1 of 8" in inner["content"] and "Signature block." in inner["content"]
    assert any(e["name"] == "Next page" for e in inner["elements"])
    canv = [r for r in inner["diag"]["regions"] if r["kind"] == "canvas"]
    assert canv and canv[0]["reason"] == "canvas_pixels" and canv[0]["rect"]["w"] == 600


def test_frame_aware_auto_detect_picks_the_viewer_iframe(fpage):
    pg, fr = fpage()
    surveys = {k: f.evaluate("() => window.__scroll({survey: true})") for k, f in fr.items()}
    assert surveys["top"]["doc"]["scrolls"] is False
    assert surveys["inner"]["doc"]["scrolls"] is True and surveys["inner"]["doc"]["scrollHeight"] >= 8000
    ids = {"top": 0, "inner": 5, "other": 6}
    rows = [{"frameId": 0, "parentFrameId": -1, "url": fr["top"].url},
            {"frameId": 5, "parentFrameId": 0, "url": fr["inner"].url},
            {"frameId": 6, "parentFrameId": 0, "url": fr["other"].url}]
    data = {"surveys": [[ids[k], v] for k, v in surveys.items() if k != "other"],
            "reports": [[ids[k], v["diag"]] for k, v in surveys.items() if k != "other"], "rows": rows}
    pick = node("""
const { geo, unmatched } = F.frameGeometry(new Map(data.reports), data.rows);
const pick = F.pickScrollTarget(new Map(data.surveys), geo);
const cls = await F.classifyFrames(data.rows, { tabOrigin: new URL(data.rows[0].url).origin, policy: { deny: [] },
  hasPermission: async () => false });
const skipped = F.skippedList({ frameSkips: cls.skipped, unmatched, reports: new Map(data.reports), geo });
console.log(JSON.stringify({ pick, allowed: cls.allowed.map((f) => f.frameId), skipped }));
""", data)
    # The viewer iframe (600px of a 700px viewport) beats the top window, which doesn't scroll anyway.
    assert pick["pick"]["frameId"] == 5 and pick["pick"]["path"] == "document" and pick["pick"]["kind"] == "document"
    # Same site (127.0.0.1, another port) but no host access granted in this run: skipped, no script injected.
    assert pick["allowed"] == [0, 5]
    sk = {s.get("frameId"): s for s in pick["skipped"]}
    assert sk[6]["reason"] == "no_host_permission" and sk[6]["origin"] == f"http://127.0.0.1:{fpage.other}"
    assert sk[6]["rect"] == {"x": 0, "y": 650, "w": 300, "h": 40}
    canvas = [s for s in pick["skipped"] if s["kind"] == "canvas"]
    assert canvas and canvas[0]["frameId"] == 5 and canvas[0]["rect"]["y"] >= 50  # in top-frame coordinates
    # Scrolling the picked target: the iframe's document moves a page, the top window doesn't.
    r = fr["inner"].evaluate("() => window.__scroll({path: 'document', by: 'page'})")
    assert r["ok"] is True and r["target"]["kind"] == "document" and r["moved"] == 600 - 40
    assert fr["inner"].evaluate("() => window.scrollY") == 560
    assert fr["top"].evaluate("() => (document.scrollingElement || document.documentElement).scrollTop") == 0


def test_container_errors_carry_a_reason(fpage):
    pg, fr = fpage()
    t = fr["top"]
    none = t.evaluate("() => window.__scroll({container: '#nope', by: 'page'})")
    assert none["ok"] is False and none["code"] == "element_not_found" and none["reason"] == "no_match"
    assert none["matches"] == 0 and "diag" in none
    many = t.evaluate("() => window.__scroll({container: '.dup', by: 'page'})")
    assert many["code"] == "ambiguous_target" and many["reason"] == "multiple_matches" and many["count"] == 2
    assert len(many["candidates"]) == 2 and all("scrollHeight" in c for c in many["candidates"])
    flat = t.evaluate("() => window.__scroll({container: '#flat', by: 'page'})")
    assert flat["code"] == "script_error" and flat["reason"] == "not_scrollable"
    assert flat["scrollHeight"] == flat["clientHeight"] and "main#flat" in flat["message"]
    bad = t.evaluate("() => window.__scroll({container: '[[['})")
    assert bad["code"] == "bad_target" and bad["reason"] == "bad_selector"
    loc = t.evaluate("() => window.__scroll({locate: '.dup'})")
    assert loc["ok"] is True and loc["matches"] == 2 and len(loc["scrollable"]) == 2
    one = fr["inner"].evaluate("() => window.__scroll({locate: 'html'})")
    assert one["scrollable"][0]["path"] == "document"


# ---------------------------------------------------------------- CLI side
class FramePage:
    """A fake extension whose scroll target is iframe 5's document: 3000px in a 600px frame."""

    def __init__(self, top=0, refuse=None):
        self.top, self.refuse = top, refuse
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, req):
        op, a = req["op"], req["args"]
        self.calls.append((op, a))
        if self.refuse:
            return {"ok": False, "error": self.refuse}
        if op == "page.scroll":
            before, mx = self.top, 3000 - 600
            if a.get("to") == "top":
                self.top = 0
            elif isinstance(a.get("to"), dict):
                self.top = int(a["to"]["top"])
            elif a.get("by") is not None:
                self.top = min(mx, self.top + (560 if a["by"] == "page" else int(a["by"])))
            res = {"tab": 7, "origin": "https://app.signnow.com", "share": "read", "op": "scroll",
                   "url": "https://app.signnow.com/webapp/document/x", "title": "signNow",
                   "target": {"description": "document (window scroll) in iframe https://app.signnow.com/viewer",
                              "kind": "document", "auto": True, "inShadow": False, "path": "document", "frameId": 5,
                              "frameOrigin": "https://app.signnow.com", "frameUrl": "https://app.signnow.com/viewer"},
                   "scrollTop": self.top, "scrollLeft": 0, "scrollHeight": 3000, "clientHeight": 600,
                   "atTop": self.top == 0, "atEnd": self.top >= mx, "moved": self.top - before, "devicePixelRatio": 2}
            if a.get("measure"):
                res["skipped"] = [{"kind": "iframe", "frameId": 9, "origin": "https://www.google.com",
                                   "url": "https://www.google.com/recaptcha/api2/anchor", "reason": "other_site",
                                   "rect": {"x": 0, "y": 650, "w": 300, "h": 40}}]
            return {"ok": True, "result": res}
        if op == "tabs.screenshot":
            return {"ok": True, "result": {"tab": 7, "url": "https://app.signnow.com/webapp/document/x", "title": "signNow",
                                           "origin": "https://app.signnow.com", "share": "read", "format": "png", "data": PNG}}
        if op == "tabs.read":
            return {"ok": True, "result": {"tab": 7, "url": "https://app.signnow.com/webapp/document/x", "title": "signNow",
                                           "origin": "https://app.signnow.com", "share": "read", "as": "text",
                                           "content": "Header\n\n[frame 5: https://app.signnow.com/viewer 1000x600]\nPage 1",
                                           "chars": 60, "truncated": False,
                                           "frames": [{"frameId": 0}, {"frameId": 5}],
                                           "skipped": [{"kind": "canvas", "frameId": 5, "description": "canvas#page-1",
                                                        "rect": {"x": 0, "y": 50, "w": 900, "h": 1200},
                                                        "reason": "canvas_pixels"}]}}
        return {"ok": True, "result": {}}


def out(r) -> dict:
    assert r.exit_code == 0, (r.exit_code, r.output, r.stderr)
    return json.loads(r.stdout)


def test_full_page_follows_the_iframe_target_and_reports_skipped(ext, home):
    page = FramePage(top=1200)
    ext(page).start()
    res = out(run("screenshot", "7", "--full-page"))
    assert res["count"] == 6 and res["atEnd"] is True  # 0, 560, 1120, 1680, 2240, 2400 (the end)
    assert res["target"]["frameId"] == 5 and "path" not in res["target"]
    assert res["skipped"][0]["reason"] == "other_site"
    scrolls = [a for op, a in page.calls if op == "page.scroll"]
    assert "frameId" not in scrolls[0]  # the measure call: auto-detect across frames
    assert all(a["frameId"] == 5 and a["path"] == "document" for a in scrolls[1:])
    assert res["restored"] is True and page.top == 1200
    log = (home / "logs" / "chromecli.log").read_text()
    assert re.search(r"op=page\.scroll tab=7 to=top frame=5 path=1 ", log), log


def test_scroll_output_keeps_the_frame_fields(ext, home):
    ext(FramePage()).start()
    res = out(run("scroll", "7"))
    assert res["target"]["frameId"] == 5 and res["target"]["frameOrigin"] == "https://app.signnow.com"
    assert "path" not in res["target"]


@pytest.mark.parametrize("error,exit_code", [
    ({"code": "element_not_found", "message": "No element matches --container in any frame chromecli can read.",
      "reason": "no_match", "matches": 0}, 3),
    ({"code": "ambiguous_target", "message": "--container matches 3 scrollable elements; give a narrower selector.",
      "reason": "multiple_matches", "count": 3, "candidates": [{"frameId": 0, "description": "div.a"}]}, 3),
    ({"code": "script_error", "message": "--container matched main#app, which doesn't scroll (scrollHeight 1042, "
      "clientHeight 1042).", "reason": "not_scrollable", "scrollHeight": 1042, "clientHeight": 1042}, 5),
    ({"code": "script_error", "message": "Frame 9 is skipped (other_site).", "reason": "frame_unreachable",
      "frameId": 9}, 5),
])
def test_error_reasons_reach_the_cli(ext, home, error, exit_code):
    ext(FramePage(refuse=error)).start()
    r = run("scroll", "7", "--container", "main")
    assert r.exit_code == exit_code, r.output
    e = err(r)
    assert e["error"] == error["code"] and e["reason"] == error["reason"]
    assert e["message"] == error["message"]  # not a bare "script_error"
    for k in ("count", "scrollHeight", "clientHeight", "matches", "frameId"):
        if k in error:
            assert e[k] == error[k]


def test_read_text_lists_skipped_items(ext, home):
    ext(FramePage()).start()
    r = run("read", "7", "--format", "text")
    assert r.exit_code == 0, r.output
    assert "[frame 5: https://app.signnow.com/viewer 1000x600]" in r.stdout
    assert "[skipped canvas canvas#page-1 900x1200 at 0,50: canvas_pixels]" in r.stdout
    res = out(run("read", "7", "--format", "json"))
    assert res["skipped"][0]["kind"] == "canvas" and res["frames"][1]["frameId"] == 5


def test_agent_help_explains_frames_and_reasons(home):
    r = run("agent-help")
    assert r.exit_code == 0
    for word in ("Iframes (0.11.1)", "f5:e3", "frame_ref", "no_match", "multiple_matches", "not_scrollable",
                 "frame_unreachable", "skipped", "other_site", "canvas_pixels"):
        assert word in r.output, word
