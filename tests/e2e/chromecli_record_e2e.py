"""Isolated E2E for chromecli recording (0.12.1). Not collected by pytest; run by hand:

    .venv/bin/python tests/e2e/chromecli_record_e2e.py            # with --allowlisted-extension-id (stands in for
                                                                   # Kevin's icon click, which can't be automated)
    .venv/bin/python tests/e2e/chromecli_record_e2e.py --noallow  # without it: the request turns awaiting_popup

Needs `playwright` in that venv and its Chromium (`playwright install chromium`; CHROMECLI_E2E_CHROME overrides the
binary). It runs Chrome for Testing on a temp profile with a temp copy of the extension (host permissions for the
fixture origins, plus a test hook) and this checkout's host on a temp WEBCLI_HOME: it never touches Kevin's Chrome,
~/.webcli or config.toml. Shares are injected into chrome.storage.session and the confirm window's Start is clicked by
Playwright. Playwright's anti-throttling flags are removed so the covered/background/minimized numbers are real."""
import hashlib, http.server, json, os, shutil, socketserver, subprocess, sys, tempfile, threading, time
from pathlib import Path
from playwright.sync_api import sync_playwright

WT = Path(__file__).resolve().parents[2]
PY = sys.executable
CFT = os.environ.get("CHROMECLI_E2E_CHROME")  # None: Playwright's own Chromium
EXT_ID = "ileggcomhbnofnapippaanhimdhchhci"
ALLOW = "--noallow" not in sys.argv
ONLY = [a for a in sys.argv[1:] if not a.startswith("--")]

W = Path(tempfile.mkdtemp(prefix="e2e-", dir="/tmp"))
HOME = W / "h"
UD = W / "ud"
env = {**os.environ, "WEBCLI_HOME": str(HOME), "PYTHONPATH": str(WT), "CHROMECLI_FORMAT": "json"}
env.pop("NO_COLOR", None)

# extension copy (+ host permissions for the fixture origins, + a test hook; the shipped files are unchanged)
shutil.copytree(WT / "extensions/chromecli", W / "ext", ignore=shutil.ignore_patterns("test"))
m = json.loads((W / "ext/manifest.json").read_text())
m["host_permissions"] = ["http://127.0.0.1/*", "http://localhost/*"]
(W / "ext/manifest.json").write_text(json.dumps(m))
with open(W / "ext/background.js", "a") as f:
    f.write("\nglobalThis.__e2e = { setPaused, revoke, popupMessage, recordings };\n")

host_sh = W / "host.sh"
host_sh.write_text(f"#!/bin/sh\nexport WEBCLI_HOME='{HOME}'\nexport PYTHONPATH='{WT}'\nexec '{PY}' -c 'import sys; from webcli.chromecli.host import main; sys.exit(main())' \"$@\"\n")
host_sh.chmod(0o755)
(UD / "NativeMessagingHosts").mkdir(parents=True)
(UD / "NativeMessagingHosts/com.truefrontier.chromecli.json").write_text(json.dumps({
    "name": "com.truefrontier.chromecli", "description": "e2e", "path": str(host_sh), "type": "stdio",
    "allowed_origins": [f"chrome-extension://{EXT_ID}/"]}))

FIX = WT / "tests/fixtures/chromecli"
class Q(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k): super().__init__(*a, directory=str(FIX), **k)
    def log_message(self, *a): pass
srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Q); srv.daemon_threads = True
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{PORT}/animated.html"

results = []
def note(name, ok, detail=""):
    results.append((name, ok, detail)); print(("PASS" if ok else "FAIL"), name, detail, flush=True)

def cli(*args, pump=None, timeout=150):
    p = subprocess.Popen([PY, "-c", "from webcli.chromecli.cli import main; main()", *args], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    end = time.time() + timeout
    while p.poll() is None and time.time() < end:
        (pump or (lambda: time.sleep(0.2)))()
    if p.poll() is None:
        p.kill()
    out, err = p.communicate()
    try: body = json.loads(out) if out.strip() else None
    except json.JSONDecodeError: body = out
    return p.returncode, body, err.strip()

with sync_playwright() as pw:
    args = [f"--disable-extensions-except={W/'ext'}", f"--load-extension={W/'ext'}", "--no-first-run",
            "--no-default-browser-check", "--window-size=1100,800", "--window-position=40,40"]
    if ALLOW:
        args.append(f"--allowlisted-extension-id={EXT_ID}")
    ctx = pw.chromium.launch_persistent_context(str(UD), headless=False, executable_path=CFT, args=args,
                                                ignore_default_args=["--disable-extensions", "--enable-automation", "--disable-background-timer-throttling", "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding", "--disable-ipc-flooding-protection"],
                                                viewport=None)
    clicked = []
    def on_page(pg):
        if "confirm.html" in pg.url or pg.url.startswith(f"chrome-extension://{EXT_ID}/confirm"):
            def go():
                try:
                    pg.wait_for_selector("#allow:not([disabled])", timeout=10000)
                    clicked.append(pg.locator("h1, .headline, #headline").first.inner_text(timeout=2000) if pg.locator("h1, .headline, #headline").count() else pg.title())
                    pg.click("#allow")
                except Exception as e:
                    clicked.append(f"ERR {e}")
            go()
    ctx.on("page", on_page)
    sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker", timeout=15000)
    page = ctx.pages[0]
    page.goto(URL)
    pump = lambda: page.wait_for_timeout(200)
    # wait for the host
    for _ in range(50):
        rc, body, err = cli("status", timeout=20)
        if rc == 0 and isinstance(body, dict) and (body.get("extension") or {}).get("connected", True):
            break
        page.wait_for_timeout(300)
    print("status rc", rc, json.dumps(body)[:400] if body else err[:300])
    tabs = sw.evaluate("async () => (await chrome.tabs.query({})).map(t => ({id: t.id, url: t.url, windowId: t.windowId}))")
    T = next(t for t in tabs if t["url"].startswith(URL))
    TID, WID = T["id"], T["windowId"]
    def share(tid, origin, level="read"):
        sw.evaluate("async ([tid, origin, level]) => { const {shares = {}} = await chrome.storage.session.get('shares'); shares[tid] = {origin, level, at: Date.now()}; await chrome.storage.session.set({shares}); }", [tid, origin, level])
    share(TID, f"http://127.0.0.1:{PORT}")
    rc, body, err = cli("list", timeout=20)
    print("list", rc, body if rc == 0 else err[:300])

    def stats(): return page.evaluate("() => ({...window.__stats, vis: document.visibilityState})")
    def shot(*extra):
        out = W / f"shot-{time.time():.3f}.png"
        rc, body, err = cli("screenshot", str(TID), "-o", str(out), *extra, pump=pump, timeout=40)
        h = hashlib.sha256(out.read_bytes()).hexdigest()[:12] if out.exists() else None
        return rc, body, err, h

    def want(name):
        return not ONLY or name in ONLY

    # 1. start: confirm window, Kevin's click (automated), recording
    rc, body, err = cli("record", "start", str(TID), "--confirm", "--fps", "30", "--max", "5m", "--wait", "20s", pump=pump)
    note("record start -> recording" if ALLOW else "record start without grant -> awaiting_popup / confirm_timeout",
         (rc == 0 and body.get("state") == "recording") if ALLOW else (rc == 4 and "chromecli icon" in err),
         f"rc={rc} clicked={clicked[-1:]} body={json.dumps(body)[:300] if body else ''} err={err[:300]}")
    if not ALLOW:
        rc2, st, _ = cli("record", "status", timeout=20)
        note("status after the expired wait", rc2 == 0, json.dumps(st)[:400])
        ctx.close(); print(json.dumps(results)); sys.exit(0)
    rec_path = Path(body["path"])
    page.wait_for_timeout(2500)
    rc, b, e, h1 = shot()
    # Injected shares carry no activeTab grant, so captureVisibleTab says permission_needed here (a real Share from the
    # popup is an icon click, which grants it). Either answer is fine; only a crash isn't.
    note("screenshot visible tab -> visible, or permission_needed without a real icon click",
         (rc == 0 and b.get("source") == "visible") or (rc == 4 and "permission_needed" in e), f"rc={rc} {e[:120]}")
    rc, b, e, h2 = shot("--covered")
    note("screenshot --covered on a visible tab -> stream", rc == 0 and b.get("source") == "stream", f"{json.dumps(b)[:250] if rc == 0 else e[:300]}")

    # 2. covered by another window
    s0 = stats()
    sw.evaluate("async () => { await chrome.windows.create({url: 'about:blank', focused: true, left: 40, top: 40, width: 1100, height: 800}); }")
    page.wait_for_timeout(2500)
    s1 = stats()
    rc, b, e, h3 = shot()
    page.wait_for_timeout(1000)
    rc2, b2, e2, h4 = shot()
    note("covered window: auto stream screenshot", rc == 0 and b.get("source") == "stream" and h3 != h4,
         f"frameAgeMs={b.get('frameAgeMs') if rc == 0 else e[:200]} frames {b.get('frames') if rc == 0 else ''}->{b2.get('frames') if rc2 == 0 else ''} changed={h3 != h4} raf/s={(s1['raf']-s0['raf'])/2.5:.0f} vis={s1['vis']}")

    # 3. background tab in the same window
    sw.evaluate("async ([wid]) => { const ws = await chrome.windows.getAll(); for (const w of ws) if (w.id !== wid) await chrome.windows.remove(w.id); await chrome.windows.update(wid, {focused: true}); await chrome.tabs.create({windowId: wid, url: 'about:blank', active: true}); }", [WID])
    page.wait_for_timeout(2500)
    s0 = stats(); page.wait_for_timeout(2000); s1 = stats()
    rc, b, e, h5 = shot(); page.wait_for_timeout(1000); rc2, b2, e2, h6 = shot()
    note("background tab: auto stream screenshot", rc == 0 and b.get("source") == "stream" and h5 != h6,
         f"frameAgeMs={b.get('frameAgeMs') if rc == 0 else e[:200]} changed={h5 != h6} raf/s={(s1['raf']-s0['raf'])/2:.0f} interval/s={(s1['interval']-s0['interval'])/2:.0f} vis={s1['vis']}")

    # 4. minimized window
    sw.evaluate("async ([wid, tid]) => { await chrome.tabs.update(tid, {active: true}); await chrome.windows.update(wid, {state: 'minimized'}); }", [WID, TID])
    page.wait_for_timeout(2500)
    s0 = stats(); page.wait_for_timeout(2000); s1 = stats()
    rc, b, e, h7 = shot(); page.wait_for_timeout(1500); rc2, b2, e2, h8 = shot()
    note("minimized window: stale stream frame comes with a warning", rc2 == 0 and b2.get("source") == "stream"
         and (b2.get("frameAgeMs", 0) <= 2000 or "minimized" in b2.get("warning", "")),
         f"frameAgeMs={b.get('frameAgeMs') if rc == 0 else e[:200]}->{b2.get('frameAgeMs') if rc2 == 0 else ''} changed={h7 != h8} raf/s={(s1['raf']-s0['raf'])/2:.0f} interval/s={(s1['interval']-s0['interval'])/2:.0f} vis={s1['vis']}")
    sw.evaluate("async ([wid]) => { await chrome.windows.update(wid, {state: 'normal', focused: true}); }", [WID])
    page.wait_for_timeout(1000)

    # 5. stop -> file
    rc, b, e = cli("record", "stop", str(TID), pump=pump, timeout=60)
    row = (b or {}).get("stopped", [{}])[0] if rc == 0 else {}
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_name,width,height", "-of", "json", str(rec_path)], capture_output=True, text=True)
    note("record stop -> playable WebM", rc == 0 and row.get("bytesWritten", 0) > 10000 and probe.returncode == 0,
         f"bytes={row.get('bytesWritten')} dur_ms={row.get('durationMs')} probe={probe.stdout.strip()[:300]} {e[:200]}")

    # 6. mp4
    mp4 = W / "out" / "demo.mp4"
    rc, b, e = cli("record", "start", str(TID), "--confirm", "--out", str(mp4), "--fps", "24", pump=pump)
    page.wait_for_timeout(3000)
    rc, b, e = cli("record", "stop", str(TID), pump=pump, timeout=90)
    row = (b or {}).get("stopped", [{}])[0] if rc == 0 else {}
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,width,height,r_frame_rate:format=duration", "-of", "json", str(mp4)], capture_output=True, text=True)
    note("--out .mp4 -> converted with ffmpeg", rc == 0 and row.get("converted") == "now" and "h264" in probe.stdout,
         f"{probe.stdout.strip()[:300]} webm_kept={mp4.with_suffix('.webm').exists()} {e[:200]}")

    # 7. auto-stop: pause
    def started():
        rc, b, e = cli("record", "start", str(TID), "--confirm", pump=pump)
        return rc == 0 and b.get("state") == "recording", (b or {}).get("recId"), e
    def last_row(rid=None):
        rc, st, _ = cli("record", "status", str(TID), timeout=20)
        rows = st if rc == 0 and isinstance(st, list) else []
        return next((r for r in rows if r.get("recId") == rid), {})
    ok, rid, e = started(); page.wait_for_timeout(2000)
    sw.evaluate("async () => { await globalThis.__e2e.setPaused(true); }"); page.wait_for_timeout(2500)
    host_rows = {r["recId"]: r for r in (json.loads(subprocess.run([PY, "-c", "from webcli.chromecli import bridge; import json; print(json.dumps(bridge.call('record.files', {})))"], env=env, capture_output=True, text=True).stdout or "{}").get("result") or {}).get("recordings", [])}
    hr = host_rows.get(rid, {})
    note("auto-stop on Pause", ok and hr.get("state") == "stopped" and hr.get("reason") == "paused" and hr.get("bytesWritten", 0) > 0, f"{json.dumps(hr)[:300]} {e[:200]}")
    sw.evaluate("async () => { await globalThis.__e2e.setPaused(false); }"); page.wait_for_timeout(500)

    # 8. auto-stop: unshare
    ok, rid, e = started(); page.wait_for_timeout(2000)
    sw.evaluate("async ([tid]) => { await globalThis.__e2e.revoke(tid, 'unshared'); }", [TID]); page.wait_for_timeout(2500)
    r = last_row(rid)
    note("auto-stop on Unshare", ok and r.get("recId") == rid and r.get("state") == "stopped" and "unshared" in str(r.get("reason")), f"{json.dumps(r)[:300]}")
    share(TID, f"http://127.0.0.1:{PORT}")

    # 9. auto-stop: origin change (127.0.0.1 -> localhost)
    ok, rid, e = started(); page.wait_for_timeout(2000)
    page.goto(f"http://localhost:{PORT}/animated.html"); page.wait_for_timeout(2500)
    r = last_row(rid)
    note("auto-stop on origin change", ok and r.get("recId") == rid and r.get("state") == "stopped" and r.get("reason") == "origin changed", f"{json.dumps(r)[:300]}")

    # 10. refusal: not shared tab (localhost now isn't shared? the share was revoked by the origin change)
    rc, b, e = cli("record", "start", str(TID), "--confirm", pump=pump, timeout=40)
    note("refused on a tab that isn't shared", rc == 4, f"rc={rc} {e[:200]}")
    rc, b, e, _ = shot()
    note("screenshot of an unshared tab refused", rc != 0, f"rc={rc} {e[:160]}")

    log = (HOME / "logs/chromecli.log").read_text()
    lines = [l for l in log.splitlines() if "event=record_" in l]
    note("log has record_start/record_stop lines", any("record_start" in l for l in lines) and any("record_stop" in l for l in lines), "\n    " + "\n    ".join(lines[:4]))
    ctx.close()
print(json.dumps([r[:2] for r in results]))
print("WORK", W)
