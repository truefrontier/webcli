// 0.16.2: the confirm window must not take a keystroke meant for another app as Allow / Deny. No button has focus,
// keys never pick a button, and the first second ignores every key and click (Escape included).
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { CONFIRM_ARM_MS, answerHow, confirmInput } from "../confirm-view.js";
import { loadBackground } from "./harness.mjs";

test("confirmInput: the first second ignores everything", () => {
  assert.equal(CONFIRM_ARM_MS, 1000);
  for (const sinceMs of [0, 1, 500, 999]) {
    for (const key of ["Enter", " ", "Escape", "a", "Tab"]) assert.equal(confirmInput({ kind: "key", key, sinceMs }), "ignore");
    for (const button of ["allow", "deny"]) assert.equal(confirmInput({ kind: "click", button, detail: 1, sinceMs }), "ignore");
  }
  assert.equal(confirmInput({ kind: "click", button: "deny" }), "ignore", "no time given = not armed");
});

test("confirmInput: afterwards, a pointer click or Escape; never Enter / Space on a button", () => {
  const sinceMs = 1000;
  assert.equal(confirmInput({ kind: "click", button: "allow", detail: 1, sinceMs }), "allow");
  assert.equal(confirmInput({ kind: "click", button: "deny", detail: 2, sinceMs: 5000 }), "deny");
  assert.equal(confirmInput({ kind: "click", button: "allow", detail: 0, sinceMs }), "ignore", "keyboard-activated click");
  assert.equal(confirmInput({ kind: "click", button: "deny", detail: 0, sinceMs }), "ignore");
  assert.equal(confirmInput({ kind: "key", key: "Escape", sinceMs }), "deny");
  for (const key of ["Enter", " ", "y", "n"]) assert.equal(confirmInput({ kind: "key", key, sinceMs }), "ignore");
});

test("answerHow", () => {
  assert.deepEqual(answerHow("click", 1234.6, { keys: 3, clicks: 1 }), { via: "click", afterMs: 1235, ignoredKeys: 3, ignoredClicks: 1 });
  assert.deepEqual(answerHow("escape", -5), { via: "escape", afterMs: 0, ignoredKeys: 0, ignoredClicks: 0 });
});

test("confirm.html: no autofocus, both buttons start disabled", () => {
  const html = readFileSync(new URL("../confirm.html", import.meta.url), "utf8");
  assert.ok(!/autofocus/i.test(html));
  assert.match(html, /<button id="deny"[^>]*\bdisabled\b/);
  assert.match(html, /<button id="allow"[^>]*\bdisabled\b/);
  const js = readFileSync(new URL("../confirm.js", import.meta.url), "utf8");
  assert.ok(!/\.focus\(\)/.test(js), "confirm.js never focuses a button");
});

// ---- the real confirm.js in a small fake DOM, with a clock the test drives
function fakePage() {
  const listeners = {};
  const timers = [];
  let now = 0;
  const node = (id) => {
    const n = { id, textContent: "", hidden: false, disabled: false, title: "", style: {}, dataset: {}, className: "",
      classList: { add() {}, toggle() {} }, replaceChildren() {}, append() {}, setAttribute() {}, blur() { blurred.push(id); },
      _l: {}, addEventListener(t, f) { (n._l[t] ||= []).push(f); }, onclick: null };
    return n;
  };
  const blurred = [];
  const nodes = {};
  const sent = [];
  let closed = false;
  const g = globalThis;
  const saved = {};
  const set = (k, v) => { saved[k] = Object.getOwnPropertyDescriptor(g, k); Object.defineProperty(g, k, { value: v, configurable: true, writable: true }); };
  const doc = {
    getElementById: (id) => (nodes[id] ||= node(id)),
    querySelector: (sel) => (nodes[sel] ||= node(sel)),
    createElement: () => node("x"), createElementNS: () => node("x"), createTextNode: () => ({}),
    body: { classList: { add(c) { doc.bodyClasses.push(c); } } }, bodyClasses: [],
    get activeElement() { return nodes.deny; }, title: "",
  };
  doc.getElementById("deny"); doc.getElementById("allow");
  nodes.deny.disabled = true; nodes.allow.disabled = true; // as in confirm.html
  set("document", doc);
  set("location", { search: "?id=abc" });
  set("performance", { now: () => now });
  set("setTimeout", (f, ms = 0) => { timers.push({ at: now + ms, f }); return timers.length; });
  set("window", { close() { closed = true; }, addEventListener(t, f) { (listeners[t] ||= []).push(f); } });
  set("chrome", { runtime: { sendMessage: async (m) => {
    sent.push(m);
    if (m.type === "confirmGet") return { ok: true, info: { op: "navigate", origin: "https://example.org", url: "https://example.org/x", reasons: [] }, expiresAt: Date.now() + 60000, queued: 0 };
    return { ok: true };
  } } });
  const flush = () => new Promise((r) => setImmediate(r));
  const page = {
    nodes, sent, blurred, doc,
    get closed() { return closed; },
    async advance(ms) {
      now += ms;
      for (const t of timers.splice(0).sort((a, b) => a.at - b.at)) { if (t.at <= now) t.f(); else timers.push(t); }
      await flush();
    },
    key(key) {
      const e = { key, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, stopPropagation() {} };
      for (const f of listeners.keydown || []) f(e);
      // a key that wasn't prevented on a focused, enabled button would click it (detail 0)
      return e;
    },
    async click(id, detail = 1) {
      const pd = { defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, stopPropagation() {} };
      for (const f of listeners.pointerdown || []) f(pd);
      if (!nodes[id].disabled && !pd.defaultPrevented) for (const f of nodes[id]._l.click || []) f({ detail });
      await flush();
    },
    answers: () => sent.filter((m) => m.type === "confirmAnswer"),
    restore() { for (const [k, d] of Object.entries(saved)) { if (d) Object.defineProperty(g, k, d); else delete g[k]; } },
  };
  return page;
}
let n = 0;
async function openConfirm() {
  const page = fakePage();
  await import(`../confirm.js?case=${++n}`);
  await page.advance(0);
  return page;
}

test("confirm.js: opens with nothing focused and both buttons disabled for a second", async () => {
  const page = await openConfirm();
  try {
    assert.ok(page.blurred.includes("deny"), "focus is dropped");
    assert.equal(page.nodes.deny.disabled, true);
    assert.equal(page.nodes.allow.disabled, true);
    await page.advance(999);
    assert.equal(page.nodes.deny.disabled, true);
    await page.advance(1);
    assert.equal(page.nodes.deny.disabled, false);
    assert.equal(page.nodes.allow.disabled, false);
    assert.ok(page.doc.bodyClasses.includes("armed"));
  } finally { page.restore(); }
});

test("confirm.js: typing (Enter, Space, Escape, letters) and clicks in the first second do nothing", async () => {
  const page = await openConfirm();
  try {
    for (const k of ["h", "i", " ", "Enter", "Escape"]) assert.equal(page.key(k).defaultPrevented, true, k);
    await page.click("deny"); await page.click("allow");
    await page.advance(990);
    assert.equal(page.answers().length, 0);
    assert.equal(page.closed, false);
    // still open after the second; Enter and Space stay inert, a keyboard "click" (detail 0) too
    await page.advance(20);
    assert.equal(page.key("Enter").defaultPrevented, true);
    assert.equal(page.key(" ").defaultPrevented, true);
    await page.click("deny", 0);
    assert.equal(page.answers().length, 0);
    // a deliberate click counts, and says how
    await page.click("deny");
    const [a] = page.answers();
    assert.equal(a.answer, "deny");
    assert.equal(a.how.via, "click");
    assert.ok(a.how.afterMs >= 1000);
    assert.equal(a.how.ignoredKeys, 5);
    assert.equal(a.how.ignoredClicks, 2);
    assert.equal(page.closed, true);
  } finally { page.restore(); }
});

test("confirm.js: Escape denies only after the first second; Allow by click", async () => {
  let page = await openConfirm();
  try {
    await page.advance(1000);
    page.key("Escape");
    await page.advance(0);
    assert.deepEqual(page.answers().map((a) => [a.answer, a.how.via]), [["deny", "escape"]]);
  } finally { page.restore(); }
  page = await openConfirm();
  try {
    await page.advance(1200);
    await page.click("allow");
    assert.deepEqual(page.answers().map((a) => [a.answer, a.how.via, a.how.ignoredKeys]), [["allow", "click", 0]]);
  } finally { page.restore(); }
});

// ---- the worker: how the answer came is in the declined error (and so in the host log)
test("declined says how: click with timing, window closed, or a malformed report", async () => {
  const h = await loadBackground();
  let id = h.start("tab.open", { url: "https://example.org/" });
  let cid = await h.confirmWindow(1);
  await h.message({ type: "confirmAnswer", id: cid, answer: "deny", how: { via: "click", afterMs: 4210, ignoredKeys: 2, ignoredClicks: 0 } },
    `confirm.html?id=${cid}`, { id: 999 });
  let err = (await h.reply(id)).error;
  assert.equal(err.code, "declined");
  assert.deepEqual([err.answeredBy, err.answeredAfterMs, err.ignoredKeys, err.ignoredClicks], ["click", 4210, 2, 0]);

  id = h.start("tab.open", { url: "https://example.org/" });
  await h.confirmWindow(2);
  const win = h.windowsCreated.filter((w) => w.type === "popup")[1];
  await h.chrome.windows.onRemoved.fire(win.id);
  err = (await h.reply(id)).error;
  assert.equal(err.answeredBy, "window_closed");
  assert.equal(err.answeredAfterMs, undefined);

  id = h.start("tab.open", { url: "https://example.org/" });
  cid = await h.confirmWindow(3);
  await h.message({ type: "confirmAnswer", id: cid, answer: "deny", how: { via: "<b>", afterMs: "x" } }, `confirm.html?id=${cid}`, { id: 999 });
  err = (await h.reply(id)).error;
  assert.equal(err.answeredBy, "page");
  assert.equal(err.answeredAfterMs, undefined);
});
