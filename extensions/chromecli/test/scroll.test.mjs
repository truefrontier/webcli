// page.scroll (0.10.2) in the service worker: a read op (Read and Act tabs, never prompts), the same refusals as read
// (not_shared / origin_blocked / paused), spec validation, the audit ring, and captureVisibleTab spacing.
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadBackground } from "./harness.mjs";

const URL1 = "http://127.0.0.1:8765/form.html";
// A fake page: echoes the spec back as a scroll reply.
function fakePage(over = {}) {
  return (tabId, spec, name) => {
    if (name !== "pageScroll") return { ok: true };
    const top = spec.to === "bottom" ? 2300 : spec.to === "top" ? 0 : spec.to?.top ?? (spec.by ? 660 : 0);
    return { ok: true, url: URL1, title: "Form", target: { description: "div#viewer", kind: "element", auto: !spec.container && !spec.path,
      inShadow: false, path: [[1, 0]] }, scrollTop: top, scrollLeft: 0, scrollHeight: 3000, clientHeight: 700,
      atTop: top === 0, atEnd: top + 700 >= 2998, moved: top, devicePixelRatio: 2, ...over };
  };
}

for (const level of ["read", "act"]) {
  test(`page.scroll works on a ${level} tab without confirm and opens no window`, async () => {
    const h = await loadBackground({ page: fakePage() });
    await h.share(1, level);
    const r = await h.call("page.scroll", { tab: 1, to: "bottom", container: "#viewer" }, { confirm: false });
    assert.equal(r.ok, true, JSON.stringify(r));
    assert.equal(r.result.share, level);
    assert.equal(r.result.op, "scroll");
    assert.equal(r.result.atEnd, true);
    assert.equal(r.result.scrollTop, 2300);
    assert.deepEqual(r.result.target.path, [[1, 0]]);
    assert.equal(h.windowsCreated.length, 0);
    assert.equal(h.injected.length, 1);
    assert.equal(h.injected[0].name, "pageScroll");
    assert.deepEqual(h.injected[0].spec, { to: "bottom", container: "#viewer", settleMs: 0 });
  });
}

test("page.scroll on an unshared tab -> not_shared, nothing injected", async () => {
  const h = await loadBackground({ page: fakePage() });
  assert.equal((await h.call("page.scroll", { tab: 2, by: "page" }, { confirm: false })).error.code, "not_shared");
  assert.equal(h.injected.length, 0);
});

test("page.scroll on a denied origin -> origin_blocked", async () => {
  const h = await loadBackground({ page: fakePage() });
  h.granted.add("https://bank.chase.com/*");
  const res = await h.message({ type: "share", tabId: 3, origin: "https://bank.chase.com", level: "read" });
  assert.equal(res.ok, false);
  assert.equal((await h.call("page.scroll", { tab: 3, to: "top" }, { confirm: false })).error.code, "origin_blocked");
  assert.equal(h.injected.length, 0);
});

test("page.scroll while paused -> paused", async () => {
  const h = await loadBackground({ page: fakePage() });
  await h.share(1, "read");
  await h.message({ type: "pause", paused: true });
  assert.equal((await h.call("page.scroll", { tab: 1, measure: true }, { confirm: false })).error.code, "paused");
  assert.equal(h.injected.length, 0);
});

test("page.scroll validates its spec", async () => {
  const h = await loadBackground({ page: fakePage() });
  await h.share(1, "read");
  const bad = [
    [{ to: "top", by: "page" }, "bad_request"],
    [{ to: "middle" }, "bad_request"],
    [{ by: -5 }, "bad_request"],
    [{ by: "page", dir: 2 }, "bad_request"],
    [{ container: 42 }, "bad_target"],
    [{ into: "x".repeat(1001) }, "bad_target"],
    [{ path: "nope" }, "bad_request"],
  ];
  for (const [args, code] of bad) {
    assert.equal((await h.call("page.scroll", { tab: 1, ...args }, { confirm: false })).error.code, code, JSON.stringify(args));
  }
  assert.equal(h.injected.length, 0);
  const ok = await h.call("page.scroll", { tab: 1, to: { top: 1234, left: 0 }, path: [[1, 0]], settleMs: 300 }, { confirm: false });
  assert.equal(ok.ok, true, JSON.stringify(ok));
  assert.deepEqual(h.injected[0].spec, { to: { top: 1234, left: 0 }, path: [[1, 0]], settleMs: 300 });
});

test("page.scroll is in the audit ring like the other read ops", async () => {
  const h = await loadBackground({ page: fakePage() });
  await h.share(1, "read");
  await h.call("page.scroll", { tab: 1, by: "page" }, { confirm: false });
  await h.call("page.scroll", { tab: 2, by: "page" }, { confirm: false });
  const { audit } = await h.chrome.storage.session.get("audit");
  const rows = audit.filter((a) => a.op === "page.scroll");
  assert.deepEqual(rows.map((a) => [a.tab, a.outcome]), [[1, "ok"], [2, "not_shared"]]);
});

test("a tab that navigated away during the scroll is unshared -> not_shared", async () => {
  const h = await loadBackground({ page: fakePage({ url: "https://elsewhere.example/" }) });
  await h.share(1, "read");
  assert.equal((await h.call("page.scroll", { tab: 1, by: "page" }, { confirm: false })).error.code, "not_shared");
  assert.equal((await h.call("tabs.read", { tab: 1 }, { confirm: false })).error.code, "not_shared");
});

test("captureVisibleTab calls are spaced out, and a quota error is retried once", async () => {
  const h = await loadBackground();
  await h.share(1, "read");
  const at = [];
  let failOnce = false;
  h.chrome.tabs.captureVisibleTab = async () => {
    at.push(Date.now());
    if (failOnce) { failOnce = false; throw new Error("This request exceeds the MAX_CAPTURE_VISIBLE_TAB_CALLS_PER_SECOND quota."); }
    return "data:image/png;base64,iVBORw0KGgo=";
  };
  globalThis.__chromecliCaptureGapMs = 120;
  try {
    for (let i = 0; i < 3; i++) assert.equal((await h.call("tabs.screenshot", { tab: 1, part: i + 1 })).ok, true);
    for (let i = 1; i < at.length; i++) assert.ok(at[i] - at[i - 1] >= 110, `gap ${at[i] - at[i - 1]}ms`);
    failOnce = true;
    at.length = 0;
    const r = await h.call("tabs.screenshot", { tab: 1 }, { deadlineMs: 5000 });
    assert.equal(r.ok, true, JSON.stringify(r));
    assert.equal(at.length, 2);
    assert.ok(at[1] - at[0] >= 950, `retry after ${at[1] - at[0]}ms`);
  } finally {
    globalThis.__chromecliCaptureGapMs = 0;
  }
});
