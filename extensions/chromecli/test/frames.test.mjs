// chromecli 0.11.1: frames. The pure helpers (frames.js) and the service worker reading / scrolling across a tab's
// frames: which frames it reads, the skipped list, the frame-aware scroll target, and the error reasons.
import { test } from "node:test";
import assert from "node:assert/strict";
import { classifyFrames, frameGeometry, mergeRead, parseFrameRef, pickScrollTarget, sameSite, siteOf, skippedList } from "../frames.js";
import { DEFAULT_DENY } from "../policy.js";
import { loadBackground } from "./harness.mjs";

const TOP = "http://127.0.0.1:8765";
const POLICY = { deny: [...DEFAULT_DENY], read_allow: [], act_allow: [] };
const yes = async () => true;
const no = async () => false;

test("siteOf / sameSite: registrable domain, schemeful, ports ignored", () => {
  assert.equal(siteOf("app.signnow.com"), "signnow.com");
  assert.equal(siteOf("a.b.example.co.uk"), "example.co.uk");
  assert.equal(siteOf("127.0.0.1"), "127.0.0.1");
  assert.equal(siteOf("localhost"), "localhost");
  assert.ok(sameSite("https://app.signnow.com", "https://cdn.signnow.com"));
  assert.ok(!sameSite("https://app.signnow.com", "http://cdn.signnow.com"));
  assert.ok(!sameSite("https://app.signnow.com", "https://docs.google.com"));
  assert.ok(sameSite("http://127.0.0.1:8765", "http://127.0.0.1:9999"));
  assert.ok(!sameSite("http://127.0.0.1:8765", "http://localhost:8765"));
});

test("classifyFrames: same origin read; same site only with host access; other sites, denied, opaque and children skipped", async () => {
  const rows = [
    { frameId: 0, parentFrameId: -1, url: "https://app.signnow.com/webapp/document/x" },
    { frameId: 3, parentFrameId: 0, url: "https://app.signnow.com/viewer?t=secret" },
    { frameId: 4, parentFrameId: 0, url: "https://cdn.signnow.com/pdf" },
    { frameId: 5, parentFrameId: 0, url: "https://www.google.com/recaptcha/api2/anchor" },
    { frameId: 6, parentFrameId: 5, url: "about:blank" },
    { frameId: 7, parentFrameId: 0, url: "about:blank" },
    { frameId: 8, parentFrameId: 0, url: "data:text/html,hi" },
    { frameId: 9, parentFrameId: 0, url: "https://chase.com/x" },
    { frameId: 10, parentFrameId: 0, url: "https://app.signnow.com/broken", errorOccurred: true },
  ];
  const r = await classifyFrames(rows, { tabOrigin: "https://app.signnow.com", policy: POLICY, hasPermission: no });
  assert.deepEqual(r.allowed.map((f) => f.frameId), [0, 3, 7]);
  assert.equal(r.allowed[2].origin, "https://app.signnow.com"); // about:blank inherits
  const why = Object.fromEntries(r.skipped.map((s) => [s.frameId, s.reason]));
  assert.deepEqual(why, { 4: "no_host_permission", 5: "other_site", 6: "parent_skipped", 8: "opaque_origin", 9: "denied", 10: "load_error" });
  // Skipped rows carry origin and a query-free URL (tokens live in queries).
  assert.ok(!JSON.stringify(r).includes("secret") || r.allowed.some((f) => f.url.includes("secret")));
  assert.equal(r.skipped.find((s) => s.frameId === 5).url, "https://www.google.com/recaptcha/api2/anchor");
  const granted = await classifyFrames(rows, { tabOrigin: "https://app.signnow.com", policy: POLICY, hasPermission: yes });
  assert.ok(granted.allowed.some((f) => f.frameId === 4));
  assert.ok(!granted.allowed.some((f) => f.frameId === 5), "host access never makes another site readable");
});

test("classifyFrames: caps the number of frames read", async () => {
  const rows = [{ frameId: 0, parentFrameId: -1, url: `${TOP}/a` }];
  for (let i = 1; i <= 40; i++) rows.push({ frameId: i, parentFrameId: 0, url: `${TOP}/f${i}` });
  const r = await classifyFrames(rows, { tabOrigin: TOP, policy: POLICY, hasPermission: yes, max: 30 });
  assert.equal(r.allowed.length, 30);
  assert.equal(r.skipped.filter((s) => s.reason === "too_many_frames").length, 11);
});

const VP = { w: 1200, h: 900 };
const diagTop = (iframes = [], regions = []) => ({ viewport: VP, iframes, regions });

test("frameGeometry: ids, then URL, then order; unmatched iframes are reported not_loaded", () => {
  const rows = [{ frameId: 0, parentFrameId: -1, url: `${TOP}/` }, { frameId: 5, parentFrameId: 0, url: `${TOP}/inner.html` },
    { frameId: 6, parentFrameId: 0, url: "http://127.0.0.1:9999/x" }];
  const reports = new Map([[0, diagTop([
    { frameId: null, url: "http://127.0.0.1:9999/x", rect: { x: 0, y: 800, w: 300, h: 100 } },
    { frameId: null, url: `${TOP}/inner.html`, rect: { x: 0, y: 60, w: 1200, h: 700 } },
    { frameId: null, url: "about:blank", rect: { x: 10, y: 10, w: 20, h: 20 } }])]]);
  const { geo, unmatched } = frameGeometry(reports, rows);
  assert.deepEqual(geo.get(5).rect, { x: 0, y: 60, w: 1200, h: 700 });
  assert.equal(geo.get(6).visible.h, 100);
  assert.equal(unmatched.length, 1);
  assert.equal(unmatched[0].reason, "not_loaded");
});

test("pickScrollTarget: a big iframe whose document scrolls beats the top window; small ones don't", () => {
  const geo = new Map([[0, { rect: { x: 0, y: 0, ...VP }, visible: { x: 0, y: 0, ...VP } }],
    [5, { rect: { x: 0, y: 60, w: 1200, h: 800 }, visible: { x: 0, y: 60, w: 1200, h: 800 } }]]);
  const top = (scrolls, best = null) => ({ ok: true, viewport: VP, doc: { scrollHeight: scrolls ? 5000 : 900, clientHeight: 900, scrolls }, best });
  const inner = (scrolls, best = null) => ({ ok: true, viewport: { w: 1200, h: 800 }, doc: { scrollHeight: scrolls ? 9000 : 800, clientHeight: 800, scrolls }, best });
  // signNow-like: the window doesn't scroll, the viewer iframe's document does.
  assert.deepEqual(pickScrollTarget(new Map([[0, top(false)], [5, inner(true)]]), geo), { frameId: 5, path: "document", area: 960000, kind: "document" });
  // An inner element scroller in the iframe.
  const p = pickScrollTarget(new Map([[0, top(true)], [5, inner(false, { path: [[1, 2]], area: 700000 })]]), geo);
  assert.deepEqual([p.frameId, p.path], [5, [[1, 2]]]);
  // A small iframe scroller loses to a scrolling window.
  const small = new Map([[0, geo.get(0)], [5, { rect: { x: 0, y: 0, w: 200, h: 100 }, visible: { x: 0, y: 0, w: 200, h: 100 } }]]);
  assert.deepEqual(pickScrollTarget(new Map([[0, top(true)], [5, inner(true)]]), small), { frameId: 0, path: "document", area: VP.w * VP.h, kind: "document" });
  // ...but wins when nothing else scrolls.
  assert.equal(pickScrollTarget(new Map([[0, top(false)], [5, inner(true)]]), small).frameId, 5);
  // Top-frame element scroller >= 30% still wins (0.10.2 behavior).
  assert.deepEqual(pickScrollTarget(new Map([[0, top(true, { path: [[0, 1]], area: 600000 })]]), geo).path, [[0, 1]]);
});

test("mergeRead: top first, frame headers, prefixed refs, global truncation", () => {
  const geo = new Map([[5, { rect: { x: 0, y: 60, w: 1200, h: 800 }, visible: null }]]);
  const m = mergeRead([
    { frameId: 0, origin: TOP, url: `${TOP}/`, result: { url: `${TOP}/`, content: "Header", elements: [{ ref: "e1", tag: "a" }] } },
    { frameId: 5, origin: TOP, url: `${TOP}/inner.html`, result: { url: `${TOP}/inner.html?x=1`, content: "Page 1 text", elements: [{ ref: "e1", tag: "button" }] } },
  ], { as: "text", maxChars: 1000, geo });
  assert.equal(m.content, `Header\n\n[frame 5: ${TOP}/inner.html 1200x800]\nPage 1 text`);
  assert.deepEqual(m.elements.map((e) => [e.ref, e.frame]), [["e1", undefined], ["f5:e1", 5]]);
  assert.equal(m.frames[1].offset, m.content.indexOf("Page 1 text"));
  assert.deepEqual(m.frames[1].rect, { x: 0, y: 60, w: 1200, h: 800 });
  const md = mergeRead([{ frameId: 0, origin: TOP, result: { content: "" } }, { frameId: 5, origin: TOP, result: { content: "x" } }], { as: "md", maxChars: 5, geo });
  assert.ok(md.truncated);
  assert.equal(md.content.length, 5);
  assert.deepEqual(parseFrameRef("f12:e3"), { frameId: 12, ref: "e3" });
  assert.deepEqual(parseFrameRef("e3"), { frameId: 0, ref: "e3" });
  assert.equal(parseFrameRef("x"), null);
});

test("skippedList: frames with their box, regions offset into top coordinates", () => {
  const geo = new Map([[5, { rect: { x: 0, y: 60, w: 1200, h: 800 }, visible: null }], [6, { rect: { x: 0, y: 800, w: 300, h: 100 } }]]);
  const out = skippedList({ frameSkips: [{ kind: "iframe", frameId: 6, origin: "https://x.com", url: "https://x.com/", reason: "other_site" }],
    reports: new Map([[5, { regions: [{ kind: "canvas", rect: { x: 10, y: 10, w: 500, h: 500 }, reason: "canvas_pixels" }] }]]), geo });
  assert.deepEqual(out[0].rect, { x: 0, y: 800, w: 300, h: 100 });
  assert.deepEqual(out[1], { kind: "canvas", rect: { x: 10, y: 70, w: 500, h: 500 }, reason: "canvas_pixels", frameId: 5 });
});

// ---- the service worker
const FRAMES = [
  { frameId: 0, parentFrameId: -1, url: `${TOP}/form.html` },
  { frameId: 5, parentFrameId: 0, url: `${TOP}/inner.html` },
  { frameId: 6, parentFrameId: 0, url: "http://localhost:9999/other.html" },
];
const topDiag = diagTop([{ frameId: null, url: `${TOP}/inner.html`, origin: TOP, rect: { x: 0, y: 60, w: 1200, h: 800 } },
  { frameId: null, url: "http://localhost:9999/other.html", origin: "http://localhost:9999", rect: { x: 0, y: 860, w: 300, h: 40 } }]);

// frame 5 is a tall same-origin viewer whose document scrolls; the top window doesn't.
function framePage(over = {}) {
  return (tabId, spec, name, frameId) => {
    if (over[name]) { const r = over[name](spec, frameId); if (r !== undefined) return r; }
    if (name === "extractPage") {
      return frameId === 0
        ? { ok: true, url: `${TOP}/form.html`, title: "Form", content: "Top bar", chars: 7, truncated: false, ...(spec.elements ? { elements: [{ ref: "e1", tag: "a", name: "Home" }] } : {}), diag: topDiag }
        : { ok: true, url: `${TOP}/inner.html`, title: "Inner", content: "Page 1 of 9", chars: 11, truncated: false, ...(spec.elements ? { elements: [{ ref: "e1", tag: "button", name: "Next" }] } : {}),
          diag: { viewport: { w: 1200, h: 800 }, iframes: [], regions: [] } };
    }
    if (name === "pageScroll") {
      if (spec.survey) {
        return frameId === 0
          ? { ok: true, url: `${TOP}/form.html`, viewport: VP, doc: { scrollHeight: 900, clientHeight: 900, scrolls: false }, best: null, diag: topDiag }
          : { ok: true, url: `${TOP}/inner.html`, viewport: { w: 1200, h: 800 }, doc: { scrollHeight: 9000, clientHeight: 800, scrolls: true }, best: null, diag: { viewport: { w: 1200, h: 800 }, iframes: [], regions: [] } };
      }
      if (spec.locate != null) return { ok: true, url: frameId ? `${TOP}/inner.html` : `${TOP}/form.html`, matches: 0, shown: 0, scrollable: [], first: null };
      const inner = frameId === 5;
      return { ok: true, url: inner ? `${TOP}/inner.html` : `${TOP}/form.html`, title: "x",
        target: { description: inner ? "document (window scroll)" : "document (window scroll)", kind: "document", auto: false, inShadow: false, path: "document" },
        scrollTop: 0, scrollLeft: 0, scrollHeight: inner ? 9000 : 900, clientHeight: inner ? 800 : 900, atTop: true, atEnd: !inner, moved: 0,
        devicePixelRatio: 2, scrolls: inner, diag: inner ? { viewport: { w: 1200, h: 800 }, iframes: [], regions: [] } : topDiag };
    }
    if (name === "pageSnapshot") {
      return { ok: true, title: "Form", elements: frameId === 0 ? [{ id: "e1", tag: "a", role: "link", name: "Home" }] : [{ id: "e1", tag: "button", role: "button", name: "Next page" }] };
    }
    return { ok: true };
  };
}

async function framed(over) {
  const h = await loadBackground({ page: framePage(over) });
  h.frames.set(1, FRAMES);
  await h.share(1, "read");
  return h;
}

test("read: merges the same-origin iframe, tags its elements, skips the other site with origin, box and reason", async () => {
  const h = await framed();
  const r = await h.call("tabs.read", { tab: 1, elements: true }, { confirm: false });
  assert.equal(r.ok, true, JSON.stringify(r));
  assert.equal(r.result.content, `Top bar\n\n[frame 5: ${TOP}/inner.html 1200x800]\nPage 1 of 9`);
  assert.deepEqual(r.result.elements.map((e) => e.ref), ["e1", "f5:e1"]);
  assert.deepEqual(r.result.frames.map((f) => f.frameId), [0, 5]);
  assert.deepEqual(r.result.skipped, [{ kind: "iframe", frameId: 6, parentFrameId: 0, origin: "http://localhost:9999",
    url: "http://localhost:9999/other.html", reason: "other_site", rect: { x: 0, y: 860, w: 300, h: 40 } }]);
  assert.ok(!("diag" in r.result));
  // No script ever ran in the skipped frame.
  assert.deepEqual([...new Set(h.injected.map((i) => i.frameId))].sort(), [0, 5]);
});

test("read: one frame -> one injection and no frames/skipped fields (0.10.2 shape)", async () => {
  const h = await loadBackground({ page: framePage({ extractPage: (spec) => ({ ok: true, url: `${TOP}/form.html`, title: "Form",
    content: "Top bar", chars: 7, truncated: false, diag: diagTop() }) }) });
  await h.share(1, "read");
  const r = await h.call("tabs.read", { tab: 1 }, { confirm: false });
  assert.equal(r.result.content, "Top bar");
  assert.equal(h.injected.length, 1);
  assert.ok(!("frames" in r.result) && !("skipped" in r.result));
});

test("read: a frame whose injection fails is reported frame_unreachable; the read still succeeds", async () => {
  const h = await framed();
  h.frameThrows.set(5, "Frame with ID 5 was removed.");
  const r = await h.call("tabs.read", { tab: 1 }, { confirm: false });
  assert.equal(r.result.content, "Top bar");
  const s = r.result.skipped.find((x) => x.frameId === 5);
  assert.equal(s.reason, "frame_unreachable");
  assert.match(s.detail, /removed/);
});

test("scroll auto-detect: the big iframe's scrolling document wins; later calls pass frameId", async () => {
  const h = await framed();
  const r = await h.call("page.scroll", { tab: 1, measure: true }, { confirm: false });
  assert.equal(r.ok, true, JSON.stringify(r));
  assert.equal(r.result.target.frameId, 5);
  assert.equal(r.result.target.auto, true);
  assert.equal(r.result.target.path, "document");
  assert.equal(r.result.scrollHeight, 9000);
  assert.match(r.result.target.description, /in iframe http:\/\/127\.0\.0\.1:8765\/inner\.html/);
  assert.deepEqual(r.result.skipped.map((s) => s.reason), ["other_site"]);
  const last = h.injected.at(-1);
  assert.equal(last.frameId, 5);
  assert.deepEqual(last.spec, { measure: true, settleMs: 0, path: "document" });
  h.injected.length = 0;
  const r2 = await h.call("page.scroll", { tab: 1, to: "top", path: "document", frameId: 5 }, { confirm: false });
  assert.equal(r2.ok, true, JSON.stringify(r2));
  assert.deepEqual(h.injected.map((i) => i.frameId), [5]);
});

test("scroll: a frameId that's skipped or gone -> script_error reason frame_unreachable", async () => {
  const h = await framed();
  const r = await h.call("page.scroll", { tab: 1, to: "top", path: "document", frameId: 6 }, { confirm: false });
  assert.equal(r.error.code, "script_error");
  assert.equal(r.error.reason, "frame_unreachable");
  assert.match(r.error.message, /other_site/);
  const g = await h.call("page.scroll", { tab: 1, to: "top", path: "document", frameId: 77 }, { confirm: false });
  assert.equal(g.error.reason, "frame_unreachable");
  assert.equal((await h.call("page.scroll", { tab: 1, measure: true, frameId: -1 }, { confirm: false })).error.code, "bad_request");
});

test("scroll --container across frames: no_match / multiple_matches (count) / not_scrollable (sizes) / the one match", async () => {
  let mode = "none";
  const h = await framed({ pageScroll: (spec, frameId) => {
    if (spec.locate == null) return undefined;
    const url = frameId ? `${TOP}/inner.html` : `${TOP}/form.html`;
    const sc = (d) => ({ path: [[0, 1]], description: d, area: 500000, scrollHeight: 5000, clientHeight: 700 });
    if (mode === "none") return { ok: true, url, matches: 0, shown: 0, scrollable: [], first: null };
    if (mode === "many") return { ok: true, url, matches: 1, shown: 1, scrollable: [sc(`div.f${frameId}`)], first: sc("x") };
    if (mode === "flat") return { ok: true, url, matches: frameId ? 1 : 0, shown: frameId ? 1 : 0, scrollable: [],
      first: frameId ? { path: [[0]], description: "main#app", area: 1, scrollHeight: 1042, clientHeight: 1042 } : null };
    return { ok: true, url, matches: frameId ? 1 : 0, shown: frameId ? 1 : 0, scrollable: frameId ? [sc("div.viewer")] : [], first: null };
  } });
  const call = () => h.call("page.scroll", { tab: 1, by: "page", container: ".viewer" }, { confirm: false });
  let r = await call();
  assert.deepEqual([r.error.code, r.error.reason, r.error.matches], ["element_not_found", "no_match", 0]);
  mode = "many";
  r = await call();
  assert.deepEqual([r.error.code, r.error.reason, r.error.count], ["ambiguous_target", "multiple_matches", 2]);
  assert.deepEqual(r.error.candidates.map((c) => c.frameId), [0, 5]);
  mode = "flat";
  r = await call();
  assert.deepEqual([r.error.code, r.error.reason, r.error.scrollHeight, r.error.clientHeight], ["script_error", "not_scrollable", 1042, 1042]);
  assert.match(r.error.message, /main#app.*doesn't scroll/);
  mode = "one";
  h.injected.length = 0;
  r = await call();
  assert.equal(r.ok, true, JSON.stringify(r));
  assert.equal(r.result.target.frameId, 5);
  const last = h.injected.at(-1);
  assert.deepEqual([last.frameId, last.spec.path, last.spec.container], [5, [[0, 1]], undefined]);
});

test("scroll (one frame): the page's error code, reason and message reach the CLI (0.10.2 sent a bare script_error)", async () => {
  const h = await loadBackground({ page: (tab, spec, name) => (name === "pageScroll"
    ? { ok: false, code: "element_not_found", reason: "no_match", message: "No element matches --container.", matches: 0, diag: topDiag } : { ok: true }) });
  await h.share(1, "read");
  const r = await h.call("page.scroll", { tab: 1, by: "page", container: "[class*=scroll]" }, { confirm: false });
  assert.equal(r.error.code, "element_not_found");
  assert.equal(r.error.reason, "no_match");
  assert.equal(r.error.message, "No element matches --container.");
  assert.ok(!("diag" in r.error));
});

test("page actions: the page's error code and message survive (no bare script_error)", async () => {
  const h = await loadBackground({ page: (tab, spec, name) => (name === "pageAction"
    ? { ok: false, code: "element_not_found", reason: "no_match", message: "No element matches #nope." } : { ok: true }) });
  await h.share(1, "act");
  const r = await h.call("page.click", { tab: 1, target: { selector: "#nope" }, expect: { origin: TOP } });
  assert.equal(r.error.code, "element_not_found");
  assert.equal(r.error.message, "No element matches #nope.");
  assert.equal(r.error.reason, "no_match");
});

test("find merges frames (refs f<id>:eN); a frame ref given to an action -> bad_target reason frame_ref", async () => {
  const h = await framed();
  const f = await h.call("page.find", { tab: 1, intent: "next page button", jev: "off" }, { confirm: false });
  assert.equal(f.ok, true, JSON.stringify(f));
  assert.equal(f.result.results[0].ref, "f5:e1");
  assert.equal(f.result.results[0].frame, 5);
  assert.deepEqual(f.result.skipped.map((s) => s.reason), ["other_site"]);
  await h.share(1, "act");
  h.frames.set(1, FRAMES);
  const c = await h.call("page.click", { tab: 1, target: { ref: "f5:e1" }, expect: { origin: TOP } });
  assert.deepEqual([c.error.code, c.error.reason, c.error.frameId], ["bad_target", "frame_ref", 5]);
  assert.ok(!h.injected.some((i) => i.name === "pageAction"));
  // A find pick in a frame is refused the same way.
  const p = await h.call("page.click", { tab: 1, target: { find: "next page button" }, expect: { origin: TOP }, jev: "off" });
  assert.deepEqual([p.error.code, p.error.reason], ["bad_target", "frame_ref"]);
});

test("wait --selector finds a match inside a readable iframe", async () => {
  const h = await framed({ pageProbe: (spec, frameId) => ({ ok: true, found: frameId === 5 }) });
  const r = await h.call("page.wait", { tab: 1, selector: ".page", timeoutMs: 500 }, { confirm: false });
  assert.equal(r.ok, true, JSON.stringify(r));
  assert.ok(!h.injected.some((i) => i.frameId === 6));
});
