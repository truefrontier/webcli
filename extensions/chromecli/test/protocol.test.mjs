// The background dispatcher with a mocked chrome.* and native port: P1 ops, shares, pause, deny, and revoke.
import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";

function event() {
  const fns = [];
  return { addListener: (f) => fns.push(f), fire: (...a) => Promise.all(fns.map((f) => f(...a))), fns };
}

function area() {
  let data = {};
  return {
    get: async (k) => (typeof k === "string" ? (k in data ? { [k]: structuredClone(data[k]) } : {}) : structuredClone(data)),
    set: async (o) => { data = { ...data, ...structuredClone(o) }; },
    _data: () => data,
  };
}

let posted, port, tabs, chromeMock, granted;

function makeChrome() {
  posted = [];
  granted = new Set();
  port = { onMessage: event(), onDisconnect: event(), postMessage: (m) => posted.push(structuredClone(m)) };
  tabs = new Map([
    [1, { id: 1, windowId: 10, active: true, url: "https://example.com/a", title: "Example", status: "complete", index: 0 }],
    [2, { id: 2, windowId: 10, active: false, url: "https://bank.chase.com/", title: "Bank", status: "complete", index: 1 }],
    [3, { id: 3, windowId: 10, active: false, url: "https://news.example.org/", title: "News", status: "complete", index: 2 }],
  ]);
  const ext = "chrome-extension://ileggcomhbnofnapippaanhimdhchhci/";
  return {
    runtime: {
      id: "ileggcomhbnofnapippaanhimdhchhci", getManifest: () => ({ version: "0.7.0" }), getURL: (p) => ext + p,
      connectNative: () => port, onMessage: event(), onStartup: event(), onInstalled: event(), lastError: null,
    },
    storage: { session: area(), local: area() },
    tabs: {
      query: async (q) => [...tabs.values()].filter((t) => !q.active || t.active),
      get: async (id) => { if (!tabs.has(id)) throw new Error("No tab"); return { ...tabs.get(id) }; },
      captureVisibleTab: async () => "data:image/png;base64,iVBORw0KGgo=",
      onUpdated: event(), onRemoved: event(), onReplaced: event(),
    },
    windows: { getLastFocused: async () => ({ id: 10, state: "normal", focused: true }) },
    permissions: { contains: async ({ origins }) => origins.every((o) => granted.has(o)) },
    scripting: { executeScript: async ({ target }) => [{ result: { title: tabs.get(target.tabId).title,
      url: tabs.get(target.tabId).url, as: "text", content: "hello", chars: 5, truncated: false } }] },
    history: { search: async () => [
      { url: "https://example.com/x", title: "X", lastVisitTime: 1760000000000, visitCount: 2 },
      { url: "https://accounts.google.com/signin", title: "Sign in", lastVisitTime: 1760000000000, visitCount: 1 },
      { url: "chrome://settings", title: "Settings", lastVisitTime: 1760000000000, visitCount: 1 },
    ] },
    action: { setBadgeText: async () => {}, setBadgeBackgroundColor: async () => {}, setTitle: async () => {} },
    commands: { onCommand: event() },
    alarms: { create: () => {}, onAlarm: event() },
  };
}

let n = 0;
async function load() {
  globalThis.chrome = chromeMock = makeChrome();
  globalThis.navigator ??= { userAgent: "Chrome/155.0.0.0" };
  await import(`../background.js?i=${++n}`);
}

async function call(op, args = {}) {
  const id = `r${Math.random()}`;
  await port.onMessage.fire({ v: 1, id, op, args });
  for (let i = 0; i < 50; i++) {
    const r = posted.find((m) => m.id === id);
    if (r) return r;
    await new Promise((res) => setTimeout(res, 2));
  }
  throw new Error(`no reply to ${op}`);
}

const popup = (msg) => new Promise((resolve) => {
  const sender = { id: chromeMock.runtime.id, url: chromeMock.runtime.getURL("popup.html") };
  for (const f of chromeMock.runtime.onMessage.fns) f(msg, sender, resolve);
});

beforeEach(load);

test("hello on connect, with the extension id", async () => {
  await new Promise((r) => setTimeout(r, 5));
  const hello = posted.find((m) => m.event === "hello");
  assert.equal(hello.extensionId, "ileggcomhbnofnapippaanhimdhchhci");
  assert.equal(hello.paused, false);
});

test("unshared tab: read -> not_shared; list is empty", async () => {
  assert.equal((await call("tabs.read", { tab: 1 })).error.code, "not_shared");
  assert.deepEqual((await call("tabs.list")).result, []);
});

test("share-read: list shows it, read works, cross-origin navigation revokes", async () => {
  granted.add("https://example.com/*");
  assert.deepEqual(await popup({ type: "share", tabId: 1, origin: "https://example.com", level: "read" }), { ok: true });
  const list = (await call("tabs.list")).result;
  assert.equal(list.length, 1);
  assert.equal(list[0].share, "read");
  const read = await call("tabs.read", { tab: 1, as: "md" });
  assert.equal(read.ok, true);
  assert.equal(read.result.content, "hello");
  tabs.get(1).url = "https://other.example.net/";
  await chromeMock.tabs.onUpdated.fire(1, { url: "https://other.example.net/" });
  assert.equal((await call("tabs.read", { tab: 1 })).error.code, "not_shared");
  assert.ok(posted.some((m) => m.event === "share_changed" && m.level === null && m.reason === "origin changed"));
});

test("a denied origin can't be shared, and ops on it say origin_blocked", async () => {
  granted.add("https://bank.chase.com/*");
  const res = await popup({ type: "share", tabId: 2, origin: "https://bank.chase.com", level: "read" });
  assert.equal(res.ok, false);
  assert.equal((await call("tabs.read", { tab: 2 })).error.code, "origin_blocked");
});

test("pause: every op but status -> paused", async () => {
  await popup({ type: "pause", paused: true });
  assert.equal((await call("tabs.list")).error.code, "paused");
  assert.equal((await call("history.search", {})).error.code, "paused");
  const st = await call("status");
  assert.equal(st.ok, true);
  assert.equal(st.result.paused, true);
});

test("unshare all", async () => {
  granted.add("https://example.com/*");
  await popup({ type: "share", tabId: 1, origin: "https://example.com", level: "act" });
  await popup({ type: "unshareAll" });
  assert.deepEqual((await call("tabs.list")).result, []);
});

test("popup state lists every shared tab with its title and level", async () => {
  granted.add("https://example.com/*");
  granted.add("https://news.example.org/*");
  await popup({ type: "share", tabId: 1, origin: "https://example.com", level: "read" });
  await popup({ type: "share", tabId: 3, origin: "https://news.example.org", level: "act" });
  const st = await popup({ type: "state", tabId: 1 });
  assert.equal(st.shareCount, 2);
  assert.deepEqual(st.shared.map((s) => [s.tabId, s.origin, s.level, s.title]).sort(),
    [[1, "https://example.com", "read", "Example"], [3, "https://news.example.org", "act", "News"]]);
  assert.equal(st.share.level, "read");
  assert.equal(st.tab.origin, "https://example.com");
});

test("screenshot of a background tab -> not_visible; of the front tab -> data", async () => {
  granted.add("https://example.com/*");
  granted.add("https://news.example.org/*");
  await popup({ type: "share", tabId: 1, origin: "https://example.com", level: "read" });
  await popup({ type: "share", tabId: 3, origin: "https://news.example.org", level: "read" });
  assert.equal((await call("tabs.screenshot", { tab: 3 })).error.code, "not_visible");
  const shot = await call("tabs.screenshot", { tab: 1 });
  assert.equal(shot.ok, true);
  assert.equal(shot.result.data, "iVBORw0KGgo=");
});

test("history filters denied origins", async () => {
  const res = await call("history.search", { limit: 5 });
  assert.deepEqual(res.result.map((r) => r.url), ["https://example.com/x"]);
});

test("unknown and later-phase ops are bad_request", async () => {
  assert.equal((await call("page.click", {})).error.code, "bad_request");
  assert.equal((await call("nope", {})).error.code, "bad_request");
});

test("a loosening policy push is held pending until Kevin accepts", async () => {
  const { DEFAULT_DENY } = await import("../policy.js");
  const policy = { implicit_active: "off", history: true, confirm_timeout: 60, read_allow: [], act_allow: [],
    deny: DEFAULT_DENY.filter((d) => d !== "*.chase.com"), hash: "loose1" };
  await port.onMessage.fire({ event: "policy", policy });
  await new Promise((r) => setTimeout(r, 10));
  const ack = posted.find((m) => m.event === "policy_ack" && m.policyHash === "loose1");
  assert.equal(ack.accepted, false);
  assert.equal((await call("tabs.read", { tab: 2 })).error.code, "policy_pending");
  await popup({ type: "acceptPolicy" });
  assert.equal((await call("tabs.read", { tab: 2 })).error.code, "not_shared");
});

test("messages from a tab (not the popup) are ignored", async () => {
  let answered = false;
  for (const f of chromeMock.runtime.onMessage.fns) {
    const r = f({ type: "share", tabId: 1, level: "act" }, { id: chromeMock.runtime.id, tab: { id: 1 }, url: "https://example.com/" }, () => { answered = true; });
    assert.equal(r, false);
  }
  assert.equal(answered, false);
});
