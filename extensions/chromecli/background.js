// chromecli service worker: native port to chromecli-host, op dispatch (read ops, tab actions, page actions), shares,
// bot-opened tabs, Pause, origin policy with pending-accept for loosening, gate heuristics + the in-browser confirm
// window (one at a time, others queue), badges, and a 200-entry audit ring buffer for the popup.
// The extension is the authority: the host and CLI are untrusted callers.
import {
  DEFAULT_POLICY, acceptPending, applyIncoming, denied, matches, originOf, pendingAffects, permissionPattern,
  rejectPending, validPolicy,
} from "./policy.js";
import { displayValue, gateReasons } from "./gate.js";
import { pageAction, pageProbe } from "./page/actions.js";
import { pageSnapshot } from "./page/snapshot.js";
import {
  JevError, LOCATE_MAX, buildSnapshot, callJev, classifyBody, classifyGate, fallbackFor, heuristicPick, heuristicRank, jevConfig,
  jevMode, locateBody, mergeReasons, parseClassify, parseLocate,
} from "./jev.js";
import { badgeFor, levelAllows, revokeReason } from "./shares.js";
import { extractPage } from "./page/extract.js";

const HOST = "com.truefrontier.chromecli";
const VERSION = chrome.runtime.getManifest().version;
const AUDIT_MAX = 200;
const READ_OPS = ["status", "tabs.list", "tabs.show", "tabs.read", "tabs.screenshot", "history.search", "page.wait",
  "page.find"];
const TAB_OPS = ["tab.open", "tab.focus", "tab.close", "tab.navigate"];
const PAGE_OPS = ["page.click", "page.type", "page.fill", "page.select", "page.press", "page.eval"];
const OPS = new Set([...READ_OPS, ...TAB_OPS, ...PAGE_OPS]);
// Ops that change something: the CLI sends them only with --confirm (confirm: true); anything else is refused.
const MUTATING = new Set([...TAB_OPS, ...PAGE_OPS]);

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
const getBotTabs = async () => (await chrome.storage.session.get("botTabs")).botTabs || {};
async function markBot(tabId, on = true) {
  await withLock(async () => {
    const bot = await getBotTabs();
    if (on) bot[tabId] = { at: Date.now() }; else delete bot[tabId];
    await chrome.storage.session.set({ botTabs: bot });
  });
}
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

// Jev settings from chrome.storage.local. The key stays in this worker: only `configured` is ever reported.
async function getJev() {
  return jevConfig(await chrome.storage.local.get(["typesafeApiKey", "jevModel", "jevFloor"]));
}

async function sendHello() {
  const [paused, shares, st, jev] = [await getPaused(), await getShares(), await getPolicyState(), await getJev()];
  post({
    event: "hello", extVersion: VERSION, extensionId: chrome.runtime.id, paused, jevConfigured: jev.configured,
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
    const result = await dispatch(req.op, args, req);
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
    const info = reply.ok ? reply.result : reply.error;
    const extra = info && Array.isArray(info.reasons) && info.reasons.length
      ? { reasons: info.reasons, confirmedInBrowser: !!info.confirmedInBrowser } : {};
    await auditAdd({ op: req.op, tab: args.tab ?? info?.tab ?? (args.active ? "active" : null), origin,
      outcome: reply.ok ? "ok" : reply.error.code, ...extra });
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
  const waiting = typeof pendingConfirms === "function" ? pendingConfirms() : 0;
  const text = paused ? "II" : waiting ? "?" : st.pending ? "!" : "";
  await chrome.action.setBadgeText({ text });
  await chrome.action.setBadgeBackgroundColor({ color: paused ? "#777777" : waiting ? "#E8710A" : "#C5221F" });
  await chrome.action.setTitle({ title: paused ? "chromecli: agents paused"
    : waiting ? "chromecli: an agent action is waiting for your OK"
    : st.pending ? "chromecli: a policy change is waiting for you" : "chromecli" });
  const shares = await getShares();
  for (const id of Object.keys(shares)) await updateBadge(Number(id));
}

async function setPaused(paused) {
  await chrome.storage.local.set({ paused: !!paused });
  if (paused) cancelAllConfirms("paused");
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

function row(tab, level, focused, botOpened = false) {
  const url = tab.url || tab.pendingUrl || "";
  return { id: tab.id, title: tab.title || "", url, origin: originOf(url), share: level,
    active: !!(focused && focused.id === tab.id), windowId: tab.windowId, botOpened: !!botOpened };
}

// ---- ops
async function dispatch(op, args, req = {}) {
  if (!OPS.has(op)) throw new OpError("bad_request", `Unknown op ${JSON.stringify(op)}.`);
  if (op === "status") return statusInfo();
  if (await getPaused()) throw new OpError("paused", "Agents are paused in the chromecli popup.");
  if (MUTATING.has(op) && req.confirm !== true) {
    throw new OpError("bad_request", `${op} changes something; it runs only with confirm: true (chromecli --confirm).`);
  }
  switch (op) {
    case "tabs.list": return listTabs(args);
    case "tabs.show": {
      const tab = await resolveTab(args);
      const bot = (await getBotTabs())[tab.id];
      const level = await access(tab).catch((e) => { if (bot && e.code === "not_shared") return null; throw e; });
      const focused = await activeTab();
      return { ...row(tab, level, focused, !!bot), status: tab.status, pinned: tab.pinned, audible: !!tab.audible,
        incognito: tab.incognito, index: tab.index, lastAccessed: tab.lastAccessed ? isoLocal(tab.lastAccessed) : null };
    }
    case "tabs.read": return readTab(args);
    case "tabs.screenshot": return screenshot(args);
    case "history.search": return historySearch(args);
    case "tab.open": return tabOpen(args, req);
    case "tab.focus": return tabFocus(args);
    case "tab.close": return tabClose(args);
    case "tab.navigate": return tabNavigate(args, req);
    case "page.wait": return pageWait(args);
    case "page.find": return pageFind(args);
    default: return pageOp(op.slice(5), args, req);
  }
  throw new OpError("bad_request", `Unknown op ${op}.`);
}

async function statusInfo() {
  const [paused, shares, st, jev] = [await getPaused(), await getShares(), await getPolicyState(), await getJev()];
  return {
    extVersion: VERSION, extensionId: chrome.runtime.id, paused, jevConfigured: jev.configured, confirmsPending: pendingConfirms(),
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
  const bot = await getBotTabs();
  const st = await getPolicyState();
  const rows = [];
  for (const tab of tabs) {
    if (args.active && !(focused && focused.id === tab.id)) continue;
    let level;
    try {
      level = await access(tab);
    } catch (e) {
      // Bot-opened tabs show as metadata (share null) even when unshared, unless they're on a denied origin.
      if (!(bot[tab.id] && e.code === "not_shared" && !denied(tab.url || tab.pendingUrl || "", st.active))) continue;
      level = null;
    }
    rows.push(row(tab, level, focused, !!bot[tab.id]));
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

// ---- in-browser confirm (spec 6.7b): one window at a time, the rest queue; 60s (confirm_timeout) -> timeout;
// closing the window = Deny; Pause cancels everything waiting.
const confirmQueue = [];
let confirmActive = null;

const pendingConfirms = () => (confirmActive ? 1 : 0) + confirmQueue.length;

function requestConfirm(info, deadline) {
  return new Promise((resolve) => {
    confirmQueue.push({ id: crypto.randomUUID(), info, resolve, deadline: Number(deadline) || 0, done: false });
    refreshConfirmBadge();
    pumpConfirm();
  });
}

async function pumpConfirm() {
  if (confirmActive || !confirmQueue.length) return;
  const item = confirmQueue.shift();
  // The CLI stops waiting at its deadline; never show (or act on) a confirm nobody waits for.
  if (item.deadline && Date.now() > item.deadline - 3000) { item.done = true; item.resolve("timeout"); pumpConfirm(); return; }
  confirmActive = item;
  const { active } = await getPolicyState();
  let ms = (Number(active.confirm_timeout) || 60) * 1000;
  if (item.deadline) ms = Math.min(ms, item.deadline - Date.now() - 3000);
  item.expiresAt = Date.now() + ms;
  item.timer = setTimeout(() => finishConfirm(item, "timeout"), ms);
  try {
    const win = await chrome.windows.create({ url: chrome.runtime.getURL(`confirm.html?id=${item.id}`), type: "popup",
      width: 460, height: 660, focused: true });
    item.windowId = win?.id ?? null;
    if (item.done && item.windowId != null) chrome.windows.remove(item.windowId).catch(() => {});
  } catch (e) {
    finishConfirm(item, "error");
  }
  refreshConfirmBadge();
}

function finishConfirm(item, answer) {
  if (!item || item.done) return;
  item.done = true;
  clearTimeout(item.timer);
  if (confirmActive === item) confirmActive = null;
  if (item.windowId != null) chrome.windows.remove(item.windowId).catch(() => {});
  item.resolve(answer);
  refreshConfirmBadge();
  pumpConfirm();
}

function cancelAllConfirms(answer) {
  for (const item of confirmQueue.splice(0)) { item.done = true; item.resolve(answer); }
  if (confirmActive) finishConfirm(confirmActive, answer);
  refreshConfirmBadge();
}

function refreshConfirmBadge() {
  // The global badge shows "?" while a confirm waits (tab badges R/A still win on their tabs).
  refreshBadges().catch(() => {});
}

// Ask Kevin; resolves on Allow, else throws declined / confirm_timeout / paused (all exit 4).
async function confirmOrThrow(info, req, where) {
  const answer = await requestConfirm(info, req?.deadlineMs);
  if (answer === "allow") return true;
  if (answer === "paused") throw new OpError("paused", "Agents were paused while the action waited for confirmation; nothing was done.", where);
  if (answer === "timeout") throw new OpError("confirm_timeout", "Nobody answered the in-browser confirm in time; nothing was done.", where);
  if (answer === "error") throw new OpError("script_error", "Couldn't open the confirm window; nothing was done.", where);
  throw new OpError("declined", "Kevin denied the action in the browser; nothing was done.", where);
}

// ---- tab actions (P2)
function allowLevel(url, policy) {
  if ((policy.act_allow || []).some((p) => matches(p, url))) return "act";
  if ((policy.read_allow || []).some((p) => matches(p, url))) return "read";
  return null;
}

// The destination of open / navigate: origin_blocked for denied URLs, policy_pending when a pending change decides it.
async function checkDestination(url) {
  const st = await getPolicyState();
  const origin = originOf(url);
  const why = denied(url, st.active);
  if (why) {
    if (pendingAffects(st, { url })) throw new OpError("policy_pending", "That URL waits on a pending policy change.", { origin });
    throw new OpError("origin_blocked", `${origin || url.slice(0, 80)} is denied (${why}).`, { origin });
  }
  if (!origin) throw new OpError("bad_request", "Only http(s) URLs can be opened.");
  if (pendingAffects(st, { url })) throw new OpError("policy_pending", "That URL waits on a pending policy change.", { origin });
  return { origin, level: allowLevel(url, st.active) };
}

// Bot-opened auto-share (spec 6.4): act_allow -> act, read_allow -> read, else unshared (metadata only).
async function autoShare(tabId, url) {
  const st = await getPolicyState();
  const origin = originOf(url);
  if (!origin || denied(url, st.active)) return { level: null };
  const level = allowLevel(url, st.active);
  if (!level) return { level: null };
  if (!(await hasHostPermission(origin))) return { level: null, skipped: "permission_needed" };
  await withLock(async () => {
    const shares = await getShares();
    shares[tabId] = { origin, level, at: Date.now(), botOpened: true };
    await setShares(shares);
  });
  post({ event: "share_changed", tab: tabId, origin, level, reason: "bot-opened" });
  await auditAdd({ op: `auto-share-${level}`, tab: tabId, origin, outcome: "ok" });
  await updateBadge(tabId);
  return { level };
}

// "bot": opened (or cross-origin navigated) by an agent, even if auto-shared via act_allow: strict gating.
// "act": Kevin Share-act'd it himself in the popup. "read": anything else.
async function tabKind(tab) {
  if ((await getBotTabs())[tab.id]) return "bot";
  return (await getShares())[tab.id]?.level === "act" ? "act" : "read";
}

// close / focus / navigate need Share-act or a bot-opened tab (never a denied origin).
async function requireActOrBot(tab) {
  const st = await getPolicyState();
  const url = tab.url || tab.pendingUrl || "";
  const why = denied(url, st.active);
  if (why) throw new OpError("origin_blocked", `Tab ${tab.id} is on a denied origin (${why}).`, { tab: tab.id, origin: originOf(url) });
  if ((await getBotTabs())[tab.id]) return "bot";
  return access(tab, "act");
}

function checkExpect(tab, expect, required) {
  const e = expect && typeof expect === "object" ? expect : {};
  if (!e.origin && !e.urlPrefix) {
    if (required) throw new OpError("missing_expect", "Page actions need expect.origin or expect.urlPrefix (--expect-origin / --expect-url).", { tab: tab.id });
    return;
  }
  const url = tab.url || "";
  const origin = originOf(url);
  if (e.origin) {
    const want = originOf(String(e.origin));
    if (!want) throw new OpError("bad_request", "expect.origin is not an http(s) origin.");
    if (want !== origin) throw new OpError("url_mismatch", `Tab ${tab.id} is at ${origin}, not ${want}.`, { tab: tab.id, origin, url });
  }
  if (e.urlPrefix && !url.startsWith(String(e.urlPrefix))) {
    throw new OpError("url_mismatch", `Tab ${tab.id}'s URL doesn't start with the expected prefix.`, { tab: tab.id, origin, url });
  }
}

async function normalWindow() {
  try { return await chrome.windows.getLastFocused({ windowTypes: ["normal"] }); } catch { return null; }
}

async function tabOpen(args, req) {
  const url = String(args.url || "");
  const { origin, level } = await checkDestination(url);
  const reasons = gateReasons("open", null, { allowlisted: !!level });
  let confirmedInBrowser = false;
  if (reasons.length) {
    await confirmOrThrow({ op: "open", origin, url, reasons, title: null }, req, { origin });
    confirmedInBrowser = true;
    await checkDestination(url); // the policy may have changed while Kevin looked
    if (await getPaused()) throw new OpError("paused", "Agents were paused; nothing was done.");
  }
  let tab;
  if (args.window === "new") {
    const win = await chrome.windows.create({ url, focused: !!args.active, type: "normal" });
    tab = win.tabs?.[0];
  } else {
    const win = await normalWindow();
    tab = await chrome.tabs.create({ url, active: !!args.active, ...(win ? { windowId: win.id } : {}) });
  }
  if (!tab) throw new OpError("script_error", "Chrome didn't return the new tab.", { origin });
  await markBot(tab.id);
  const share = await autoShare(tab.id, url);
  return { tab: tab.id, op: "open", url, origin, windowId: tab.windowId, active: !!args.active, botOpened: true,
    share: share.level, ...(share.skipped ? { shareSkipped: share.skipped } : {}),
    gated: reasons.length > 0, reasons, confirmedInBrowser, dispatched: true, verified: true };
}

async function tabFocus(args) {
  const tab = await resolveTab({ tab: args.tab });
  const level = await requireActOrBot(tab);
  await chrome.tabs.update(tab.id, { active: true });
  await chrome.windows.update(tab.windowId, { focused: true });
  return { tab: tab.id, op: "focus", origin: originOf(tab.url || ""), share: level === "bot" ? null : level,
    botOpened: level === "bot", gated: false, reasons: [], confirmedInBrowser: false, dispatched: true, verified: true };
}

async function tabClose(args) {
  const tab = await resolveTab({ tab: args.tab });
  const level = await requireActOrBot(tab);
  await chrome.tabs.remove(tab.id);
  return { tab: tab.id, op: "close", origin: originOf(tab.url || ""), title: tab.title || "", botOpened: level === "bot",
    gated: false, reasons: [], confirmedInBrowser: false, dispatched: true, verified: true };
}

async function waitFor(check, ms, every = 150) {
  const end = Date.now() + ms;
  for (;;) {
    try { if (await check()) return true; } catch { /* page in flux */ }
    if (Date.now() >= end) return false;
    await new Promise((r) => setTimeout(r, every));
  }
}

async function tabNavigate(args, req) {
  let tab = await resolveTab({ tab: args.tab });
  await requireActOrBot(tab);
  checkExpect(tab, args.expect, false);
  const url = String(args.url || "");
  const { origin, level } = await checkDestination(url); // the deny list always applies
  // 0.9.1: a same-origin move in a tab Kevin Share-act'd himself isn't gated; cross-origin and bot tabs are as before.
  const sameOriginAct = originOf(tab.url || "") === origin && (await tabKind(tab)) === "act";
  const reasons = gateReasons("navigate", null, { allowlisted: !!level || sameOriginAct });
  let confirmedInBrowser = false;
  if (reasons.length) {
    await confirmOrThrow({ op: "navigate", origin, url, reasons, title: tab.title || "", from: originOf(tab.url || "") },
      req, { tab: tab.id, origin });
    confirmedInBrowser = true;
    tab = await resolveTab({ tab: tab.id });
    await requireActOrBot(tab);
    checkExpect(tab, args.expect, false);
    await checkDestination(url);
    if (await getPaused()) throw new OpError("paused", "Agents were paused; nothing was done.");
  }
  const cross = originOf(tab.url || "") !== origin;
  if (cross) {
    // A cross-origin navigate by the agent makes it a bot-opened tab on the new origin (spec 6.4).
    await revoke(tab.id, "navigated by agent");
    await markBot(tab.id);
  }
  await chrome.tabs.update(tab.id, { url });
  let share = { level: null };
  if (cross) {
    await waitFor(async () => { const t = await chrome.tabs.get(tab.id); return originOf(t.pendingUrl || t.url || "") === origin; }, 10000);
    share = await autoShare(tab.id, url);
  } else {
    share = { level: (await getShares())[tab.id]?.level ?? null };
  }
  return { tab: tab.id, op: "navigate", url, origin, crossOrigin: cross, share: share.level,
    ...(share.skipped ? { shareSkipped: share.skipped } : {}), botOpened: cross || !!(await getBotTabs())[tab.id],
    gated: reasons.length > 0, reasons, confirmedInBrowser, dispatched: true, verified: true };
}

// ---- page actions (P3)
function normTarget(t, optional) {
  const tt = t && typeof t === "object" ? t : {};
  const given = ["selector", "ref", "text", "find"].filter((k) => tt[k] != null && tt[k] !== "");
  if (given.length === 0 && optional) return null;
  if (given.length !== 1) throw new OpError("bad_target", "Give exactly one target: selector, ref, text (+ role), or find.");
  const k = given[0];
  return k === "text" ? { text: String(tt.text), ...(tt.role ? { role: String(tt.role) } : {}) } : { [k]: String(tt[k]) };
}

async function pagePre(args, need = "act") {
  if (!Number.isInteger(args.tab)) throw new OpError("bad_target", "Page actions need a tab id (--tab).");
  const tab = await resolveTab({ tab: args.tab });
  const level = await access(tab, need);
  const origin = originOf(tab.url || "");
  if (!(await hasHostPermission(origin))) {
    throw new OpError("permission_needed", `chromecli has no host access to ${origin}.`, { tab: tab.id, origin });
  }
  if (need === "act") checkExpect(tab, args.expect, true);
  return { tab, origin, level };
}

async function inject(tab, origin, func, spec, { afterDispatch = false } = {}) {
  let res;
  try {
    [res] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, world: "ISOLATED", func, args: [spec] });
  } catch (e) {
    const m = String(e?.message || e);
    if (/permission|cannot access|must request/i.test(m)) throw new OpError("permission_needed", m, { tab: tab.id, origin });
    if (afterDispatch) return { ok: true, dispatched: true, lost: m };
    throw new OpError("script_error", m, { tab: tab.id, origin });
  }
  const out = res?.result;
  if (!out) {
    if (afterDispatch) return { ok: true, dispatched: true, lost: "no result" };
    throw new OpError("script_error", "The page returned nothing.", { tab: tab.id, origin });
  }
  if (!out.ok) throw new OpError(out.code || "script_error", out.message || "Page action failed.", { tab: tab.id, origin, ...out, ok: undefined, code: undefined, message: undefined });
  return out;
}

async function pageOp(op, args, req) {
  if (op === "eval") {
    await pagePre(args);
    // Chrome's fixed CSP for extension isolated worlds forbids eval / new Function, so user code can't run there.
    throw new OpError("eval_disabled", "eval is disabled: Chrome's MV3 isolated-world CSP blocks running code strings, and chromecli won't run them in the page's own world.", { tab: args.tab });
  }
  let target = normTarget(args.target, op === "press");
  if (op === "press" && !String(args.key || "")) throw new OpError("bad_request", "press needs a key.");
  const { tab, origin } = await pagePre(args);
  const kind = await tabKind(tab);
  const j = await jevFor(args.jev);
  let jev = { used: false, mode: j.mode, ...(j.why ? { reason: j.why } : {}) };
  let jevInfo = null;
  let jevFailed = false;
  let found = null;
  if (target?.find != null) {
    const r = await locate(tab, origin, String(target.find), j);
    jev = { ...jev, ...r.jev };
    jevFailed = !!r.jev.error;
    if (!r.pick) {
      throw new OpError("ambiguous_target", `No element clearly matches that description (${r.list[0] ? r.list[0].source : "no candidates"}).`,
        { tab: tab.id, origin, candidates: r.list.slice(0, 5), jev });
    }
    found = { ref: r.pick, source: r.list.find((c) => c.ref === r.pick)?.source || "heuristic" };
    target = { ref: r.pick };
  }
  const spec = { mode: "describe", op, target, key: args.key };
  const d = await inject(tab, origin, pageAction, spec);
  const element = d.element;
  const extra = [];
  if ((op === "click" || op === "press") && j.use) {
    try {
      const resp = await callJev(globalThis.fetch, j.cfg.key, classifyBody({ model: j.cfg.model, page: { origin, title: tab.title || "" },
        element: classifyElement(element) }));
      const c = parseClassify(resp, j.cfg.floor);
      const gate = classifyGate(c, kind, j.cfg);
      if (gate) extra.push(gate);
      jev = { ...jev, used: true, choice: c.choice, confidence: c.confidence };
      // What Jev said, shown even when it isn't a gate (e.g. "safe" below the floor on a tab Kevin shared).
      jevInfo = { reason: `jev:${c.choice}`, choice: c.choice, confidence: c.confidence, gated: !!gate,
        policy: kind === "act" ? `act-shared: gates only ${"purchase/destructive/send_publish/credential"} at >= ${j.cfg.riskFloor}`
          : `bot-opened: gates unless safe at >= ${j.cfg.floor}` };
    } catch (e) {
      if (!(e instanceof JevError)) throw e;
      jev = { ...jev, ...jevFail(j, e, { tab: tab.id, origin, element }) };
      jevFailed = true;
    }
  }
  if (jevFailed) extra.push(...fallbackFor(kind, element));
  // Jev only adds gates; the heuristic ones always stay.
  const reasons = mergeReasons(gateReasons(op, element, { key: args.key }), extra);
  const where = { tab: tab.id, origin, element, reasons, jev, tabKind: kind, ...(jevInfo ? { jevInfo } : {}) };
  let confirmedInBrowser = false;
  if (reasons.length) {
    const value = ["type", "fill", "select"].includes(op) ? displayValue(args.value, element) : null;
    await confirmOrThrow({ op, origin, url: tab.url, title: tab.title || "", element, reasons, value,
      key: op === "press" ? String(args.key) : null }, req, where);
    confirmedInBrowser = true;
    // Re-check every precondition after the confirm (spec 6.6).
    if (await getPaused()) throw new OpError("paused", "Agents were paused; nothing was done.", where);
    await pagePre(args);
  }
  const done = await inject(tab, origin, pageAction, { ...spec, mode: "do", value: args.value, fingerprint: d.fingerprint },
    { afterDispatch: true });
  const out = { tab: tab.id, origin, op, element, gated: reasons.length > 0, reasons, confirmedInBrowser, dispatched: true,
    verified: null, jev, tabKind: kind, ...(jevInfo ? { jevInfo } : {}), ...(found ? { found } : {}), ...(done.selected ? { selected: done.selected } : {}), ...(done.effect ? { effect: done.effect } : {}) };
  // The page went away mid-call (a click that navigates can do that): it was dispatched, the result is unknown.
  if (done.lost) out.resultLost = String(done.lost).slice(0, 200);
  const failed = [];
  const checks = [];
  if (["fill", "type", "select"].includes(op)) { checks.push("read_back"); if (done.readBack !== true) failed.push("read_back"); }
  const verify = args.verify && typeof args.verify === "object" ? args.verify : {};
  if (verify.urlPrefix) {
    checks.push("url_prefix");
    const ok = await waitFor(async () => String((await chrome.tabs.get(tab.id)).url || "").startsWith(String(verify.urlPrefix)), 10000);
    if (!ok) failed.push("url_prefix");
  }
  if (verify.text) {
    checks.push("text");
    const ok = await waitFor(async () => {
      const t = await chrome.tabs.get(tab.id);
      if (originOf(t.url || "") !== origin) return false;
      const [r] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, world: "ISOLATED", func: pageProbe, args: [{ text: String(verify.text) }] });
      return !!r?.result?.found;
    }, 10000, 250);
    if (!ok) failed.push("text");
  }
  if (checks.length) out.verified = failed.length === 0;
  out.checks = checks;
  if (failed.length) {
    throw new OpError("sent_unverified", `The ${op} was dispatched but its check failed (${failed.join(", ")}). Don't repeat it blindly; read the page first.`, { ...out, failed });
  }
  return out;
}

// ---- Jev (P4): find, --find, classification. The key goes only into callJev's fetch, never into a reply or log.
async function jevFor(mode) {
  const m = jevMode(mode);
  const cfg = await getJev();
  if (m === "off") return { use: false, mode: m, why: "off", cfg: null };
  if (!cfg.configured) {
    if (m === "on") {
      throw new OpError("jev_not_configured", "--jev on, but no Jev key is set.", {
        hint: "Kevin: open chromecli's options (popup → Jev → Options) and paste a TypeSafe API key." });
    }
    return { use: false, mode: m, why: "not_configured", cfg: null };
  }
  return { use: true, mode: m, cfg };
}

// Under --jev on a failure is exit 5 jev_error; under auto it is recorded and the caller gates more strictly.
function jevFail(j, e, where) {
  if (j.mode === "on") throw new OpError("jev_error", e.message, where);
  return { used: false, error: e.message };
}

function classifyElement(el) {
  const out = {};
  for (const k of ["tag", "role", "type", "name", "label", "autocomplete", "inForm", "formAction"]) {
    if (el?.[k] != null && el[k] !== "") out[k] = typeof el[k] === "string" ? el[k].slice(0, 120) : el[k];
  }
  if (el?.ref) out.id = el.ref;
  return out;
}

const candidateRow = (el, score, source) => ({ ref: el.id, score, source, tag: el.tag, role: el.role || null, name: el.name || "" });

async function locate(tab, origin, intent, j) {
  if (!intent.trim()) throw new OpError("bad_target", "find needs a description (intent).");
  const snap = await inject(tab, origin, pageSnapshot, { max: 300 });
  const ranked = heuristicRank(intent, snap.elements || []);
  let list = ranked.map((r) => candidateRow(r.el, r.score, "heuristic"));
  let pick = heuristicPick(ranked)?.el.id ?? null;
  let jev = { used: false };
  if (j.use) {
    const snapshot = buildSnapshot(ranked.map((r) => r.el));
    const cands = snapshot.slice(0, LOCATE_MAX).map((e) => e.id);
    try {
      const resp = await callJev(globalThis.fetch, j.cfg.key, locateBody({ model: j.cfg.model, intent,
        page: { origin, title: snap.title || tab.title || "" }, elements: snapshot, candidates: cands }));
      const p = parseLocate(resp, cands, j.cfg.floor);
      const byId = new Map((snap.elements || []).map((e) => [e.id, e]));
      list = p.ranked.map((r) => candidateRow(byId.get(r.id), r.noul, "jev"));
      pick = p.pick;
      jev = { used: true };
    } catch (e) {
      if (!(e instanceof JevError)) throw e;
      jev = jevFail(j, e, { tab: tab.id, origin });
    }
  }
  return { list, pick, jev };
}

async function pageFind(args) {
  const { tab, origin } = await pagePre(args, "read");
  const j = await jevFor(args.jev);
  const limit = Math.min(Math.max(1, Number(args.limit) || 5), 50);
  const r = await locate(tab, origin, String(args.intent || ""), j);
  return { tab: tab.id, origin, results: r.list.slice(0, limit), pick: r.pick,
    jev: { mode: j.mode, ...(j.why ? { reason: j.why } : {}), ...r.jev } };
}

async function jevTest() {
  const cfg = await getJev();
  if (!cfg.configured) return { ok: false, error: "No key saved." };
  try {
    const resp = await callJev(globalThis.fetch, cfg.key, classifyBody({ model: cfg.model, page: { origin: "https://example.com", title: "Example" },
      element: { id: "e1", tag: "a", role: "link", name: "Next page" } }));
    const c = parseClassify(resp, cfg.floor);
    return { ok: true, choice: c.choice, confidence: c.confidence };
  } catch (e) {
    return { ok: false, error: e instanceof JevError ? e.message : "Unexpected error." };
  }
}

async function pageWait(args) {
  const { tab, origin } = await pagePre(args, "read");
  const ms = Math.min(Math.max(100, Number(args.timeoutMs) || 15000), 120000);
  const probe = args.selector ? { selector: String(args.selector) } : args.text != null ? { text: String(args.text) } : null;
  if (!probe && !args.urlPrefix) throw new OpError("bad_target", "wait needs selector, text, or urlPrefix.");
  const start = Date.now();
  let lastErr = null;
  const ok = await waitFor(async () => {
    const t = await chrome.tabs.get(tab.id);
    await access(t, "read");
    if (args.urlPrefix) return String(t.url || "").startsWith(String(args.urlPrefix));
    const [r] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, world: "ISOLATED", func: pageProbe, args: [probe] });
    if (r?.result && r.result.ok === false) { lastErr = r.result; return true; }
    return !!r?.result?.found;
  }, ms, 250);
  if (lastErr) throw new OpError(lastErr.code || "bad_target", lastErr.message || "Bad probe.", { tab: tab.id, origin });
  if (!ok) throw new OpError("timeout", `Waited ${ms / 1000}s; the condition didn't hold.`, { tab: tab.id, origin });
  return { tab: tab.id, origin, found: true, waitedMs: Date.now() - start };
}

// ---- popup messages (only from this extension's own pages, never from a tab)
// The confirm window is an extension page in a popup window (so it has sender.tab); it may only read and answer
// its own confirm. No web page can load confirm.html (it is not web-accessible).
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  const url = String(sender.url || "");
  if (sender.id !== chrome.runtime.id || !url.startsWith(chrome.runtime.getURL(""))) return false;
  const fromConfirm = url.startsWith(chrome.runtime.getURL("confirm.html"));
  const fromOptions = url.startsWith(chrome.runtime.getURL("options.html"));
  if (fromConfirm ? !["confirmGet", "confirmAnswer"].includes(msg?.type)
    : fromOptions ? !["jevStatus", "jevTest"].includes(msg?.type) : !!sender.tab) return false;
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
      // Every shared tab, for the popup's list (read-only view of the share map).
      const shared = [];
      for (const [id, s] of Object.entries(shares)) {
        let t = null;
        try { t = await chrome.tabs.get(Number(id)); } catch { /* gone; revoked on its own */ }
        shared.push({ tabId: Number(id), origin: s.origin, level: s.level, at: s.at ?? null,
          title: t?.title || "", favIconUrl: t?.favIconUrl || "", windowId: t?.windowId ?? null });
      }
      return { ok: true, tab: tab && { id: tab.id, title: tab.title, url, origin: originOf(url), favIconUrl: tab.favIconUrl || "" },
        shared,
        share: tab ? shares[tab.id] || null : null, deniedReason: tab ? denied(url, st.active) : null,
        paused, pending: st.pending, policyHash: st.active.hash, host: hostStatus,
        shareCount: Object.keys(shares).length, audit: audit.slice(-20).reverse(), version: VERSION,
        confirms: pendingConfirms(), jevConfigured: (await getJev()).configured };
    }
    case "jevStatus": { const j = await getJev(); return { ok: true, configured: j.configured, model: j.model, floor: j.floor }; }
    case "jevTest": return jevTest();
    case "showConfirm": {
      if (confirmActive?.windowId != null) await chrome.windows.update(confirmActive.windowId, { focused: true }).catch(() => {});
      return { ok: true };
    }
    case "confirmGet": {
      const item = confirmActive && confirmActive.id === msg.id ? confirmActive : null;
      if (!item) return { ok: false, error: "This request is no longer waiting." };
      return { ok: true, id: item.id, info: item.info, expiresAt: item.expiresAt, queued: confirmQueue.length };
    }
    case "confirmAnswer": {
      const item = confirmActive && confirmActive.id === msg.id ? confirmActive : null;
      if (!item) return { ok: false, error: "This request is no longer waiting." };
      finishConfirm(item, msg.answer === "allow" ? "allow" : "deny");
      return { ok: true };
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
chrome.tabs.onRemoved.addListener(async (tabId) => {
  await revoke(tabId, "closed");
  if ((await getBotTabs())[tabId]) await markBot(tabId, false);
});
chrome.windows.onRemoved.addListener((windowId) => {
  // Closing the confirm window is a Deny.
  if (confirmActive && confirmActive.windowId === windowId) finishConfirm(confirmActive, "deny");
});
chrome.tabs.onReplaced.addListener((_added, removed) => revoke(removed, "replaced"));
// A new / removed Jev key changes status.jevConfigured: tell the host (never the key).
chrome.storage.onChanged?.addListener((changes, area) => {
  if (area === "local" && ("typesafeApiKey" in changes)) sendHello();
});
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
