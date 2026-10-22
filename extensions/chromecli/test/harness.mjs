// A mocked chrome.* + native port for loading background.js under node --test (tab and page actions).
export function event() {
  const fns = [];
  return { addListener: (f) => fns.push(f), fire: (...a) => Promise.all(fns.map((f) => f(...a))), fns };
}

function area() {
  let data = {};
  return {
    // Like chrome.storage: a key or an array of keys returns only those; null/undefined returns everything.
    get: async (k) => {
      if (k == null) return structuredClone(data);
      const keys = typeof k === "string" ? [k] : Array.isArray(k) ? k : Object.keys(k);
      return Object.fromEntries(keys.filter((x) => x in data).map((x) => [x, structuredClone(data[x])]));
    },
    set: async (o) => { data = { ...data, ...structuredClone(o) }; },
    _data: () => data,
  };
}

const EXT_ID = "ileggcomhbnofnapippaanhimdhchhci";
const EXT = `chrome-extension://${EXT_ID}/`;
let n = 0;

// page(tabId, spec, func) -> the injected function's result (a fake page).
export async function loadBackground({ page } = {}) {
  const h = { posted: [], granted: new Set(), windowsCreated: [], windowsRemoved: [], tabsCreated: [], tabsRemoved: [],
    tabsUpdated: [], injected: [], page: page || (() => ({ ok: true })) };
  h.port = { onMessage: event(), onDisconnect: event(), postMessage: (m) => h.posted.push(structuredClone(m)) };
  h.tabs = new Map([
    [1, { id: 1, windowId: 10, active: true, url: "http://127.0.0.1:8765/form.html", title: "Form", status: "complete", index: 0 }],
    [2, { id: 2, windowId: 10, active: false, url: "https://example.com/a", title: "Example", status: "complete", index: 1 }],
    [3, { id: 3, windowId: 10, active: false, url: "https://bank.chase.com/", title: "Bank", status: "complete", index: 2 }],
  ]);
  let nextTab = 100;
  let nextWin = 500;
  const chrome = {
    runtime: { id: EXT_ID, getManifest: () => ({ version: "0.8.0" }), getURL: (p) => EXT + p,
      connectNative: () => h.port, onMessage: event(), onStartup: event(), onInstalled: event(), lastError: null },
    storage: { session: area(), local: area(), onChanged: event() },
    tabs: {
      query: async (q) => [...h.tabs.values()].filter((t) => !q.active || t.active),
      get: async (id) => { if (!h.tabs.has(id)) throw new Error("No tab"); return { ...h.tabs.get(id) }; },
      create: async ({ url, active, windowId }) => {
        const t = { id: nextTab++, windowId: windowId ?? 10, active: !!active, url, title: "", status: "loading", index: 9 };
        h.tabs.set(t.id, t); h.tabsCreated.push({ url, active, windowId }); return { ...t };
      },
      update: async (id, props) => {
        const t = h.tabs.get(id); if (!t) throw new Error("No tab");
        h.tabsUpdated.push({ id, ...props });
        if (props.url) t.url = props.url;
        if (props.active) t.active = true;
        return { ...t };
      },
      remove: async (id) => { h.tabs.delete(id); h.tabsRemoved.push(id); },
      captureVisibleTab: async () => "data:image/png;base64,iVBORw0KGgo=",
      onUpdated: event(), onRemoved: event(), onReplaced: event(),
    },
    windows: {
      getLastFocused: async () => ({ id: 10, state: "normal", focused: true, type: "normal" }),
      create: async (opts) => {
        const w = { id: nextWin++, ...opts };
        if (opts.type !== "popup") {
          const t = { id: nextTab++, windowId: w.id, active: true, url: opts.url, title: "", status: "loading", index: 0 };
          h.tabs.set(t.id, t); w.tabs = [{ ...t }];
        }
        h.windowsCreated.push(w); return w;
      },
      remove: async (id) => { h.windowsRemoved.push(id); },
      update: async () => ({}),
      onRemoved: event(),
    },
    permissions: { contains: async ({ origins }) => origins.every((o) => h.granted.has(o)) },
    scripting: {
      executeScript: async ({ target, func, args, world }) => {
        h.injected.push({ tabId: target.tabId, name: func.name, spec: structuredClone(args?.[0]), world });
        return [{ result: await h.page(target.tabId, args?.[0], func.name) }];
      },
    },
    history: { search: async () => [] },
    action: { setBadgeText: async () => {}, setBadgeBackgroundColor: async () => {}, setTitle: async () => {} },
    commands: { onCommand: event() },
    alarms: { create: () => {}, onAlarm: event() },
  };
  h.chrome = chrome;
  globalThis.chrome = chrome;
  globalThis.navigator ??= { userAgent: "Chrome/155.0.0.0" };
  await import(`../background.js?h=${++n}`);

  h.start = (op, args = {}, { confirm = true, deadlineMs } = {}) => {
    const id = `r${++n}${Math.random()}`;
    h.port.onMessage.fire({ v: 1, id, op, args, confirm, ...(deadlineMs ? { deadlineMs } : {}) });
    return id;
  };
  h.reply = async (id, ms = 2000) => {
    const end = Date.now() + ms;
    while (Date.now() < end) {
      const r = h.posted.find((m) => m.id === id);
      if (r) return r;
      await new Promise((res) => setTimeout(res, 2));
    }
    throw new Error(`no reply to ${id}`);
  };
  h.call = async (op, args, opts) => h.reply(h.start(op, args, opts));
  h.message = (msg, from = "popup.html", tab = undefined) => new Promise((resolve) => {
    const sender = { id: EXT_ID, url: EXT + from, ...(tab ? { tab } : {}) };
    let answered = false;
    for (const f of chrome.runtime.onMessage.fns) {
      const r = f(msg, sender, (v) => { answered = true; resolve(v); });
      if (r === false && !answered) resolve(undefined);
    }
  });
  // Wait for the confirm window; returns its id (from the URL).
  h.confirmWindow = async (count = 1, ms = 2000) => {
    const end = Date.now() + ms;
    while (Date.now() < end) {
      const popups = h.windowsCreated.filter((w) => w.type === "popup");
      if (popups.length >= count) return new URL(popups[count - 1].url).searchParams.get("id");
      await new Promise((res) => setTimeout(res, 2));
    }
    throw new Error("no confirm window");
  };
  h.answer = (id, answer) => h.message({ type: "confirmAnswer", id, answer }, `confirm.html?id=${id}`, { id: 999 });
  h.share = async (tabId, level) => {
    const t = h.tabs.get(tabId);
    const origin = new URL(t.url).origin;
    h.granted.add(`${new URL(t.url).protocol}//${new URL(t.url).hostname}/*`);
    return h.message({ type: "share", tabId, origin, level });
  };
  h.policy = async (p) => {
    const { DEFAULT_DENY } = await import("../policy.js");
    const pol = { implicit_active: "off", history: true, confirm_timeout: 60, read_allow: [], act_allow: [],
      deny: [...DEFAULT_DENY], hash: `p${++n}`, ...p };
    await h.port.onMessage.fire({ event: "policy", policy: pol });
    await new Promise((r) => setTimeout(r, 10));
    await h.message({ type: "acceptPolicy" });
  };
  await new Promise((r) => setTimeout(r, 5));
  return h;
}
