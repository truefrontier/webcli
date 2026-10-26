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

// page(tabId, spec, func, frameId) -> the injected function's result (a fake page). h.frames: tabId -> the
// webNavigation.getAllFrames rows (default: the top frame only); h.frameThrows: frameIds whose injection throws.
// Like Chrome: a granted match pattern covers a requested one ("*://*.apple.com/*" covers
// "https://developer.apple.com/*" and "https://apple.com/*"; "*" scheme = http and https).
const PAT = /^(\*|https?):\/\/(\*\.)?([^/:]+)\/\*$/;
export function grantedCovers(granted, want) {
  if (granted.has(want)) return true;
  const w = PAT.exec(want);
  if (!w) return false;
  for (const g of granted) {
    const m = PAT.exec(g);
    if (!m) continue;
    const schemeOk = m[1] === "*" ? true : m[1] === w[1];
    // "https://*/*" (host "*") matches every host, like the manifest's optional_host_permissions.
    const hostOk = m[3] === "*" && !m[2] ? true
      : m[2] ? (w[3] === m[3] || w[3].endsWith(`.${m[3]}`)) : (!w[2] && w[3] === m[3]);
    if (schemeOk && hostOk && (m[1] === "*" || w[1] !== "*")) return true;
  }
  return false;
}

export async function loadBackground({ page } = {}) {
  const h = { posted: [], granted: new Set(), windowsCreated: [], windowsRemoved: [], tabsCreated: [], tabsRemoved: [],
    tabsUpdated: [], injected: [], notes: [], page: page || (() => ({ ok: true })), frames: new Map(), frameThrows: new Map(),
    // 0.12.1 recording: tabCapture + a fake offscreen recorder (h.off.* answers; captureError makes getMediaStreamId throw).
    streamIds: [], offscreenOpen: false, offscreenCreated: 0, toOffscreen: [], captureError: null,
    off: { start: null, frame: null, stopDelayMs: 0 } };
  globalThis.__chromecliOpenWaitMs = 40;
  globalThis.__chromecliCaptureGapMs = 0;
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
      connectNative: () => h.port, onMessage: event(), onStartup: event(), onInstalled: event(), lastError: null,
      getContexts: async ({ contextTypes }) => (h.offscreenOpen && contextTypes.includes("OFFSCREEN_DOCUMENT")
        ? [{ contextType: "OFFSCREEN_DOCUMENT", documentUrl: EXT + "offscreen.html" }] : []),
      sendMessage: async (msg) => {
        if (msg?.target !== "offscreen") return undefined;
        if (!h.offscreenOpen) throw new Error("Could not establish connection. Receiving end does not exist.");
        h.toOffscreen.push(structuredClone(msg));
        if (msg.type === "start") return h.off.start ? h.off.start(msg) : { ok: true, mime: "video/webm;codecs=vp9", width: 2880, height: 1800, audio: !!msg.audio };
        if (msg.type === "frame") return h.off.frame ? h.off.frame(msg) : { ok: true, data: "iVBORw0KGgo=", width: 2880, height: 1800, frames: 42, frameAgeMs: 16 };
        if (msg.type === "stop") {
          if (h.off.stopDelayMs) await new Promise((r) => setTimeout(r, h.off.stopDelayMs));
          const done = { type: "rec_done", recId: msg.recId, bytes: h.offBytes?.[msg.recId] || 0, chunks: h.offChunks?.[msg.recId] || 0, frames: 42, mime: "video/webm;codecs=vp9", durationMs: 1000, reason: msg.reason };
          await h.message(done, "offscreen.html");
          return { ok: true, ...done };
        }
        return { ok: true };
      } },
    tabCapture: { getMediaStreamId: async ({ targetTabId }) => {
      if (h.captureError) throw new Error(h.captureError);
      h.streamIds.push(targetTabId); return `stream-${targetTabId}`;
    } },
    offscreen: {
      createDocument: async ({ url, reasons }) => { h.offscreenCreated++; h.offscreenOpen = true; h.offscreenReasons = reasons; h.offscreenUrl = url; },
      closeDocument: async () => { h.offscreenOpen = false; },
    },
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
      onUpdated: event(), onRemoved: event(), onReplaced: event(), onCreated: event(),
    },
    notifications: { create: async (id, opts) => { h.notes.push({ id, ...opts }); return id; } },
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
    permissions: { contains: async ({ origins }) => origins.every((o) => grantedCovers(h.granted, o)), onAdded: event() },
    scripting: {
      executeScript: async ({ target, func, args, world }) => {
        const frameId = target.frameIds ? target.frameIds[0] : 0;
        h.injected.push({ tabId: target.tabId, name: func.name, spec: structuredClone(args?.[0]), world, frameId });
        if (h.frameThrows.has(frameId)) throw new Error(h.frameThrows.get(frameId));
        return [{ frameId, result: await h.page(target.tabId, args?.[0], func.name, frameId) }];
      },
    },
    webNavigation: {
      onBeforeNavigate: event(), onCommitted: event(),
      getAllFrames: async ({ tabId }) => structuredClone(h.frames.get(tabId)
        || [{ frameId: 0, parentFrameId: -1, url: h.tabs.get(tabId)?.url || "", errorOccurred: false }]),
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
  // A tab the page opened (window.open / target=_blank): created with openerTabId, then (optionally) its URL lands.
  h.openFromPage = async (opener, url, { pending = false } = {}) => {
    const t = { id: nextTab++, windowId: 10, active: true, url: pending ? "about:blank" : url, title: "", status: "loading",
      index: 9, openerTabId: opener };
    h.tabs.set(t.id, t);
    await chrome.tabs.onCreated.fire({ ...t });
    if (pending) { t.url = url; await chrome.tabs.onUpdated.fire(t.id, { url }); }
    return t.id;
  };
  // The offscreen recorder hands the worker one chunk (base64).
  h.chunk = (recId, seq, data = "AAAA") => h.message({ type: "rec_chunk", recId, seq, data }, "offscreen.html");
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
