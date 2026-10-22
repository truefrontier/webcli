// 0.10.0 catalog-linter test-drive fixes in the service worker: opened-tab share inheritance, list --all, preview
// probe, wait --expect-*, reload / same-origin link on act tabs, page-script errors.
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadBackground } from "./harness.mjs";

const FORM = "http://127.0.0.1:8765";
const EXPECT = { origin: FORM };
const KEY = "tsk_test_0123456789abcdef";
const els = {
  next: { tag: "a", role: "link", name: "Next", type: "", inForm: false },
  place: { tag: "button", role: "button", name: "Place order", type: "submit", inForm: true, defaultSubmit: true },
  pw: { tag: "input", role: "textbox", name: "Password", type: "password", autocomplete: "current-password", inForm: true, isField: true },
};
const bySel = { "#next": els.next, "#place": els.place, "#pw": els.pw };

function fakePage(opts = {}) {
  return (tabId, spec, name) => {
    if (opts.onInject) { const r = opts.onInject(tabId, spec, name); if (r !== undefined) return r; }
    if (name === "pageProbe") return { ok: true, found: !!opts.found };
    const el = bySel[spec.target?.selector];
    if (!el) return { ok: false, code: "element_not_found", message: "No visible element matches the target." };
    if (spec.mode !== "do") return { ok: true, element: el, fingerprint: `fp:${el.name}` };
    return { ok: true, element: el, dispatched: true, readBack: true };
  };
}

function jevSafe(conf) {
  globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => ({ answers: { target: { choice: "safe", confidence: conf } } }) });
}

// ---- B1: tabs opened by an action on an act-shared tab
test("a click that opens a same-origin tab: the new tab inherits act; openedTabs reports it; logged", async () => {
  let h;
  h = await loadBackground({ page: fakePage({ onInject: (tabId, spec) => {
    if (spec?.mode === "do") setTimeout(() => h.openFromPage(1, `${FORM}/nav.html`), 5);
  } }) });
  await h.share(1, "act");
  const r = await h.call("page.click", { tab: 1, target: { selector: "#next" }, expect: EXPECT }, {});
  assert.equal(r.ok, true, JSON.stringify(r));
  assert.equal(r.result.openedTabs.length, 1);
  const o = r.result.openedTabs[0];
  assert.equal(o.share, "act");
  assert.equal(o.url, `${FORM}/nav.html`);
  assert.ok(h.posted.some((m) => m.event === "share_changed" && m.tab === o.id && m.level === "act" && m.reason === "opened from tab 1"));
  assert.equal((await h.call("tabs.list")).result.find((t) => t.id === o.id).share, "act");
});

test("opened tab on another origin inherits read only; on a denied origin nothing", async () => {
  let h;
  let url = "https://example.com/docs";
  h = await loadBackground({ page: fakePage({ onInject: (tabId, spec) => {
    if (spec?.mode === "do") setTimeout(() => h.openFromPage(1, url, { pending: true }), 5);
  } }) });
  await h.share(1, "act");
  h.granted.add("https://example.com/*");
  let r = await h.call("page.click", { tab: 1, target: { selector: "#next" }, expect: EXPECT });
  assert.equal(r.result.openedTabs[0].share, "read", JSON.stringify(r.result.openedTabs));
  url = "https://bank.chase.com/login";
  h.granted.add("https://bank.chase.com/*");
  r = await h.call("page.click", { tab: 1, target: { selector: "#next" }, expect: EXPECT });
  assert.equal(r.result.openedTabs[0].share, null);
  assert.equal(r.result.openedTabs[0].shareSkipped, "origin_blocked");
});

test("opened tab without host permission: share null, shareSkipped permission_needed", async () => {
  let h;
  h = await loadBackground({ page: fakePage({ onInject: (tabId, spec) => {
    if (spec?.mode === "do") setTimeout(() => h.openFromPage(1, "https://other.example.net/"), 5);
  } }) });
  await h.share(1, "act");
  const r = await h.call("page.click", { tab: 1, target: { selector: "#next" }, expect: EXPECT });
  assert.deepEqual(r.result.openedTabs.map((t) => [t.share, t.shareSkipped]), [[null, "permission_needed"]]);
});

test("tabs opened outside an action window, or from a read-shared tab, inherit nothing", async () => {
  const h = await loadBackground({ page: fakePage() });
  await h.share(1, "act");
  const id = await h.openFromPage(1, `${FORM}/nav.html`);
  assert.equal((await h.call("tabs.list")).result.some((t) => t.id === id), false);
  const h2 = await loadBackground({ page: fakePage() });
  await h2.share(2, "read");
  const id2 = await h2.openFromPage(2, "https://example.com/b");
  assert.equal((await h2.call("tabs.list")).result.some((t) => t.id === id2), false);
});

test("list --all: unshared tabs as metadata (share null), denied origins still hidden", async () => {
  const h = await loadBackground();
  await h.share(1, "read");
  const plain = (await h.call("tabs.list")).result.map((t) => t.id);
  assert.deepEqual(plain, [1]);
  const all = (await h.call("tabs.list", { all: true })).result;
  assert.deepEqual(all.map((t) => [t.id, t.share]), [[1, "read"], [2, null]]);
  assert.ok(!all.some((t) => t.id === 3), "bank.chase.com is denied");
  assert.equal((await h.call("tabs.read", { tab: 2 })).error.code, "not_shared", "metadata only");
});

// ---- B3: preview probe
test("page.probe: found + heuristic gate estimate, read-only (describe only, no confirm, no dispatch)", async () => {
  const h = await loadBackground({ page: fakePage() });
  await h.share(1, "act");
  let r = await h.call("page.probe", { tab: 1, op: "click", target: { selector: "#place" }, expect: EXPECT }, { confirm: false });
  assert.equal(r.ok, true, JSON.stringify(r));
  assert.equal(r.result.found, true);
  assert.deepEqual(r.result.gateEstimate.heuristic, ["submit_button", "submit_word:order"]);
  assert.equal(r.result.gateEstimate.wouldPrompt, true);
  assert.equal(r.result.gateEstimate.grantSkippable, true);
  r = await h.call("page.probe", { tab: 1, op: "click", target: { selector: "#missing" } }, { confirm: false });
  assert.equal(r.result.found, false);
  assert.equal(r.result.wouldFail, "element_not_found");
  r = await h.call("page.probe", { tab: 1, op: "fill", target: { selector: "#pw" } }, { confirm: false });
  assert.equal(r.result.gateEstimate.grantSkippable, false);
  r = await h.call("page.probe", { tab: 1, op: "click", target: { selector: "#next" }, expect: { origin: "https://example.com" } }, { confirm: false });
  assert.equal(r.result.wouldFail, "url_mismatch");
  assert.deepEqual(h.injected.map((i) => i.spec.mode), ["describe", "describe", "describe"]);
  assert.equal(h.windowsCreated.length, 0);
  // with Jev configured a plain click "depends on Jev"
  await h.chrome.storage.local.set({ typesafeApiKey: KEY });
  r = await h.call("page.probe", { tab: 1, op: "click", target: { selector: "#next" } }, { confirm: false });
  assert.equal(r.result.gateEstimate.wouldPrompt, "depends_on_jev");
  assert.match(r.result.gateEstimate.jev, /0\.7/);
  // a read-shared tab: probe says it would fail
  await h.share(1, "read");
  r = await h.call("page.probe", { tab: 1, op: "click", target: { selector: "#next" } }, { confirm: false });
  assert.equal(r.result.wouldFail, "not_act_shared");
});

// ---- B4: wait --expect-*
test("wait honors expect.origin / expect.urlPrefix", async () => {
  const h = await loadBackground({ page: fakePage({ found: true }) });
  await h.share(1, "read");
  assert.equal((await h.call("page.wait", { tab: 1, text: "x", expect: { origin: "https://example.com" } })).error.code, "url_mismatch");
  assert.equal((await h.call("page.wait", { tab: 1, text: "x", expect: { urlPrefix: `${FORM}/nope` } })).error.code, "url_mismatch");
  assert.equal((await h.call("page.wait", { tab: 1, text: "x", expect: EXPECT })).ok, true);
});

// ---- B5: reload and same-origin link click on Kevin's act tab don't prompt
test("act tab: same-page reload (navigate to the same URL) doesn't prompt and keeps the share", async () => {
  const h = await loadBackground({ page: fakePage() });
  await h.share(1, "act");
  const url = h.tabs.get(1).url;
  const r = await h.call("tab.navigate", { tab: 1, url });
  assert.equal(r.ok, true, JSON.stringify(r));
  assert.equal(r.result.gated, false);
  await h.chrome.tabs.onUpdated.fire(1, { url });
  assert.equal((await h.call("tabs.list")).result[0].share, "act");
  assert.equal(h.windowsCreated.length, 0);
});

test("act tab: a same-origin link click (Jev safe 0.7) doesn't prompt and the share survives the navigation", async () => {
  const h = await loadBackground({ page: fakePage() });
  await h.chrome.storage.local.set({ typesafeApiKey: KEY });
  jevSafe(0.7);
  await h.share(1, "act");
  const r = await h.call("page.click", { tab: 1, target: { selector: "#next" }, expect: EXPECT });
  assert.equal(r.ok, true, JSON.stringify(r));
  assert.equal(r.result.gated, false);
  h.tabs.get(1).url = `${FORM}/nav.html`;
  await h.chrome.tabs.onUpdated.fire(1, { url: `${FORM}/nav.html` });
  assert.equal((await h.call("tabs.list")).result[0].share, "act");
  assert.equal(h.windowsCreated.length, 0);
  // and without a Jev key: heuristics only, also no prompt
  const h2 = await loadBackground({ page: fakePage() });
  await h2.share(1, "act");
  assert.equal((await h2.call("page.click", { tab: 1, target: { selector: "#next" }, expect: EXPECT })).result.gated, false);
});

// ---- B2: page-script errors carry the underlying message
test("script_error carries the in-page message; read surfaces a page-side failure", async () => {
  const h = await loadBackground({ page: fakePage({ onInject: (tabId, spec, name) =>
    name === "extractPage" ? { ok: false, code: "script_error", message: "In-page error: TypeError: x is null" } : undefined }) });
  await h.share(1, "act");
  h.granted.add("http://127.0.0.1/*");
  const r = await h.call("tabs.read", { tab: 1 });
  assert.equal(r.error.code, "script_error");
  assert.match(r.error.message, /TypeError: x is null/);
  const h2 = await loadBackground({ page: () => undefined });
  await h2.share(1, "act");
  const r2 = await h2.call("page.click", { tab: 1, target: { selector: "#next" }, expect: EXPECT });
  assert.equal(r2.error.code, "script_error");
  assert.match(r2.error.message, /navigated or reloaded|blocked/);
});
