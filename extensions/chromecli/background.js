// chromecli service worker: native port to chromecli-host, op dispatch (P1: read-only), shares, Pause, origin
// policy with pending-accept for loosening, badges, and a 200-entry audit ring buffer for the popup.
// The extension is the authority: the host and CLI are untrusted callers.
import {
  DEFAULT_POLICY, acceptPending, applyIncoming, denied, originOf, pendingAffects, permissionPattern, rejectPending,
  validPolicy,
} from "./policy.js";
import { badgeFor, levelAllows, revokeReason } from "./shares.js";
import { extractPage } from "./page/extract.js";

const HOST = "com.truefrontier.chromecli";
const VERSION = chrome.runtime.getManifest().version;
const AUDIT_MAX = 200;
const OPS = new Set(["status", "tabs.list", "tabs.show", "tabs.read", "tabs.screenshot", "history.search"]);

let port = null;
let backoff = 1000;
let reconnectTimer = null;
let hostStatus = { connected: false, lastError: null, since: null };

class OpError extends Error {
  constructor(code, message, extra = {}) { super(message); this.code = code; this.extra = extra; }
}

// ---- serialized storage access
let chain = Promise.resolve();
function withLock(fn) {
  const run = chain.then(fn, fn);
  chain = run.catch(() => {});
  return run;
}
const getShares = async () => (await chrome.storage.session.get("shares")).shares || {};
const setShares = (shares) => chrome.storage.session.set({ shares });
const getPaused = async () => !!(await chrome.storage.local.get("paused")).paused;
async function getPolicyState() {
  return (await chrome.storage.local.get("policyState")).policyState || { active: DEFAULT_POLICY, pending: null };
}
const setPolicyState = (policyState) => chrome.storage.local.set({ policyState });

async function auditAdd(entry) {
  const { audit = [] } = await chrome.storage.session.get("audit");
  audit.push({ at: isoLocal(Date.now()), ...entry });
  await chrome.storage.session.set({ audit: audit.slice(-AUDIT_MAX) });
}

function isoLocal(ms) {
  const d = new Date(ms);
  const off = -d.getTimezoneOffset();
  const p = (n) => String(Math.floor(Math.abs(n))).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}:` +
    `${p(d.getSeconds())}${off >= 0 ? "+" : "-"}${p(off / 60)}:${p(off % 60)}`;
}

// ---- native port
function post(msg) {
  if (!port) return false;
  try { port.postMessage(msg); return true; } catch (e) { hostStatus.lastError = String(e?.message || e); return false; }
}

function schedule() {
  clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(connect, backoff);
  backoff = Math.min(backoff * 2, 30000);
}

function connect() {
  if (port) return;
  clearTimeout(reconnectTimer);
  try {
    port = chrome.runtime.connectNative(HOST);
  } catch (e) {
    port = null;
    hostStatus = { connected: false, lastError: String(e?.message || e), since: null };
    schedule();
    return;
  }
  hostStatus = { connected: true, lastError: null, since: isoLocal(Date.now()) };
  port.onMessage.addListener(onHostMessage);
  port.onDisconnect.addListener(() => {
    const err = chrome.runtime.lastError?.message || hostStatus.lastError || "disconnected";
    port = null;
    hostStatus = { connected: false, lastError: err, since: null };
    schedule();
  });
  sendHello();
}

async function sendHello() {
  const [paused, shares, st] = [await getPaused(), await getShares(), await getPolicyState()];
  post({
    event: "hello", extVersion: VERSION, extensionId: chrome.runtime.id, paused, jevConfigured: false,
    shares: Object.entries(shares).map(([tab, s]) => ({ tab: Number(tab), origin: s.origin, level: s.level })),
    policyHash: st.active.hash, policyPending: !!st.pending,
  });
}

function onHostMessage(msg) {
  backoff = 1000;
  if (!msg || typeof msg !== "object") return;
  if (msg.event === "policy") { onPolicy(msg.policy); return; }
  if (msg.event === "error") { hostStatus.lastError = `${msg.code}: ${msg.message || ""}`; return; }
  if (msg.id && msg.op) handleRequest(msg);
}

async function handleRequest(req) {
  const args = req.args && typeof req.args === "object" ? req.args : {};
  let reply;
  let origin = null;
  try {
    const result = await dispatch(req.op, args);
    origin = result?.origin || null;
    reply = { v: 1, id: req.id, ok: true, result };
  } catch (e) {
    const err = e instanceof OpError ? { code: e.code, message: e.message, ...e.extra }
      : { code: "script_error", message: String(e?.message || e) };
    origin = err.origin || null;
    reply = { v: 1, id: req.id, ok: false, error: err };
  }
  post(reply);
  if (req.op !== "status") {
    await auditAdd({ op: req.op, tab: args.tab ?? (args.active ? "active" : null), origin,
      outcome: reply.ok ? "ok" : reply.error.code });
  }
}

// ---- policy
async function onPolicy(p) {
  if (!validPolicy(p)) { post({ event: "policy_ack", policyHash: p?.hash ?? null, accepted: false, error: "invalid" }); return; }
  await withLock(async () => {
    const st = await getPolicyState();
    if (st.active.hash === p.hash) { post({ event: "policy_ack", policyHash: p.hash, accepted: true, pending: false }); return; }
    if (st.pending && st.pending.policy.hash === p.hash) {
      post({ event: "policy_ack", policyHash: p.hash, accepted: false, pending: true }); return;
    }
    const { state, accepted } = applyIncoming(st, p);
    await setPolicyState(state);
    post({ event: "policy_ack", policyHash: p.hash, accepted, pending: !accepted,
      loosening: state.pending?.loosening || [] });
  });
  await revokeDenied();
  await refreshBadges();
}

async function revokeDenied() {
  const { active } = await getPolicyState();
  const shares = await getShares();
  for (const id of Object.keys(shares)) {
    let tab = null;
    try { tab = await chrome.tabs.get(Number(id)); } catch { /* closed */ }
    const why = tab ? revokeReason(shares[id], tab.url || "", active) : "closed";
    if (why) await revoke(Number(id), why);
  }
}

// ---- shares and badges
async function revoke(tabId, reason) {
  let gone = null;
  await withLock(async () => {
    const shares = await getShares();
    if (shares[tabId]) { gone = shares[tabId]; delete shares[tabId]; await setShares(shares); }
  });
  if (gone) {
    post({ event: "share_changed", tab: tabId, origin: gone.origin, level: null, reason });
    await auditAdd({ op: "unshare", tab: tabId, origin: gone.origin, outcome: reason });
    await updateBadge(tabId);
  }
}

async function updateBadge(tabId) {
  const [shares, paused] = [await getShares(), await getPaused()];
  try {
    if (shares[tabId]) {
      const b = badgeFor(shares[tabId], paused);
      await chrome.action.setBadgeText({ tabId, text: b.text });
      await chrome.action.setBadgeBackgroundColor({ tabId, color: b.color });
    } else {
      // null clears the tab's own text so the global one (II when paused, ! for a pending policy) shows.
      await chrome.action.setBadgeText({ tabId, text: null });
    }
  } catch { /* tab gone */ }
}

async function refreshBadges() {
  const [paused, st] = [await getPaused(), await getPolicyState()];
  const text = paused ? "II" : st.pending ? "!" : "";
  await chrome.action.setBadgeText({ text });
  await chrome.action.setBadgeBackgroundColor({ color: paused ? "#777777" : "#C5221F" });
  await chrome.action.setTitle({ title: paused ? "chromecli: agents paused"
    : st.pending ? "chromecli: a policy change is waiting for you" : "chromecli" });
  const shares = await getShares();
  for (const id of Object.keys(shares)) await updateBadge(Number(id));
}

async function setPaused(paused) {
  await chrome.storage.local.set({ paused: !!paused });
  post({ event: "paused_changed", paused: !!paused });
  await auditAdd({ op: paused ? "pause" : "resume", tab: null, origin: null, outcome: "ok" });
  await refreshBadges();
}

// ---- access checks
async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  return tab || null;
}

async function hasHostPermission(origin) {
  try { return await chrome.permissions.contains({ origins: [permissionPattern(origin)] }); } catch { return false; }
}

// The share level for `tab` ("read" | "act" | "implicit"), or throws not_shared / origin_blocked / policy_pending.
async function access(tab, need = "read") {
  const st = await getPolicyState();
  const url = tab.url || tab.pendingUrl || "";
  const origin = originOf(url);
  const why = denied(url, st.active);
  if (why) {
    if (pendingAffects(st, { url })) throw new OpError("policy_pending", `Tab ${tab.id}'s origin waits on a pending policy change.`, { tab: tab.id, origin });
    throw new OpError("origin_blocked", `Tab ${tab.id} is on a denied origin (${why}).`, { tab: tab.id, origin });
  }
  const shares = await getShares();
  const share = shares[tab.id];
  if (share) {
    const reason = revokeReason(share, url, st.active);
    if (reason) {
      await revoke(tab.id, reason);
    } else if (levelAllows(share.level, need)) {
      return share.level;
    } else {
      throw new OpError("not_act_shared", `Tab ${tab.id} is shared for read only.`, { tab: tab.id, origin });
    }
  }
  if (need === "read" && st.active.implicit_active === "read") {
    const act = await activeTab();
    if (act && act.id === tab.id && origin && await hasHostPermission(origin)) return "implicit";
  }
  if (pendingAffects(st, { feature: "implicit_active" }) || pendingAffects(st, { url })) {
    throw new OpError("policy_pending", `Access to tab ${tab.id} waits on a pending policy change.`, { tab: tab.id, origin });
  }
  throw new OpError("not_shared", `Tab ${tab.id} is not shared.`, { tab: tab.id });
}

async function resolveTab(args) {
  if (args.active) {
    const tab = await activeTab();
    if (!tab) throw new OpError("tab_not_found", "No active tab (no Chrome window?).");
    return tab;
  }
  if (!Number.isInteger(args.tab)) throw new OpError("bad_target", "Give a tab id (integer) or active: true.");
  try { return await chrome.tabs.get(args.tab); } catch { throw new OpError("tab_not_found", `No tab ${args.tab}.`, { tab: args.tab }); }
}

function row(tab, level, focused) {
  const url = tab.url || tab.pendingUrl || "";
  return { id: tab.id, title: tab.title || "", url, origin: originOf(url), share: level,
    active: !!(focused && focused.id === tab.id), windowId: tab.windowId, botOpened: false };
}

// ---- ops
async function dispatch(op, args) {
  if (!OPS.has(op)) throw new OpError("bad_request", `Unknown op ${JSON.stringify(op)}.`);
  if (op === "status") return statusInfo();
  if (await getPaused()) throw new OpError("paused", "Agents are paused in the chromecli popup.");
  switch (op) {
    case "tabs.list": return listTabs(args);
    case "tabs.show": {
      const tab = await resolveTab(args);
      const level = await access(tab);
      const focused = await activeTab();
      return { ...row(tab, level, focused), status: tab.status, pinned: tab.pinned, audible: !!tab.audible,
        incognito: tab.incognito, index: tab.index, lastAccessed: tab.lastAccessed ? isoLocal(tab.lastAccessed) : null };
    }
    case "tabs.read": return readTab(args);
    case "tabs.screenshot": return screenshot(args);
    case "history.search": return historySearch(args);
  }
  throw new OpError("bad_request", `Unknown op ${op}.`);
}

async function statusInfo() {
  const [paused, shares, st] = [await getPaused(), await getShares(), await getPolicyState()];
  return {
    extVersion: VERSION, extensionId: chrome.runtime.id, paused, jevConfigured: false,
    chromeVersion: (navigator.userAgent.match(/Chrome\/([\d.]+)/) || [])[1] || null,
    shares: Object.entries(shares).map(([tab, s]) => ({ tab: Number(tab), origin: s.origin, level: s.level })),
    policy: { hash: st.active.hash, pending: !!st.pending, pendingLoosening: st.pending?.loosening || [],
      implicitActive: st.active.implicit_active, history: st.active.history, deny: st.active.deny,
      readAllow: st.active.read_allow, actAllow: st.active.act_allow },
  };
}

async function listTabs(args) {
  const tabs = await chrome.tabs.query({});
  const focused = await activeTab();
  const rows = [];
  for (const tab of tabs) {
    if (args.active && !(focused && focused.id === tab.id)) continue;
    let level;
    try { level = await access(tab); } catch { continue; }
    rows.push(row(tab, level, focused));
  }
  return rows;
}

async function readTab(args) {
  const tab = await resolveTab(args);
  const level = await access(tab);
  const origin = originOf(tab.url);
  if (!(await hasHostPermission(origin))) {
    throw new OpError("permission_needed", `chromecli has no host access to ${origin}.`, { tab: tab.id, origin });
  }
  const opts = { as: args.as === "md" ? "md" : "text", maxChars: Math.min(Math.max(1, Number(args.maxChars) || 200000), 5000000),
    elements: !!args.elements };
  let res;
  try {
    [res] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, world: "ISOLATED", func: extractPage, args: [opts] });
  } catch (e) {
    const m = String(e?.message || e);
    if (/permission|cannot access|must request/i.test(m)) throw new OpError("permission_needed", m, { tab: tab.id, origin });
    throw new OpError("script_error", m, { tab: tab.id, origin });
  }
  const out = res?.result;
  if (!out) throw new OpError("script_error", "The page returned nothing.", { tab: tab.id, origin });
  // The page may have navigated while we read: re-check before handing anything back.
  const st = await getPolicyState();
  if (originOf(out.url) !== origin || denied(out.url, st.active)) {
    await revoke(tab.id, "origin changed during read");
    throw new OpError("not_shared", `Tab ${tab.id} navigated away during the read.`, { tab: tab.id });
  }
  return { tab: tab.id, origin, share: level, ...out };
}

async function screenshot(args) {
  const tab = await resolveTab(args);
  const level = await access(tab);
  const origin = originOf(tab.url);
  let win = null;
  try { win = await chrome.windows.getLastFocused({ windowTypes: ["normal"] }); } catch { /* none */ }
  if (!tab.active || !win || win.id !== tab.windowId || win.state === "minimized") {
    throw new OpError("not_visible", `Tab ${tab.id} isn't the active tab of the front Chrome window.`, { tab: tab.id, origin });
  }
  const format = args.format === "jpeg" ? "jpeg" : "png";
  const opts = format === "jpeg" ? { format, quality: Math.min(Math.max(1, Number(args.quality) || 80), 100) } : { format };
  let dataUrl;
  try {
    dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, opts);
  } catch (e) {
    const m = String(e?.message || e);
    if (/permission|activeTab|all_urls/i.test(m)) {
      throw new OpError("permission_needed", m, { tab: tab.id, origin,
        hint: "Kevin: click the chromecli icon on that tab (grants activeTab for it), then retry." });
    }
    throw new OpError("script_error", m, { tab: tab.id, origin });
  }
  const after = await chrome.tabs.get(tab.id).catch(() => null);
  if (!after || !after.active || originOf(after.url) !== origin) {
    throw new OpError("not_visible", `Tab ${tab.id} changed during the capture.`, { tab: tab.id, origin });
  }
  return { tab: tab.id, url: tab.url, title: tab.title, origin, share: level, format,
    data: dataUrl.slice(dataUrl.indexOf(",") + 1) };
}

async function historySearch(args) {
  const st = await getPolicyState();
  if (!st.active.history) {
    if (pendingAffects(st, { feature: "history_enabled" })) throw new OpError("policy_pending", "history waits on a pending policy change.");
    throw new OpError("history_disabled", "history is off ([chromecli] history = false).");
  }
  const limit = Math.min(Math.max(1, Number(args.limit) || 50), 1000);
  const items = await chrome.history.search({ text: String(args.query || ""), startTime: Number(args.since) || 0,
    maxResults: Math.min(limit * 4, 5000) });
  return items.filter((i) => i.url && !denied(i.url, st.active)).slice(0, limit).map((i) => ({
    title: i.title || "", url: i.url, lastVisit: i.lastVisitTime ? isoLocal(i.lastVisitTime) : null,
    visitCount: i.visitCount ?? null,
  }));
}

// ---- popup messages (only from this extension's own pages, never from a tab)
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (sender.id !== chrome.runtime.id || sender.tab || !String(sender.url || "").startsWith(chrome.runtime.getURL(""))) return false;
  popupMessage(msg).then(sendResponse, (e) => sendResponse({ ok: false, error: String(e?.message || e) }));
  return true;
});

async function popupMessage(msg) {
  switch (msg?.type) {
    case "state": {
      const [shares, paused, st] = [await getShares(), await getPaused(), await getPolicyState()];
      const { audit = [] } = await chrome.storage.session.get("audit");
      let tab = null;
      try { tab = msg.tabId != null ? await chrome.tabs.get(msg.tabId) : null; } catch { /* gone */ }
      const url = tab?.url || "";
      return { ok: true, tab: tab && { id: tab.id, title: tab.title, url, origin: originOf(url) },
        share: tab ? shares[tab.id] || null : null, deniedReason: tab ? denied(url, st.active) : null,
        paused, pending: st.pending, policyHash: st.active.hash, host: hostStatus,
        shareCount: Object.keys(shares).length, audit: audit.slice(-20).reverse(), version: VERSION };
    }
    case "share": {
      if (!["read", "act"].includes(msg.level)) return { ok: false, error: "bad level" };
      const tab = await chrome.tabs.get(msg.tabId);
      const st = await getPolicyState();
      const origin = originOf(tab.url || "");
      const why = denied(tab.url || "", st.active);
      if (!origin || why) return { ok: false, error: `Can't share: ${why || "not an http(s) page"}` };
      if (msg.origin !== origin) return { ok: false, error: "The tab changed; reopen the popup." };
      if (!(await hasHostPermission(origin))) return { ok: false, error: "Chrome didn't grant access to this site." };
      await withLock(async () => {
        const shares = await getShares();
        shares[tab.id] = { origin, level: msg.level, at: Date.now() };
        await setShares(shares);
      });
      post({ event: "share_changed", tab: tab.id, origin, level: msg.level, reason: "shared" });
      await auditAdd({ op: `share-${msg.level}`, tab: tab.id, origin, outcome: "ok" });
      await updateBadge(tab.id);
      return { ok: true };
    }
    case "unshare": await revoke(msg.tabId, "unshared"); return { ok: true };
    case "unshareAll": {
      const shares = await getShares();
      for (const id of Object.keys(shares)) await revoke(Number(id), "unshare all");
      return { ok: true };
    }
    case "pause": await setPaused(!!msg.paused); return { ok: true };
    case "acceptPolicy":
    case "rejectPolicy": {
      let hash = null;
      await withLock(async () => {
        const st = await getPolicyState();
        if (!st.pending) return;
        hash = st.pending.policy.hash;
        await setPolicyState(msg.type === "acceptPolicy" ? acceptPending(st) : rejectPending(st));
      });
      if (hash) {
        post({ event: "policy_ack", policyHash: hash, accepted: msg.type === "acceptPolicy", pending: false, byKevin: true });
        await auditAdd({ op: msg.type === "acceptPolicy" ? "policy-accept" : "policy-reject", tab: null, origin: null, outcome: hash });
      }
      await revokeDenied();
      await refreshBadges();
      return { ok: true };
    }
    case "reconnect": backoff = 1000; connect(); return { ok: true };
  }
  return { ok: false, error: "unknown message" };
}

// ---- tab events
chrome.tabs.onUpdated.addListener(async (tabId, changeInfo) => {
  if (changeInfo.url) {
    const shares = await getShares();
    if (shares[tabId]) {
      const { active } = await getPolicyState();
      const why = revokeReason(shares[tabId], changeInfo.url, active);
      if (why) await revoke(tabId, why);
    }
  }
  if (changeInfo.url || changeInfo.status) await updateBadge(tabId);
});
chrome.tabs.onRemoved.addListener((tabId) => revoke(tabId, "closed"));
chrome.tabs.onReplaced.addListener((_added, removed) => revoke(removed, "replaced"));
chrome.commands.onCommand.addListener(async (command) => {
  if (command === "pause-agents") await setPaused(!(await getPaused()));
});

// ---- lifecycle: connect at every service-worker start; the open native port keeps the worker alive (Chrome >= 105).
// The alarm re-connects if the worker was stopped anyway.
chrome.runtime.onStartup.addListener(() => { connect(); refreshBadges(); });
chrome.runtime.onInstalled.addListener(() => { connect(); refreshBadges(); });
chrome.alarms.create("chromecli-keepalive", { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener((a) => { if (a.name === "chromecli-keepalive") connect(); });
connect();
