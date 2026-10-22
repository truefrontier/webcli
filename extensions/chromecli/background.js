// chromecli service worker: native port to chromecli-host, op dispatch (read ops, tab actions, page actions), shares,
// bot-opened tabs, Pause, origin policy with pending-accept for loosening, gate heuristics + the in-browser confirm
// window (one at a time, others queue), badges, and a 200-entry audit ring buffer for the popup.
// The extension is the authority: the host and CLI are untrusted callers.
import {
  DEFAULT_POLICY, acceptPending, applyIncoming, denied, denyException, matches, originOf, pendingAffects, permissionPattern,
  rejectPending, validPolicy,
} from "./policy.js";
import { displayValue, gateReasons } from "./gate.js";
import { pageAction, pageProbe } from "./page/actions.js";
import { pageSnapshot } from "./page/snapshot.js";
import { pageScroll } from "./page/scroll.js";
import {
  JEV_STORAGE_KEYS, JevError, LOCATE_MAX, buildSnapshot, callJev, classifyBody, classifyGate, fallbackFor, heuristicPick, heuristicRank, jevConfig,
  jevMode, locateBody, mergeReasons, parseClassify, parseLocate,
} from "./jev.js";
import { badgeFor, levelAllows, revokeReason } from "./shares.js";
import { extractPage } from "./page/extract.js";
import { classifyFrames, frameGeometry, frameOrigin, mergeRead, parseFrameRef, pickScrollTarget, shortUrl, skippedList } from "./frames.js";
import {
  GrantError, PAGE_GRANT_OPS, amountCheck, canSkip, fmtAmount, grantMatches, grantView, isLive, parseScope, scopeBody,
  validateGrant,
} from "./grants.js";
import {
  BATCH_MAX, checkClause, clauseScopeBody, mapBody, pageContext, parseClauseScope, parseMap, quoteKey, splitQuote,
} from "./clauses.js";
import {
  CHUNK_BYTES, GRANT_ERROR, isActive, recBadge, recButton, recView, recordOptions, refusal, validRecId,
} from "./recorder.js";

const HOST = "com.truefrontier.chromecli";
const VERSION = chrome.runtime.getManifest().version;
const AUDIT_MAX = 200;
// page.scroll (0.10.2) is a read op: it moves a scroll position on a Read or Act tab, never prompts, never gated.
const READ_OPS = ["status", "tabs.list", "tabs.show", "tabs.read", "tabs.screenshot", "history.search", "page.wait",
  "page.find", "page.probe", "page.scroll", "grant.list", "record.status"];
const TAB_OPS = ["tab.open", "tab.focus", "tab.close", "tab.navigate"];
const PAGE_OPS = ["page.click", "page.type", "page.fill", "page.select", "page.press", "page.eval"];
const GRANT_WRITE_OPS = ["grant.add", "grant.add_batch", "grant.revoke"];
// 0.12.1: record.start needs --confirm and Kevin's Start; record.stop only ever stops (no --confirm).
const RECORD_OPS = ["record.start", "record.stop"];
const OPS = new Set([...READ_OPS, ...TAB_OPS, ...PAGE_OPS, ...GRANT_WRITE_OPS, ...RECORD_OPS]);
// Ops that change something: the CLI sends them only with --confirm (confirm: true); anything else is refused.
const MUTATING = new Set([...TAB_OPS, ...PAGE_OPS, ...GRANT_WRITE_OPS, "record.start"]);

let port = null;
let backoff = 1000;
let reconnectTimer = null;
let hostStatus = { connected: false, lastError: null, since: null };

class OpError extends Error {
  constructor(code, message, extra = {}) { super(message); this.code = code; this.extra = extra; }
}
function dropUndefined(o) {
  return Object.fromEntries(Object.entries(o || {}).filter(([k, v]) => v !== undefined && k !== "code" && k !== "message" && k !== "ok"));
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
// Pre-approval grants (0.10.0): session storage, so they die with the browser. 0.10.1: no toggle; grant.add needs
// userApproved: true from the CLI and Jev's scope check (grantAdd).
const getGrants = async () => (await chrome.storage.session.get("grants")).grants || {};
const setGrants = (grants) => chrome.storage.session.set({ grants });
async function clearGrants(reason) {
  const n = Object.keys(await getGrants()).length;
  await withLock(() => setGrants({}));
  if (n) {
    post({ event: "grants_cleared", count: n, reason });
    await auditAdd({ op: "grants-revoked", tab: null, origin: null, outcome: `${n} (${reason})` });
  }
  return n;
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
  return jevConfig(await chrome.storage.local.get(JEV_STORAGE_KEYS));
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
    // The code and message always win over same-named extra fields (0.10.2 let a page reply's undefined code and
    // message through, so agents saw a bare script_error).
    const err = e instanceof OpError ? { ...dropUndefined(e.extra), code: e.code, message: e.message }
      : { code: "script_error", message: String(e?.message || e) };
    origin = err.origin || null;
    reply = { v: 1, id: req.id, ok: false, error: err };
  }
  // 0.11.3: say when a deny exception (deny_except) is what let this URL through (host log + popup audit).
  const info0 = reply.ok ? reply.result : reply.error;
  if (info0 && typeof info0 === "object" && req.op !== "status") {
    let url = typeof info0.url === "string" ? info0.url : null;
    if (!url && Number.isInteger(info0.tab)) { try { url = (await chrome.tabs.get(info0.tab)).url || null; } catch { /* closed */ } }
    const ex = url ? denyException(url, (await getPolicyState()).active) : null;
    if (ex) info0.denyExcept = ex;
  }
  post(reply);
  if (req.op !== "status") {
    const info = reply.ok ? reply.result : reply.error;
    const extra = info && Array.isArray(info.reasons) && info.reasons.length
      ? { reasons: info.reasons, confirmedInBrowser: !!info.confirmedInBrowser } : {};
    if (info?.denyExcept) extra.denyExcept = info.denyExcept;
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
  // 0.12.1: unshare / origin change / close / deny ends the tab's recording (the file is finalized).
  const rec = recFor(tabId);
  const why = reason === "unshared" ? "unshared" : reason === "closed" ? "tab closed" : `unshared (${reason})`;
  if (rec) stopRecording(rec.recId, why).catch(() => {});
  if (gone) {
    post({ event: "share_changed", tab: tabId, origin: gone.origin, level: null, reason });
    await auditAdd({ op: "unshare", tab: tabId, origin: gone.origin, outcome: reason });
    await updateBadge(tabId);
  }
}

async function updateBadge(tabId) {
  const [shares, paused] = [await getShares(), await getPaused()];
  try {
    const rb = recBadge(recFor(tabId));
    if (rb) {
      await chrome.action.setBadgeText({ tabId, text: rb.text });
      await chrome.action.setBadgeBackgroundColor({ tabId, color: rb.color });
    } else if (shares[tabId]) {
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
  if (paused) { cancelAllConfirms("paused"); await clearGrants("paused"); stopAllRecordings("paused").catch(() => {}); }
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
    case "record.start": return recordStart(args, req);
    case "record.stop": return recordStop(args);
    case "record.status": return recordStatus(args);
    case "history.search": return historySearch(args);
    case "tab.open": return tabOpen(args, req);
    case "tab.focus": return tabFocus(args);
    case "tab.close": return tabClose(args);
    case "tab.navigate": return tabNavigate(args, req);
    case "page.wait": return pageWait(args);
    case "page.find": return pageFind(args);
    case "page.probe": return pageProbeOp(args);
    case "page.scroll": return pageScrollOp(args);
    case "grant.add": return grantAdd(args);
    case "grant.add_batch": return grantAddBatch(args);
    case "grant.list": return grantList();
    case "grant.revoke": return grantRevoke(args);
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
    recording: { canRecord: !!(chrome.tabCapture?.getMediaStreamId && chrome.offscreen?.createDocument),
      active: [...recordings.values()].filter(isActive).map((r) => ({ recId: r.recId, tab: r.tab, state: r.state })) },
    policy: { hash: st.active.hash, pending: !!st.pending, pendingLoosening: st.pending?.loosening || [],
      implicitActive: st.active.implicit_active, history: st.active.history, deny: st.active.deny,
      denyExcept: st.active.deny_except || [], frameActAllow: st.active.frame_act_allow || [], readAllow: st.active.read_allow, actAllow: st.active.act_allow },
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
      // Bot-opened tabs (and every tab with --all) show as metadata (share null) even when unshared, unless they're
      // on a denied origin.
      if (!((bot[tab.id] || args.all) && e.code === "not_shared" && !denied(tab.url || tab.pendingUrl || "", st.active))) continue;
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
  // 0.11.1: every frame the share covers (see frames.js), each read on its own, merged with a frame tag.
  const fr = await tabFrames(tab, origin);
  const runs = await injectFrames(tab, origin, fr.allowed, extractPage, opts);
  const top = runs.find((r) => r.frameId === 0);
  if (top.error) throw top.error;
  const out = top.result;
  // The page may have navigated while we read: re-check before handing anything back.
  const st = await getPolicyState();
  if (originOf(out.url) !== origin || denied(out.url, st.active)) {
    await revoke(tab.id, "origin changed during read");
    throw new OpError("not_shared", `Tab ${tab.id} navigated away during the read.`, { tab: tab.id });
  }
  const { parts, failed, reports } = frameRuns(runs, fr);
  const { geo, unmatched } = frameGeometry(reports, fr.rows);
  const merged = mergeRead(parts, { as: opts.as, maxChars: opts.maxChars, geo });
  const skipped = skippedList({ frameSkips: fr.skipped, failed, unmatched, reports, geo });
  const { diag, content, chars, truncated, elements, ...rest } = out;
  return { tab: tab.id, origin, share: level, ...rest, content: merged.content, chars: merged.chars, truncated: merged.truncated,
    ...(merged.elements ? { elements: merged.elements } : {}),
    ...(fr.allowed.length > 1 || fr.skipped.length ? { frames: merged.frames } : {}),
    ...(skipped.length ? { skipped } : {}) };
}

// ---- frames (0.11.1)
// The tab's frames an op may read and the ones it skips (frames.js has the rule). Without webNavigation (or when
// Chrome lists nothing) it's the top frame only, as before 0.11.1.
async function tabFrames(tab, origin) {
  const topOnly = { allowed: [{ frameId: 0, parentFrameId: -1, url: tab.url || "", origin, depth: 0 }], skipped: [],
    rows: [{ frameId: 0, parentFrameId: -1, url: tab.url || "" }], listed: false };
  if (!chrome.webNavigation?.getAllFrames) return topOnly;
  let rows;
  try { rows = await chrome.webNavigation.getAllFrames({ tabId: tab.id }); } catch { return topOnly; }
  if (!Array.isArray(rows) || !rows.some((f) => f.frameId === 0)) return topOnly;
  const st = await getPolicyState();
  // 0.11.3: frame_act_allow origins count only on tabs Kevin shared Act himself (or inherited Act), never bot tabs.
  const actOrigins = (st.active.frame_act_allow || []).length && (await tabKind(tab)) === "act" ? st.active.frame_act_allow : [];
  const { allowed, skipped } = await classifyFrames(rows, { tabOrigin: origin, policy: st.active, hasPermission: hasHostPermission, actOrigins });
  return { allowed, skipped, rows, listed: true };
}

// One injection into one frame: {frameId, origin, url, result} or {frameId, origin, error: OpError} (reason set).
async function injectOne(tab, origin, frame, func, spec) {
  const where = { tab: tab.id, origin, ...(frame.frameId ? { frameId: frame.frameId, frameOrigin: frame.origin } : {}) };
  let res;
  try {
    [res] = await chrome.scripting.executeScript({ target: { tabId: tab.id, frameIds: [frame.frameId] }, world: "ISOLATED", func, args: [spec] });
  } catch (e) {
    const m = String(e?.message || e);
    if (/permission|cannot access|must request/i.test(m)) {
      return { ...frame, error: new OpError(frame.frameId ? "script_error" : "permission_needed", m,
        { ...where, reason: frame.frameId ? "frame_unreachable" : "no_host_permission" }) };
    }
    return { ...frame, error: new OpError("script_error", m, { ...where, reason: frame.frameId ? "frame_unreachable" : "inject_failed" }) };
  }
  const out = res?.result;
  if (!out) return { ...frame, error: new OpError("script_error", noResultMessage(res), { ...where, reason: frame.frameId ? "frame_unreachable" : "no_result" }) };
  if (out.ok === false) {
    const { ok, code, message, diag, ...extra } = out;
    return { ...frame, error: new OpError(code || "script_error", message || "The page script failed.", { ...where, reason: "page_exception", ...extra }), diag };
  }
  // A subframe that navigated elsewhere since we listed it is dropped.
  if (frame.frameId && frameOrigin(out.url, frame.origin) !== frame.origin) {
    return { ...frame, error: new OpError("script_error", "The frame navigated during the call.", { ...where, reason: "frame_unreachable" }) };
  }
  return { ...frame, result: out };
}

const injectFrames = (tab, origin, frames, func, spec) => Promise.all(frames.map((f) => injectOne(tab, origin, f, func, spec)));

// Split per-frame runs into results (in frame order), failures (as skipped rows), and each frame's page diag.
function frameRuns(runs, fr) {
  const parts = [];
  const failed = [];
  const reports = new Map();
  for (const r of runs) {
    const diag = r.result?.diag || r.diag;
    if (diag) reports.set(r.frameId, diag);
    if (r.error) {
      if (r.frameId !== 0) {
        failed.push({ kind: "iframe", frameId: r.frameId, parentFrameId: r.parentFrameId, origin: r.origin, url: shortUrl(r.url),
          reason: "frame_unreachable", detail: String(r.error.message || "").slice(0, 200) });
      }
      continue;
    }
    parts.push(r);
  }
  return { parts, failed, reports };
}

function frameError(frameId, fr) {
  const sk = fr.skipped.find((s) => s.frameId === frameId);
  return new OpError("script_error", sk ? `Frame ${frameId} is skipped (${sk.reason}).` : `No frame ${frameId} in this tab (it navigated or closed); start again.`,
    { reason: "frame_unreachable", frameId, ...(sk ? { skipped: [sk] } : {}) });
}

async function screenshot(args) {
  const tab = await resolveTab(args);
  const level = await access(tab);
  const origin = originOf(tab.url);
  let win = null;
  try { win = await chrome.windows.getLastFocused({ windowTypes: ["normal"] }); } catch { /* none */ }
  const visible = !!(tab.active && win && win.id === tab.windowId && win.state !== "minimized");
  const format = args.format === "jpeg" ? "jpeg" : "png";
  // 0.12.1: covered (or asked with covered: true): a frame from the tab's recording stream.
  const rec = recFor(tab.id);
  if (args.covered === true || !visible) {
    if (rec?.state === "recording") return streamShot(tab, rec, level, format, args.quality);
    throw new OpError("not_visible", visible ? `Tab ${tab.id} has no active recording to take a covered screenshot from.`
      : `Tab ${tab.id} isn't the active tab of the front Chrome window, and it isn't being recorded.`, { tab: tab.id, origin,
      hint: "Start a recording (chromecli record start TAB --confirm; Kevin clicks Start) and screenshot again, or have Kevin bring the tab to the front." });
  }
  const opts = format === "jpeg" ? { format, quality: Math.min(Math.max(1, Number(args.quality) || 80), 100) } : { format };
  let dataUrl;
  try {
    dataUrl = await captureVisible(tab.windowId, opts);
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
  return { tab: tab.id, url: tab.url, title: tab.title, origin, share: level, format, source: "visible",
    data: dataUrl.slice(dataUrl.indexOf(",") + 1) };
}

// captureVisibleTab allows about 2 calls a second (MAX_CAPTURE_VISIBLE_TAB_CALLS_PER_SECOND): space captures out
// (0.10.2: `screenshot --full-page` takes one per viewport) and retry once after a second if Chrome still refuses.
let lastCapture = 0;
const CAPTURE_GAP_MS = () => (globalThis.__chromecliCaptureGapMs ?? 550);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function captureVisible(windowId, opts) {
  const wait = lastCapture + CAPTURE_GAP_MS() - Date.now();
  if (wait > 0) await sleep(wait);
  lastCapture = Date.now();
  try {
    return await chrome.tabs.captureVisibleTab(windowId, opts);
  } catch (e) {
    if (!/MAX_CAPTURE_VISIBLE_TAB_CALLS_PER_SECOND|quota/i.test(String(e?.message || e))) throw e;
    await sleep(1000);
    lastCapture = Date.now();
    return chrome.tabs.captureVisibleTab(windowId, opts);
  }
}

// ---- scroll (0.10.2): a read op on Read or Act tabs (never Off / denied / paused: the same checks and errors as read).
function scrollSpec(args) {
  const spec = {};
  const actions = ["measure", "to", "by", "into"].filter((k) => args[k] != null && args[k] !== false && args[k] !== "");
  if (actions.length > 1) throw new OpError("bad_request", "Give at most one of measure, to, by, into.");
  if (args.measure === true) spec.measure = true;
  if (args.to != null) {
    const t = args.to;
    if (t === "top" || t === "bottom") spec.to = t;
    else if (t && typeof t === "object" && Number.isFinite(Number(t.top)) && Number(t.top) >= 0) {
      spec.to = { top: Number(t.top), ...(Number.isFinite(Number(t.left)) ? { left: Math.max(0, Number(t.left)) } : {}) };
    } else throw new OpError("bad_request", "to must be top, bottom, or {top: px}.");
  }
  if (args.by != null) {
    if (args.by === "page") spec.by = "page";
    else if (Number.isFinite(Number(args.by)) && Number(args.by) > 0 && Number(args.by) <= 1e6) spec.by = Number(args.by);
    else throw new OpError("bad_request", "by must be page or a pixel count (1..1000000).");
  }
  if (args.dir != null) {
    if (args.dir !== 1 && args.dir !== -1) throw new OpError("bad_request", "dir must be 1 (down) or -1 (up).");
    spec.dir = args.dir;
  }
  for (const k of ["into", "container"]) {
    if (args[k] == null || args[k] === "") continue;
    if (typeof args[k] !== "string" || args[k].length > 1000) throw new OpError("bad_target", `${k} must be a CSS selector (<= 1000 chars).`);
    spec[k] = args[k];
  }
  if (args.path != null) {
    const ok = args.path === "document" || (Array.isArray(args.path) && args.path.length > 0 && args.path.length <= 32 &&
      args.path.every((seg) => Array.isArray(seg) && seg.length > 0 && seg.length <= 500 && seg.every((i) => Number.isInteger(i) && i >= 0)));
    if (!ok) throw new OpError("bad_request", "path must be \"document\" or the target.path of an earlier scroll reply.");
    spec.path = args.path;
  }
  if (args.frameId != null && !(Number.isInteger(args.frameId) && args.frameId >= 0)) {
    throw new OpError("bad_request", "frameId must be the target.frameId of an earlier scroll reply.");
  }
  if (args.overlap != null) spec.overlap = Math.min(Math.max(0, Number(args.overlap) || 0), 400);
  spec.settleMs = Math.min(Math.max(0, Number(args.settleMs) || 0), 3000);
  return spec;
}

async function pageScrollOp(args) {
  const spec = scrollSpec(args);
  const tab = await resolveTab(args);
  const level = await access(tab); // not_shared / origin_blocked / policy_pending, like read
  const origin = originOf(tab.url || "");
  if (!(await hasHostPermission(origin))) {
    throw new OpError("permission_needed", `chromecli has no host access to ${origin}.`, { tab: tab.id, origin });
  }
  // 0.11.1: the target can be in any frame the share covers. One frame: one call, as before. Several: survey them
  // (auto), or find the --container / --into match, then scroll in that frame only.
  const fr = await tabFrames(tab, origin);
  const where = { tab: tab.id, origin };
  let frame = fr.allowed[0];
  let run = spec;
  let auto = false;
  let surveyed = null;
  if (args.frameId != null && args.frameId !== 0) {
    frame = fr.allowed.find((f) => f.frameId === args.frameId);
    if (!frame) { const e = frameError(args.frameId, fr); e.extra = { ...where, ...e.extra }; throw e; }
  } else if (fr.allowed.length > 1 && spec.path == null) {
    if (spec.container) {
      const runs = await injectFrames(tab, origin, fr.allowed, pageScroll, { locate: spec.container });
      surveyed = runs;
      const top = runs.find((r) => r.frameId === 0);
      if (top.error && top.error.code === "bad_target") throw top.error;
      const ok = runs.filter((r) => r.result);
      const matches = ok.reduce((n, r) => n + (r.result.matches || 0), 0);
      const scroll = ok.flatMap((r) => (r.result.scrollable || []).map((c) => ({ ...c, frameId: r.frameId, frame: r })));
      const extra = () => ({ ...where, skipped: skipsFrom(runs, fr) });
      if (!matches) throw new OpError("element_not_found", "No element matches --container in any frame chromecli can read.", { ...extra(), reason: "no_match", matches: 0 });
      if (scroll.length > 1) {
        throw new OpError("ambiguous_target", `--container matches ${scroll.length} scrollable elements; give a narrower selector.`,
          { ...where, reason: "multiple_matches", count: scroll.length, matches,
            candidates: scroll.slice(0, 5).map((c) => ({ frameId: c.frameId, description: c.description, scrollHeight: c.scrollHeight, clientHeight: c.clientHeight })) });
      }
      if (!scroll.length) {
        const f = ok.find((r) => r.result.first)?.result.first;
        if (!ok.some((r) => r.result.shown)) {
          throw new OpError("element_not_found", `--container matches ${matches} element(s), none visible.`, { ...extra(), reason: "no_match", matches, shown: 0 });
        }
        throw new OpError("script_error", `--container matched ${f?.description || "an element"}, which doesn't scroll (scrollHeight ${f?.scrollHeight}, clientHeight ${f?.clientHeight}).`,
          { ...extra(), reason: "not_scrollable", matches, scrollHeight: f?.scrollHeight ?? null, clientHeight: f?.clientHeight ?? null, description: f?.description || null });
      }
      frame = scroll[0].frame;
      run = { ...spec, container: undefined, path: scroll[0].path };
      delete run.container;
    } else if (spec.into) {
      const runs = await injectFrames(tab, origin, fr.allowed, pageScroll, { locateInto: spec.into });
      const top = runs.find((r) => r.frameId === 0);
      if (top.error && top.error.code === "bad_target") throw top.error;
      const hit = runs.find((r) => r.result?.shown) || runs.find((r) => r.result?.matches);
      if (!hit) throw new OpError("element_not_found", "No element matches --into in any frame chromecli can read.", { ...where, reason: "no_match", matches: 0, skipped: skipsFrom(runs, fr) });
      frame = hit;
    } else {
      const runs = await injectFrames(tab, origin, fr.allowed, pageScroll, { survey: true });
      surveyed = runs;
      const top = runs.find((r) => r.frameId === 0);
      if (top.error) throw top.error;
      const surveys = new Map(runs.filter((r) => r.result).map((r) => [r.frameId, r.result]));
      const reports = new Map(runs.filter((r) => r.result?.diag).map((r) => [r.frameId, r.result.diag]));
      const pick = pickScrollTarget(surveys, frameGeometry(reports, fr.rows).geo);
      frame = fr.allowed.find((f) => f.frameId === pick.frameId) || fr.allowed[0];
      run = { ...spec, path: pick.path };
      auto = true;
    }
  }
  const r = await injectOne(tab, origin, frame, pageScroll, run);
  if (r.error) {
    if (r.diag || surveyed) r.error.extra = { ...r.error.extra, skipped: skipsFrom(surveyed || [r], fr, r.diag ? new Map([[r.frameId, r.diag]]) : null) };
    throw r.error;
  }
  const out = r.result;
  const st = await getPolicyState();
  const topUrl = frame.frameId === 0 ? out.url : (await chrome.tabs.get(tab.id).catch(() => ({ url: "" }))).url;
  if (originOf(topUrl) !== origin || denied(topUrl, st.active)) {
    await revoke(tab.id, "origin changed during scroll");
    throw new OpError("not_shared", `Tab ${tab.id} navigated away during the scroll.`, { tab: tab.id });
  }
  const { ok, diag, scrolls, ...rest } = out;
  const target = { ...rest.target, auto: auto || !!rest.target?.auto };
  if (frame.frameId !== 0) {
    target.frameId = frame.frameId;
    target.frameOrigin = frame.origin;
    target.frameUrl = shortUrl(frame.url);
    target.description = `${target.description} in iframe ${shortUrl(frame.url)}`.slice(0, 240);
  }
  const reply = { tab: tab.id, origin, share: level, op: "scroll", ...rest, target };
  if (frame.frameId !== 0) { reply.url = tab.url; reply.title = tab.title || rest.title; reply.frameUrl = shortUrl(out.url); }
  // Nothing scrolls, or frames were left out: say what chromecli couldn't look into.
  const skipped = skipsFrom(surveyed || [{ ...frame, result: out }], fr);
  if (skipped.length && (!scrolls || fr.skipped.length)) reply.skipped = skipped;
  if (!scrolls) reply.scrollable = false;
  return reply;
}

// The skipped list from per-frame page runs (each run's diag) and the frames the share left out.
function skipsFrom(runs, fr, extraReports = null) {
  const { failed, reports } = frameRuns(runs, fr);
  if (extraReports) for (const [k, v] of extraReports) reports.set(k, v);
  const { geo, unmatched } = frameGeometry(reports, fr.rows);
  return skippedList({ frameSkips: fr.skipped, failed, unmatched, reports, geo });
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
  const ex = denyException(url, st.active);
  await auditAdd({ op: `auto-share-${level}`, tab: tabId, origin, outcome: "ok", ...(ex ? { denyExcept: ex } : {}) });
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
  const g = reasons.length && canSkip(reasons) ? await findGrant("open", { origin, url }) : {};
  if (g.used) await grantUsedSideEffects(g.used, { op: "open", name: url, origin, tab: null });
  if (reasons.length && !g.used) {
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
    gated: reasons.length > 0, reasons, confirmedInBrowser, ...grantResult(g.used), dispatched: true, verified: true };
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
  const g = reasons.length && canSkip(reasons) ? await findGrant("navigate", { origin, url }) : {};
  if (g.used) await grantUsedSideEffects(g.used, { op: "navigate", name: url, origin, tab: tab.id });
  if (reasons.length && !g.used) {
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
    gated: reasons.length > 0, reasons, confirmedInBrowser, ...grantResult(g.used), dispatched: true, verified: true };
}

// ---- page actions (P3)
function normTarget(t, optional) {
  const tt = t && typeof t === "object" ? t : {};
  const given = ["selector", "ref", "text", "find"].filter((k) => tt[k] != null && tt[k] !== "");
  if (given.length === 0 && optional) return null;
  if (given.length !== 1) throw new OpError("bad_target", "Give exactly one target: selector, ref, text (+ role), or find.");
  const k = given[0];
  if (k === "ref" && !parseFrameRef(String(tt.ref))) {
    throw new OpError("bad_target", `${tt.ref} isn't an element ref (eN or fN:eN, from read --elements or find).`, { reason: "bad_ref" });
  }
  return k === "text" ? { text: String(tt.text), ...(tt.role ? { role: String(tt.role) } : {}) } : { [k]: String(tt[k]) };
}

// Page actions act in the top frame. 0.11.3: a frame ref ("f12:e3", from read --elements or find) is allowed only
// when (a) Kevin shared the tab Act (or it inherited Act; never a bot-opened tab), (b) the frame's origin is exactly
// in frame_act_allow and isn't denied, and (c) chromecli has host access to that origin. Anything else: bad_target
// reason frame_ref (policy_pending while a pending change would allow it; permission_needed without host access).
// Returns {frameId, frameOrigin, ref (the frame-local eN)}; {frameId: 0} for top-frame refs.
async function frameTarget(tab, origin, ref) {
  const f = parseFrameRef(ref);
  if (!f || f.frameId === 0) return { frameId: 0, ref: f ? f.ref : ref };
  const refuse = (why, extra = {}) => new OpError("bad_target", `${ref} is in iframe ${f.frameId}: ${why}`,
    { tab: tab.id, origin, reason: "frame_ref", frameId: f.frameId, ref, ...extra });
  const kind = await tabKind(tab);
  const st = await getPolicyState();
  const fr = await tabFrames(tab, origin);
  const row = fr.allowed.find((x) => x.frameId === f.frameId) || fr.skipped.find((x) => x.frameId === f.frameId);
  if (!row) {
    throw new OpError("script_error", `No frame ${f.frameId} in this tab (it navigated or closed); read it again.`,
      { tab: tab.id, origin, reason: "frame_unreachable", frameId: f.frameId });
  }
  const fo = row.origin;
  const listed = (st.active.frame_act_allow || []).includes(fo);
  if (kind !== "act") {
    throw refuse(kind === "bot" ? "frames are acted in only on tabs Kevin shared Act (this tab was opened by an agent)."
      : "frames are acted in only on tabs Kevin shared Act.", { frameOrigin: fo, tabKind: kind });
  }
  if (!listed) {
    if (fo && st.pending?.loosening.some((l) => l.kind === "frame_act_allow_added" && l.value === fo)) {
      throw new OpError("policy_pending", `Acting in ${fo} frames waits on a pending policy change (Kevin: Accept in the popup).`,
        { tab: tab.id, origin, frameId: f.frameId, frameOrigin: fo });
    }
    throw refuse(`${fo || "its origin"} isn't in frame_act_allow (chromecli acts only in the top frame and listed frame origins).`,
      { frameOrigin: fo });
  }
  if (row.reason === "denied") throw refuse(`${fo} is denied; no frame_act_allow entry overrides the deny list.`, { frameOrigin: fo, frameReason: "denied" });
  if (row.reason === "no_host_permission") {
    throw new OpError("permission_needed", `chromecli has no host access to ${fo} (frame ${f.frameId}).`,
      { tab: tab.id, origin, frameId: f.frameId, frameOrigin: fo, reason: "no_host_permission",
        hint: "Kevin: Accept the policy change in the popup (it asks Chrome for access), or set the tab to Act again in the popup." });
  }
  if (row.reason) throw refuse(`the frame is skipped (${row.reason}).`, { frameOrigin: fo, frameReason: row.reason });
  if (!row.frameAct) throw refuse(`${fo} frames can't be acted in here.`, { frameOrigin: fo });
  return { frameId: f.frameId, frameOrigin: fo, ref: f.ref };
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

// executeScript resolved without a value: the frame went away, or the page script threw before returning.
function noResultMessage(res) {
  const e = res?.error;
  const m = e ? String(e.message || e).slice(0, 300) : "";
  return m ? `In-page error: ${m}` : "The page returned nothing (it navigated or reloaded during the call, or the page script was blocked).";
}

async function inject(tab, origin, func, spec, { afterDispatch = false, frameId = 0 } = {}) {
  let res;
  try {
    const target = frameId ? { tabId: tab.id, frameIds: [frameId] } : { tabId: tab.id };
    [res] = await chrome.scripting.executeScript({ target, world: "ISOLATED", func, args: [spec] });
  } catch (e) {
    const m = String(e?.message || e);
    if (/permission|cannot access|must request/i.test(m)) throw new OpError("permission_needed", m, { tab: tab.id, origin, reason: "no_host_permission" });
    if (afterDispatch) return { ok: true, dispatched: true, lost: m };
    throw new OpError("script_error", m, { tab: tab.id, origin, reason: "inject_failed" });
  }
  const out = res?.result;
  if (!out) {
    if (afterDispatch) return { ok: true, dispatched: true, lost: "no result" };
    throw new OpError("script_error", noResultMessage(res), { tab: tab.id, origin, reason: "no_result" });
  }
  if (!out.ok) {
    // 0.10.2 spread the reply with code/message set to undefined, which wiped them from the error (a bare script_error).
    const { ok, code, message, diag, ...extra } = out;
    throw new OpError(code || "script_error", message || "Page action failed.", { tab: tab.id, origin, ...(frameId ? { frameId } : {}), ...extra });
  }
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
  // 0.11.3: a frame ref acts in that frame only under frameTarget's rules (else frame_ref, nothing dispatched).
  let frame = { frameId: 0 };
  if (target?.ref != null) {
    frame = await frameTarget(tab, origin, target.ref);
    target = { ref: frame.ref };
  }
  const fopt = { frameId: frame.frameId };
  const fwhere = frame.frameId ? { frameId: frame.frameId, frameOrigin: frame.frameOrigin } : {};
  const spec = { mode: "describe", op, target, key: args.key };
  const d = await inject(tab, origin, pageAction, spec, fopt);
  const element = d.element;
  const extra = [];
  if ((op === "click" || op === "press") && j.use) {
    try {
      const resp = await callJev(globalThis.fetch, j.cfg.key, classifyBody({ model: j.cfg.model,
        page: { origin: frame.frameOrigin || origin, title: tab.title || "" }, element: classifyElement(element) }));
      const c = parseClassify(resp, j.cfg.floor);
      const gate = classifyGate(c, kind, j.cfg);
      if (gate) extra.push(gate);
      jev = { ...jev, used: true, choice: c.choice, confidence: c.confidence };
      // What Jev said, shown even when it isn't a gate (e.g. "safe" below the floor on a tab Kevin shared).
      jevInfo = { reason: `jev:${c.choice}`, choice: c.choice, confidence: c.confidence, gated: !!gate,
        policy: kind === "act" ? `act-shared: gates unless safe at >= ${j.cfg.actSafeFloor}`
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
  const where = { tab: tab.id, origin, ...fwhere, element, reasons, jev, tabKind: kind, ...(jevInfo ? { jevInfo } : {}) };
  let confirmedInBrowser = false;
  // A pre-approval grant can answer the gate, unless a reason is never skippable (secret / payment fields, eval).
  const g = reasons.length && canSkip(reasons)
    ? await findGrant(op, { tab, origin, url: tab.url, element, value: args.value, key: args.key, spec, fingerprint: d.fingerprint,
      frameId: frame.frameId, frameOrigin: frame.frameOrigin || null }) : {};
  if (g.used) {
    if (await getPaused()) throw new OpError("paused", "Agents were paused; nothing was done.", where);
    await grantUsedSideEffects(g.used, { op, name: element?.name || element?.tag || "", origin, tab: tab.id });
  }
  if (reasons.length && !g.used) {
    const value = ["type", "fill", "select"].includes(op) ? displayValue(args.value, element) : null;
    await confirmOrThrow({ op, origin, url: tab.url, title: tab.title || "", element, reasons, value, ...fwhere,
      key: op === "press" ? String(args.key) : null, ...(g.near ? { grant: g.near } : {}) }, req, where);
    confirmedInBrowser = true;
    // Re-check every precondition after the confirm (spec 6.6).
    if (await getPaused()) throw new OpError("paused", "Agents were paused; nothing was done.", where);
    const again = await pagePre(args);
    if (frame.frameId) {
      const f2 = await frameTarget(again.tab, origin, `f${frame.frameId}:${frame.ref}`);
      if (f2.frameOrigin !== frame.frameOrigin) throw new OpError("script_error", "The frame navigated during the confirm; nothing was done.", { ...where, reason: "frame_unreachable" });
    }
  }
  if (op === "click" || op === "press") noteAction(tab.id);
  const t0 = Date.now();
  const done = await inject(tab, origin, pageAction, { ...spec, mode: "do", value: args.value, fingerprint: d.fingerprint },
    { afterDispatch: true, ...fopt });
  const out = { tab: tab.id, origin, ...fwhere, op, element, gated: reasons.length > 0, reasons, confirmedInBrowser, ...grantResult(g.used), dispatched: true,
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
      const [r] = await chrome.scripting.executeScript({ target: frame.frameId ? { tabId: tab.id, frameIds: [frame.frameId] } : { tabId: tab.id },
        world: "ISOLATED", func: pageProbe, args: [{ text: String(verify.text) }] }).catch(() => [null]);
      return !!r?.result?.found;
    }, 10000, 250);
    if (!ok) failed.push("text");
  }
  if (checks.length) out.verified = failed.length === 0;
  out.checks = checks;
  if (op === "click" || op === "press") out.openedTabs = await collectOpened(tab.id, t0);
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

const candidateRow = (el, score, source) => ({ ref: el.id, score, source, tag: el.tag, role: el.role || null, name: el.name || "",
  ...(el.frame ? { frame: el.frame } : {}) });

// One snapshot across the frames the share covers (0.11.1): subframe ids become "f<frameId>:e<n>".
async function snapshotFrames(tab, origin, spec) {
  const fr = await tabFrames(tab, origin);
  if (fr.allowed.length === 1) return { ...(await inject(tab, origin, pageSnapshot, spec)), skipped: fr.skipped };
  const runs = await injectFrames(tab, origin, fr.allowed, pageSnapshot, spec);
  const top = runs.find((r) => r.frameId === 0);
  if (top.error) throw top.error;
  const elements = [];
  for (const r of runs) {
    if (!r.result) continue;
    for (const el of r.result.elements || []) elements.push(r.frameId ? { ...el, id: `f${r.frameId}:${el.id}`, frame: r.frameId } : el);
  }
  const failed = frameRuns(runs, fr).failed;
  return { ...top.result, elements, skipped: [...fr.skipped, ...failed] };
}

async function locate(tab, origin, intent, j) {
  if (!intent.trim()) throw new OpError("bad_target", "find needs a description (intent).");
  const snap = await snapshotFrames(tab, origin, { max: 300 });
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
  return { list, pick, jev, skipped: snap.skipped || [] };
}

async function pageFind(args) {
  const { tab, origin } = await pagePre(args, "read");
  const j = await jevFor(args.jev);
  const limit = Math.min(Math.max(1, Number(args.limit) || 5), 50);
  const r = await locate(tab, origin, String(args.intent || ""), j);
  return { tab: tab.id, origin, results: r.list.slice(0, limit), pick: r.pick,
    jev: { mode: j.mode, ...(j.why ? { reason: j.why } : {}), ...r.jev }, ...(r.skipped.length ? { skipped: r.skipped } : {}) };
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
  checkExpect(tab, args.expect, false);
  const ms = Math.min(Math.max(100, Number(args.timeoutMs) || 15000), 120000);
  const probe = args.selector ? { selector: String(args.selector) } : args.text != null ? { text: String(args.text) } : null;
  if (!probe && !args.urlPrefix) throw new OpError("bad_target", "wait needs selector, text, or urlPrefix.");
  const start = Date.now();
  let lastErr = null;
  const ok = await waitFor(async () => {
    const t = await chrome.tabs.get(tab.id);
    await access(t, "read");
    if (args.urlPrefix) return String(t.url || "").startsWith(String(args.urlPrefix));
    // Any frame the share covers (0.11.1); a bad selector is reported from the top frame.
    const fr = await tabFrames(t, origin);
    const runs = await Promise.all(fr.allowed.map((f) => chrome.scripting.executeScript({ target: { tabId: tab.id, frameIds: [f.frameId] },
      world: "ISOLATED", func: pageProbe, args: [probe] }).then(([r]) => ({ frameId: f.frameId, r }), () => ({ frameId: f.frameId, r: null }))));
    const top = runs.find((x) => x.frameId === 0)?.r;
    if (top?.result && top.result.ok === false) { lastErr = top.result; return true; }
    return runs.some((x) => x.r?.result?.found);
  }, ms, 250);
  if (lastErr) throw new OpError(lastErr.code || "bad_target", lastErr.message || "Bad probe.", { tab: tab.id, origin, ...(lastErr.reason ? { reason: lastErr.reason } : {}) });
  if (!ok) throw new OpError("timeout", `Waited ${ms / 1000}s; the condition didn't hold.`, { tab: tab.id, origin });
  return { tab: tab.id, origin, found: true, waitedMs: Date.now() - start };
}

// ---- pre-approval grants (0.10.0)
// Live grants for (op, origin); expired ones are pruned on the way.
async function liveGrants() {
  const now = Date.now();
  const all = await getGrants();
  const live = Object.values(all).filter((g) => isLive(g, now));
  if (live.length !== Object.keys(all).length) {
    await withLock(async () => {
      const cur = await getGrants();
      for (const [id, g] of Object.entries(cur)) if (!isLive(g, Date.now())) delete cur[id];
      await setGrants(cur);
    });
  }
  return live;
}

// The grant that answers this gate, if any: {used} (already consumed) or {near} (scope matched, amount didn't).
async function findGrant(op, c) {
  const now = Date.now();
  const cands = (await liveGrants()).filter((g) => g.op === op && g.origin === c.origin && (g.frameOrigin || null) === (c.frameOrigin || null));
  if (!cands.length) return {};
  let insp = {};
  if (PAGE_GRANT_OPS.includes(op)) {
    const selectors = [...new Set(cands.map((g) => g.target?.selector).filter(Boolean))];
    const amountSelectors = [...new Set(cands.map((g) => g.amountSelector).filter(Boolean))];
    const autoAmount = cands.some((g) => g.maxAmount != null && !g.amountSelector);
    if (selectors.length || amountSelectors.length || autoAmount) {
      try {
        insp = await inject(c.tab, c.origin, pageAction, { ...c.spec, mode: "inspect", selectors, amountSelectors, autoAmount },
          { frameId: c.frameId || 0 });
      } catch { return {}; } // can't inspect: no grant, Kevin confirms
      if (c.fingerprint && insp.fingerprint && insp.fingerprint !== c.fingerprint) return {}; // the page changed
    }
  }
  let near = null;
  for (const g of cands) {
    const m = grantMatches(g, { op, origin: c.origin, frameOrigin: c.frameOrigin || null, now, element: c.element, value: c.value, key: c.key, url: c.url,
      selectorHit: g.target?.selector ? insp.selectorHits?.[g.target.selector] === true : undefined });
    if (!m.ok) continue;
    const texts = g.maxAmount == null ? [] : (insp.amountTexts?.[g.amountSelector || "_auto"] || []);
    const amt = amountCheck(g, texts);
    if (!amt.ok) {
      near = near || { id: g.id, quote: g.quote, maxAmount: g.maxAmount, currency: g.currency, amount: amt.amount,
        why: amt.why === "over_cap" ? "The amount on the page is over the pre-approved cap." : "chromecli couldn't read an amount on the page." };
      continue;
    }
    const usesLeft = await consumeGrant(g.id);
    if (usesLeft != null) return { used: { ...g, usesLeft, amount: amt.amount } };
  }
  return near ? { near } : {};
}

// Decrement a grant's uses (deleting it at 0). Returns uses left, or null if it was gone / spent meanwhile.
function consumeGrant(id) {
  return withLock(async () => {
    const grants = await getGrants();
    const g = grants[id];
    if (!isLive(g)) return null;
    g.usesLeft -= 1;
    if (g.usesLeft <= 0) delete grants[id]; else grants[id] = g;
    await setGrants(grants);
    return Math.max(0, g.usesLeft);
  });
}

function grantResult(g) {
  if (!g) return {};
  return { confirmedBy: `grant:${g.id}`, grant: { id: g.id, quote: g.quote, usesLeft: g.usesLeft,
    ...(g.amount != null ? { amount: g.amount, currency: g.currency, maxAmount: g.maxAmount } : {}) } };
}

async function grantUsedSideEffects(g, { op, name, origin, tab }) {
  const amount = g.amount != null ? fmtAmount(g.amount, g.currency) : "";
  await auditAdd({ op: `${op} (pre-approved)`, tab, origin, outcome: `grant:${g.id}` });
  try {
    await chrome.notifications?.create(`chromecli-grant-${g.id}-${Date.now()}`, {
      type: "basic", iconUrl: "icons/icon128.png", title: "chromecli",
      message: `Agent used your pre-approval: ${op} ${String(name).slice(0, 80)} on ${String(origin).replace(/^https?:\/\//, "")}${amount ? ` (${amount})` : ""}`,
      contextMessage: `“${g.quote.slice(0, 120)}”`, priority: 1 });
  } catch { /* notifications unavailable */ }
}

// 0.11.5: the tab a grant is for (title, URL path, store), shown to Jev. --tab names it (it must be on the grant's
// origin and visible to chromecli: shared or bot-opened); otherwise the one chromecli-visible tab (or several on the same
// store) on that origin. None found -> no page context.
async function grantContext(origin, tabId) {
  const bot = await getBotTabs();
  const visible = async (tab) => !!bot[tab.id] || await access(tab).then(() => true, () => false);
  if (tabId != null) {
    if (!Number.isInteger(tabId)) throw new OpError("bad_request", "--tab must be a tab id.");
    const tab = await resolveTab({ tab: tabId });
    if (!(await visible(tab))) throw new OpError("not_shared", `Tab ${tab.id} is not shared or bot-opened.`, { tab: tab.id });
    if (originOf(tab.url || "") !== origin) {
      throw new OpError("bad_request", `Tab ${tab.id} is on ${originOf(tab.url || "") || "another page"}, not ${origin}.`, { tab: tab.id });
    }
    return pageContext(tab.url, tab.title);
  }
  const ctxs = [];
  for (const tab of await chrome.tabs.query({})) {
    if (originOf(tab.url || "") !== origin || !(await visible(tab))) continue;
    const c = pageContext(tab.url, tab.title);
    if (c) ctxs.push(c);
  }
  if (!ctxs.length) return null;
  if (ctxs.length === 1) return ctxs[0];
  const stores = new Set(ctxs.map((c) => c.store || ""));
  // several tabs: only the shared store (no title / path, which differ) when they agree on one
  if (stores.size === 1 && ctxs[0].store) {
    const { store, pathPrefix } = ctxs[0];
    return { url: `${origin}${pathPrefix || ""}`, path: pathPrefix || "", store, ...(pathPrefix ? { pathPrefix } : {}) };
  }
  return null;
}

// Per-clause uses already granted (session storage; revoking doesn't refund, Kevin restates to re-approve).
const getLedger = async () => (await chrome.storage.session.get("clauseLedger")).clauseLedger || {};
const ledgerKey = (quote, clauseId) => `${quoteKey(quote)}#${clauseId}`;

async function grantAdd(args) {
  const r = await addGrants([args], { quote: args.quote, userApproved: args.userApproved, tab: args.tab, partial: false });
  const x = r.results[0];
  return { op: "grant.add", added: true, grant: x.grant, jevCheck: x.jevCheck, ...(x.clause ? { clause: x.clause } : {}),
    confirmedInBrowser: false };
}

async function grantAddBatch(args) {
  const list = Array.isArray(args.grants) ? args.grants : null;
  if (!list || !list.length) throw new OpError("bad_request", "grant.add_batch needs a non-empty grants list.");
  if (list.length > BATCH_MAX) throw new OpError("bad_request", `grant.add_batch takes at most ${BATCH_MAX} grants.`);
  const r = await addGrants(list, { quote: args.quote, userApproved: args.userApproved, tab: args.tab, partial: args.partial === true });
  return { op: "grant.add_batch", ...r, confirmedInBrowser: false };
}

// The shared path for grant.add (one grant) and grant.add_batch: validate all, then Jev. A quote with a list of items
// (preamble: item, item, ...) is checked clause by clause; any other quote, grant by grant as before. Fail closed.
async function addGrants(list, { quote, userApproved, tab, partial }) {
  const now = Date.now();
  const gs = [];
  for (const [i, a] of list.entries()) {
    try {
      gs.push(validateGrant({ ...(a && typeof a === "object" ? a : {}), quote, userApproved }, { now }));
    } catch (e) {
      if (e instanceof GrantError) throw new OpError(e.code, list.length > 1 ? `grant ${i + 1}: ${e.message}` : e.message, list.length > 1 ? { index: i + 1 } : {});
      throw e;
    }
  }
  const st = await getPolicyState();
  for (const g of gs) {
    const why = denied(`${g.origin}/`, st.active) || (g.urlPrefix ? denied(g.urlPrefix, st.active) : null)
      || (g.frameOrigin ? denied(`${g.frameOrigin}/`, st.active) : null);
    if (why) throw new OpError("origin_blocked", `${g.origin} is denied (${why}); no grant can cover it.`, { origin: g.origin });
  }
  // page context per origin (title, path, store); a store pins the grant to that store's pages
  const ctxBy = {};
  for (const g of gs) {
    if (!(g.origin in ctxBy)) ctxBy[g.origin] = await grantContext(g.origin, tab ?? null);
    const c = ctxBy[g.origin];
    if (c) { g.context = c; if (c.pathPrefix) g.pathPrefix = c.pathPrefix; }
  }
  const origin0 = gs[0].origin;
  const cfg = await getJev();
  if (!cfg.configured) {
    await auditAdd({ op: "grant-refused", tab: null, origin: origin0, outcome: "jev_not_configured" });
    throw new OpError("jev_not_configured", "Pre-approvals need Jev to check the quote against the grant, and no Jev key is set. Nothing was granted.",
      { origin: origin0, hint: "Kevin: save a TypeSafe key in chromecli's options. Until then every risky action asks you in Chrome." });
  }
  const split = splitQuote(gs[0].quote);
  let results;
  try {
    results = split.multi ? await clauseVerdicts(gs, split, cfg) : await singleVerdicts(gs, cfg);
  } catch (e) {
    if (e instanceof OpError) throw e;
    await auditAdd({ op: "grant-refused", tab: null, origin: origin0, outcome: "jev_error" });
    throw new OpError("jev_error", `Jev couldn't check the grant${gs.length > 1 ? "s" : ""} (${String(e?.message || e).slice(0, 200)}). Nothing was granted.`, { origin: origin0 });
  }
  const okOnes = results.filter((r) => r.ok);
  const refused = results.filter((r) => !r.ok);
  const view = (r) => ({ index: r.index, op: r.g.op, target: r.g.target?.text ?? r.g.target?.selector ?? r.g.urlPrefix ?? r.g.origin,
    verdict: r.verdict, confidence: r.confidence, reason: r.reason, ...(r.clause ? { clause: r.clause } : {}) });
  for (const r of refused) {
    post({ event: "grant_refused", op: r.g.op, origin: r.g.origin, quote: r.g.quote, verdict: r.verdict, confidence: r.confidence,
      reason: r.reason, ...(r.clause ? { clause: r.clause.text } : {}) });
  }
  if (refused.length && (!partial || !okOnes.length)) {
    await auditAdd({ op: "grant-refused", tab: null, origin: origin0, outcome: `${refused.length}/${results.length} refused` });
    const code = refused.some((r) => r.verdict === "overreach") ? "grant_overreach" : "grant_unclear";
    const first = refused[0];
    const msg = gs.length === 1 ? first.reason
      : `${refused.length} of ${gs.length} grants refused; nothing was granted${partial ? "" : " (all or nothing; --partial stores the authorized ones)"}. First: grant ${first.index}: ${first.reason}`;
    throw new OpError(code, msg, { origin: origin0, ...(gs.length === 1 ? { jevCheck: first.jevCheck } : {}),
      ...(split.multi ? { preamble: split.preamble } : {}), results: results.map(view),
      hint: "Nothing was granted. Ask Kevin to approve exactly this (or let the action ask him in Chrome)." });
  }
  const stored = await withLock(async () => {
    const grants = await getGrants();
    if (Object.keys(grants).length + okOnes.length > 50) throw new OpError("bad_request", "Too many active pre-approvals (50); revoke some first.");
    const ledger = await getLedger();
    for (const [k, v] of Object.entries(ledger)) if (v.until <= Date.now()) delete ledger[k];
    const out = [];
    for (const r of okOnes) {
      const n = { ...r.g, jev: { verdict: r.verdict, confidence: r.confidence, narrowed: r.narrowed || [],
        ...(r.mapConfidence != null ? { mapConfidence: r.mapConfidence } : {}) }, ...(r.clause ? { clause: r.clause } : {}) };
      grants[n.id] = n;
      if (r.clause) {
        const k = ledgerKey(n.quote, r.clause.id);
        const until = Math.max(ledger[k]?.until || 0, n.expiresAt);
        ledger[k] = { granted: (ledger[k]?.granted || 0) + n.uses, until };
      }
      out.push({ r, n });
    }
    await setGrants(grants);
    await chrome.storage.session.set({ clauseLedger: ledger });
    return out;
  });
  for (const { r, n } of stored) {
    await auditAdd({ op: `grant-${n.op}`, tab: null, origin: n.origin, outcome: `${n.id} jev:${r.verdict}${r.clause ? ` clause:${r.clause.id}` : ""}` });
    post({ event: "grant_added", grant: n.id, op: n.op, origin: n.origin, quote: n.quote, verdict: r.verdict,
      confidence: r.confidence, narrowed: (r.narrowed || []).map((x) => x.field), ...(r.clause ? { clause: r.clause.text } : {}) });
  }
  const added = new Map(stored.map(({ r, n }) => [r.index, n]));
  // grant.add keeps the full grant view; the batch reply names the quote, preamble and page once, not per grant
  const slim = (n) => {
    if (gs.length === 1) return grantView(n);
    const { quote: _q, context: _c, clause: _cl, ...rest } = grantView(n);
    return rest;
  };
  return {
    added: stored.length, refused: refused.length, ...(split.multi ? { preamble: split.preamble, windowSec: split.windowSec } : {}),
    ...(gs.length > 1 && ctxBy[origin0] ? { page: ctxBy[origin0] } : {}),
    results: results.map((r) => ({ ...view(r), stored: added.has(r.index),
      ...(added.has(r.index) ? { grant: slim(added.get(r.index)) } : {}), jevCheck: r.jevCheck })),
  };
}

// No item list: each grant against the whole quote (the 0.10.1 check, incl. narrowing), one call per grant.
async function singleVerdicts(gs, cfg) {
  const out = [];
  for (const [i, g] of gs.entries()) {
    const v = parseScope(await callJev(globalThis.fetch, cfg.key, scopeBody({ model: cfg.model, grant: g })), g);
    out.push({ index: i + 1, g: v.grant, ok: v.ok, verdict: v.verdict, confidence: v.confidence, reason: v.reason, narrowed: v.narrowed,
      jevCheck: { verdict: v.verdict, confidence: v.confidence, reason: v.reason, narrowed: v.narrowed } });
  }
  return out;
}

// An item list: Jev maps every grant to one clause (one call); code checks each clause's uses / amount / window;
// Jev scores each remaining grant against the preamble plus only its clause (one call).
async function clauseVerdicts(gs, split, cfg) {
  const maps = parseMap(await callJev(globalThis.fetch, cfg.key, mapBody({ model: cfg.model, split, grants: gs })), split, gs.length);
  const ledger = await getLedger();
  const spent = {};
  const out = gs.map((g, i) => ({ index: i + 1, g, mapConfidence: maps[i].confidence }));
  const pairs = [];
  for (const [i, r] of out.entries()) {
    const m = maps[i];
    const c = split.clauses.find((x) => x.id === m.clauseId);
    if (!c) {
      Object.assign(r, { ok: false, verdict: "unclear", confidence: m.confidence,
        reason: m.rawChoice === "none" ? "Jev: no item in the quote approves this grant." : `Jev matched item ${m.rawChoice} only at ${m.confidence ?? "?"} (needs >= 0.8); treated as unclear.` });
      continue;
    }
    r.clause = { id: c.id, text: c.text, uses: c.uses ?? 1, ...(c.amount != null ? { amount: c.amount } : {}),
      ...(c.windowSec != null ? { windowSec: c.windowSec } : {}) };
    const lk = ledgerKey(r.g.quote, c.id);
    const used = (ledger[lk] && ledger[lk].until > Date.now() ? ledger[lk].granted : 0) + (spent[c.id] || 0);
    const chk = checkClause(r.g, c, { used });
    if (!chk.ok) { Object.assign(r, { ok: false, verdict: chk.verdict, confidence: null, reason: chk.reason }); continue; }
    r.g = chk.grant;
    spent[c.id] = (spent[c.id] || 0) + r.g.uses;
    pairs.push({ key: `g${i + 1}`, grant: r.g, clause: c, r });
  }
  if (pairs.length) {
    const resp = await callJev(globalThis.fetch, cfg.key, clauseScopeBody({ model: cfg.model, split, pairs }));
    for (const p of pairs) {
      const v = parseClauseScope(resp, p.key);
      Object.assign(p.r, { ok: v.ok, verdict: v.verdict, confidence: v.confidence, reason: v.reason });
    }
  }
  for (const r of out) r.jevCheck = { verdict: r.verdict, confidence: r.confidence, reason: r.reason, narrowed: [],
    ...(r.clause ? { clause: r.clause.text, mapConfidence: r.mapConfidence } : {}) };
  return out;
}

async function grantList() {
  return { grants: (await liveGrants()).sort((a, b) => a.createdAt - b.createdAt).map((g) => grantView(g)) };
}

async function grantRevoke(args) {
  if (args.all === true) return { op: "grant.revoke", revoked: await clearGrants("revoked by agent"), all: true };
  const id = String(args.id || "");
  if (!id) throw new OpError("bad_request", "grant.revoke needs an id or all: true.");
  let hit = false;
  await withLock(async () => {
    const grants = await getGrants();
    hit = !!grants[id];
    delete grants[id];
    await setGrants(grants);
  });
  if (!hit) throw new OpError("bad_target", `No active pre-approval ${id}.`);
  await auditAdd({ op: "grant-revoke", tab: null, origin: null, outcome: id });
  return { op: "grant.revoke", revoked: 1, id };
}

// ---- preview probe (0.10.0): read-only "would this target be found, and would it ask Kevin?" for --confirm-less
// previews. Never dispatches, never calls Jev, never consumes a grant.
async function pageProbeOp(args) {
  const op = String(args.op || "");
  if (!["click", "type", "fill", "select", "press"].includes(op)) throw new OpError("bad_request", "page.probe op must be click/type/fill/select/press.");
  const { tab, origin } = await pagePre(args, "read");
  const kind = await tabKind(tab);
  const base = { tab: tab.id, origin, op, tabKind: kind, share: (await getShares())[tab.id]?.level ?? null };
  try { checkExpect(tab, args.expect, false); } catch (e) {
    if (e instanceof OpError) return { ...base, found: null, wouldFail: e.code, message: e.message };
    throw e;
  }
  if (kind === "read") return { ...base, found: null, wouldFail: "not_act_shared", message: `Tab ${tab.id} isn't Share-act'd or bot-opened.` };
  let target;
  try { target = normTarget(args.target, op === "press"); } catch (e) { return { ...base, found: false, wouldFail: e.code, message: e.message }; }
  if (target?.find != null) {
    const r = await locate(tab, origin, String(target.find), { use: false, mode: "off" });
    if (!r.pick) return { ...base, found: false, wouldFail: "ambiguous_target", candidates: r.list.slice(0, 5) };
    target = { ref: r.pick };
  }
  let pframe = { frameId: 0 };
  if (target?.ref != null) {
    try { pframe = await frameTarget(tab, origin, target.ref); } catch (e) {
      if (!(e instanceof OpError)) throw e;
      return { ...base, found: false, wouldFail: e.code, message: e.message, ...(e.extra?.reason ? { reason: e.extra.reason } : {}) };
    }
    target = { ref: pframe.ref };
  }
  let d;
  try {
    d = await inject(tab, origin, pageAction, { mode: "describe", op, target, key: args.key }, { frameId: pframe.frameId });
  } catch (e) {
    if (!(e instanceof OpError)) throw e;
    return { ...base, found: false, wouldFail: e.code, message: e.message,
      ...(e.extra?.candidates ? { candidates: e.extra.candidates } : {}) };
  }
  const heuristic = gateReasons(op, d.element, { key: args.key });
  const jev = await getJev();
  const jevApplies = jev.configured && (op === "click" || op === "press") && args.jev !== "off";
  const bar = kind === "act" ? jev.actSafeFloor : jev.floor;
  return { ...base, ...(pframe.frameId ? { frameId: pframe.frameId, frameOrigin: pframe.frameOrigin } : {}), found: true, element: d.element, gateEstimate: {
    heuristic, wouldPrompt: heuristic.length ? true : jevApplies ? "depends_on_jev" : false,
    ...(jevApplies ? { jev: `Jev classifies at run time: no Jev prompt if it is >= ${bar} sure the ${op} is safe (${kind} tab).` } : {}),
    grantSkippable: heuristic.length ? canSkip(heuristic) : null } };
}

// ---- tabs opened by an action (0.10.0): a click/press on an act-shared tab that opens a tab (openerTabId) shares
// the new tab too: act if it's on the opener's origin, read otherwise, never on a denied origin.
const actionWindows = new Map(); // opener tabId -> until (ms)
const inheriting = new Map(); // new tabId -> { opener, openerOrigin, until, at, last }
const OPEN_WAIT_MS = () => (globalThis.__chromecliOpenWaitMs ?? 600);
function noteAction(tabId) { actionWindows.set(tabId, Date.now() + 15000); }

async function inheritShare(tabId, url) {
  const inh = inheriting.get(tabId);
  if (!inh) return;
  if (inh.until < Date.now()) { inheriting.delete(tabId); return; }
  if (await getPaused()) { inh.last = { share: null, why: "paused" }; return; }
  const st = await getPolicyState();
  const origin = originOf(url || "");
  if (!origin) return; // about:blank first; wait for the real URL
  if (denied(url, st.active)) { inh.last = { share: null, why: "origin_blocked" }; return; }
  const level = origin === inh.openerOrigin ? "act" : "read";
  if (!(await hasHostPermission(origin))) { inh.last = { share: null, why: "permission_needed" }; return; }
  let changed = false;
  await withLock(async () => {
    const shares = await getShares();
    const cur = shares[tabId];
    if (cur && cur.origin === origin && cur.level === level) return;
    shares[tabId] = { origin, level, at: Date.now(), inheritedFrom: inh.opener };
    await setShares(shares);
    changed = true;
  });
  inh.last = { share: level };
  if (changed) {
    post({ event: "share_changed", tab: tabId, origin, level, reason: `opened from tab ${inh.opener}` });
    const ex = denyException(url, st.active);
    await auditAdd({ op: `inherit-share-${level}`, tab: tabId, origin, outcome: `from tab ${inh.opener}`, ...(ex ? { denyExcept: ex } : {}) });
    await updateBadge(tabId);
  }
}

async function onTabCreated(tab) {
  const opener = tab?.openerTabId;
  if (opener == null || !((actionWindows.get(opener) || 0) > Date.now())) return;
  const s = (await getShares())[opener];
  if (!s || s.level !== "act") return;
  inheriting.set(tab.id, { opener, openerOrigin: s.origin, until: Date.now() + 20000, at: Date.now(), last: null });
  // The URL may have landed while we looked up the opener: use the tab's current one.
  const now = await chrome.tabs.get(tab.id).catch(() => tab);
  await inheritShare(tab.id, now.pendingUrl || now.url || "");
}

async function collectOpened(openerId, since) {
  await new Promise((r) => setTimeout(r, OPEN_WAIT_MS()));
  const mine = () => [...inheriting.entries()].filter(([, v]) => v.opener === openerId && v.at >= since);
  if (!mine().length) return [];
  // Give new tabs a moment to get a real URL (and their share).
  await waitFor(async () => {
    for (const [id] of mine()) {
      const t = await chrome.tabs.get(id).catch(() => null);
      if (t && !originOf(t.pendingUrl || t.url || "")) return false;
    }
    return true;
  }, 1500, 150);
  const shares = await getShares();
  const out = [];
  for (const [id, v] of mine()) {
    const t = await chrome.tabs.get(id).catch(() => null);
    const url = t ? (t.url || t.pendingUrl || "") : "";
    if (url && !shares[id]) await inheritShare(id, url);
    const share = (await getShares())[id]?.level ?? null;
    out.push({ id, url, share, ...(share ? {} : v.last?.why ? { shareSkipped: v.last.why } : {}) });
  }
  return out;
}

// ---- tab recording (0.12.1): chrome.tabCapture + an offscreen MediaRecorder (see recorder.js). No debugger.
// record.start needs --confirm AND Kevin's Start in the confirm window; Chrome also needs the tab's activeTab grant
// (Kevin clicked the chromecli icon on it since it last changed site). Without the grant the request waits for
// Kevin's Record in the popup on that tab (state awaiting_popup). Pause, Unshare, closing the tab, or the tab
// leaving its origin stop the recording and finalize the file.
const recordings = new Map(); // recId -> rec (active ones plus the last few finished)
const REC_KEEP = 20;
const recFor = (tabId) => [...recordings.values()].find((r) => r.tab === tabId && isActive(r)) || null;
const OFFSCREEN = "offscreen.html";

// Keep finished recordings (newest last) for status, at most REC_KEEP.
function recNote(rec) {
  recordings.delete(rec.recId);
  recordings.set(rec.recId, rec);
  const done = [...recordings.values()].filter((r) => !isActive(r));
  for (const r of done.slice(0, Math.max(0, done.length - REC_KEEP))) recordings.delete(r.recId);
}

async function hasOffscreen() {
  try {
    if (chrome.runtime.getContexts) {
      const ctx = await chrome.runtime.getContexts({ contextTypes: ["OFFSCREEN_DOCUMENT"], documentUrls: [chrome.runtime.getURL(OFFSCREEN)] });
      return ctx.length > 0;
    }
    return !!(await chrome.offscreen?.hasDocument?.());
  } catch { return false; }
}

let offscreenCreating = null;
async function ensureOffscreen() {
  if (await hasOffscreen()) return;
  if (!offscreenCreating) {
    offscreenCreating = chrome.offscreen.createDocument({ url: OFFSCREEN, reasons: ["USER_MEDIA"],
      justification: "Record a tab Kevin shared (chromecli record start) with tabCapture + MediaRecorder." })
      .finally(() => { offscreenCreating = null; });
  }
  await offscreenCreating;
}

async function closeOffscreenIfIdle() {
  if ([...recordings.values()].some((r) => isActive(r) && r.state !== "awaiting_popup")) return;
  try { if (await hasOffscreen()) await chrome.offscreen.closeDocument(); } catch { /* gone */ }
}

const toOffscreen = (msg) => chrome.runtime.sendMessage({ target: "offscreen", ...msg });

// The share checks for recording: access() (paused / deny / Off) plus Kevin-shared only (recorder.js refusal).
async function checkRecordable(tab) {
  await access(tab, "read");
  const [shares, bot] = [await getShares(), await getBotTabs()];
  const why = refusal({ share: shares[tab.id], bot: !!bot[tab.id] });
  if (why) throw new OpError(why.code, why.message, { tab: tab.id, origin: originOf(tab.url || ""), reason: why.reason });
  return shares[tab.id].level;
}

// The capture size: the tab's viewport in device pixels (capped at 4K), so text stays sharp.
async function captureSize(tab) {
  let dpr = 2;
  try {
    const [res] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: () => window.devicePixelRatio });
    if (Number(res?.result) > 0) dpr = Number(res.result);
  } catch { /* no access: assume a Retina display */ }
  const w = Math.round((tab.width || 1440) * dpr);
  const hh = Math.round((tab.height || 900) * dpr);
  const k = Math.min(1, 3840 / w, 2160 / hh);
  return { maxWidth: Math.max(2, Math.round(w * k)), maxHeight: Math.max(2, Math.round(hh * k)) };
}

async function recordStart(args, req) {
  if (!validRecId(args.recId)) throw new OpError("bad_request", "record.start needs a recId from chromecli-host.");
  let opts;
  try { opts = recordOptions(args); } catch (e) { throw new OpError(e.code || "bad_request", e.message); }
  if (!chrome.tabCapture?.getMediaStreamId || !chrome.offscreen?.createDocument) {
    throw new OpError("permission_needed", "This chromecli extension has no tabCapture / offscreen permission yet.",
      { hint: "Kevin: reload the chromecli extension in chrome://extensions (0.12.1 adds tabCapture and offscreen)." });
  }
  const tab = await resolveTab(args);
  const level = await checkRecordable(tab);
  const origin = originOf(tab.url || "");
  const where = { tab: tab.id, origin };
  if (recFor(tab.id)) throw new OpError("bad_request", `Tab ${tab.id} is already being recorded (chromecli record status).`, { ...where, reason: "already_recording" });
  if (recordings.has(args.recId)) throw new OpError("bad_request", "That recId is taken.", where);
  const info = { op: "record", origin, url: tab.url, title: tab.title || "", reasons: [], record: opts };
  await confirmOrThrow(info, req, where);
  // Kevin clicked Start: check again (the tab may have moved on while the window was up).
  const now = await chrome.tabs.get(tab.id).catch(() => null);
  if (!now) throw new OpError("tab_not_found", `Tab ${tab.id} closed before the recording started.`, where);
  await checkRecordable(now);
  if (originOf(now.url || "") !== origin) throw new OpError("not_shared", `Tab ${tab.id} left ${origin}; not recording.`, where);
  if (recFor(tab.id)) throw new OpError("bad_request", `Tab ${tab.id} is already being recorded.`, { ...where, reason: "already_recording" });
  const rec = { recId: args.recId, tab: tab.id, origin, title: now.title || "", level, ...opts, state: "starting",
    createdAt: Date.now(), bytes: 0, chunks: 0, via: "confirm" };
  recordings.set(rec.recId, rec);
  await updateBadge(tab.id);
  let streamId;
  try {
    streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tab.id });
  } catch (e) {
    const m = String(e?.message || e);
    if (!GRANT_ERROR.test(m)) { await recFail(rec, `tabCapture: ${m}`); throw new OpError("capture_failed", `Chrome refused to capture tab ${tab.id}: ${m}`, where); }
    // Chrome wants the extension invoked on this tab: wait for Kevin's Record in the popup.
    rec.state = "awaiting_popup";
    rec.waitUntil = Date.now() + opts.waitSec * 1000;
    rec.waitTimer = setTimeout(() => recCancel(rec, "expired", "Kevin didn't press Record in time"), opts.waitSec * 1000);
    rec.waitTimer?.unref?.();
    await updateBadge(tab.id);
    chrome.notifications?.create?.(`chromecli-rec-${rec.recId}`, { type: "basic", iconUrl: "icons/icon128.png",
      title: "chromecli: one more click to record", message: `Open “${trunc(rec.title, 60)}”, click the chromecli icon, then Record.`,
      priority: 2 }).catch?.(() => {});
    post({ event: "rec_waiting", recId: rec.recId, tab: tab.id, origin });
    await auditAdd({ op: "record-wait", tab: tab.id, origin, outcome: "awaiting_popup" });
    return { ...recView(rec), confirmedInBrowser: true,
      hint: `Chrome only lets an extension capture a tab after Kevin clicks its icon on that tab. Kevin: switch to tab ${tab.id}, click the chromecli icon, press Record. Waiting up to ${opts.waitSec}s.` };
  }
  await beginCapture(rec, streamId, now);
  return { ...recView(rec), confirmedInBrowser: true };
}

async function beginCapture(rec, streamId, tab) {
  // Stopped (pause, unshare, ...) while Chrome was handing out the stream ID.
  if (!["starting", "awaiting_popup"].includes(rec.state)) {
    const why = rec.reason || "stopped";
    await recCancel(rec, "cancelled", why);
    throw new OpError("capture_failed", `Tab ${rec.tab}'s recording was stopped before it started (${why}).`, { tab: rec.tab, origin: rec.origin });
  }
  rec.state = "starting";
  const size = await captureSize(tab);
  let res;
  try {
    await ensureOffscreen();
    res = await toOffscreen({ type: "start", recId: rec.recId, streamId, fps: rec.fps, audio: rec.audio, chunkBytes: CHUNK_BYTES, ...size });
  } catch (e) {
    res = { ok: false, error: String(e?.message || e) };
  }
  if (!res?.ok) {
    await recFail(rec, res?.error || "the recorder didn't start");
    throw new OpError("capture_failed", `Couldn't start recording tab ${rec.tab}: ${res?.error || "no answer"}`, { tab: rec.tab, origin: rec.origin });
  }
  // Stopped (pause, unshare, ...) while the recorder was starting.
  if (rec.state !== "starting") { toOffscreen({ type: "stop", recId: rec.recId, reason: rec.reason || "stopped" }).catch(() => {}); return; }
  Object.assign(rec, { state: "recording", startedAt: Date.now(), mime: res.mime, width: res.width, height: res.height, audio: !!res.audio && rec.audio });
  rec.maxTimer = setTimeout(() => stopRecording(rec.recId, "max_duration").catch(() => {}), rec.maxSec * 1000);
  rec.maxTimer?.unref?.(); // (node tests only; Chrome's timers are numbers)
  post({ event: "rec_started", recId: rec.recId, tab: rec.tab, origin: rec.origin, mime: rec.mime, fps: rec.fps, audio: rec.audio,
    maxSec: rec.maxSec, width: rec.width, height: rec.height, via: rec.via });
  await auditAdd({ op: "record-start", tab: rec.tab, origin: rec.origin, outcome: "ok" });
  await updateBadge(rec.tab);
}

async function recFail(rec, error) {
  rec.state = "failed"; rec.error = String(error).slice(0, 300); rec.stoppedAt = Date.now();
  clearTimeout(rec.waitTimer);
  post({ event: "rec_stopped", recId: rec.recId, tab: rec.tab, origin: rec.origin, state: "failed", reason: "failed", error: rec.error, bytes: 0, durationMs: 0 });
  await auditAdd({ op: "record-stop", tab: rec.tab, origin: rec.origin, outcome: "failed" });
  recNote(rec);
  await updateBadge(rec.tab);
}

// A request that never started recording (expired, paused, unshared while waiting for the popup).
async function recCancel(rec, state, reason) {
  if (!isActive(rec)) return;
  clearTimeout(rec.waitTimer);
  Object.assign(rec, { state, reason, stoppedAt: Date.now() });
  post({ event: "rec_stopped", recId: rec.recId, tab: rec.tab, origin: rec.origin, state, reason, bytes: 0, durationMs: 0 });
  await auditAdd({ op: "record-stop", tab: rec.tab, origin: rec.origin, outcome: state });
  recNote(rec);
  await updateBadge(rec.tab);
}

// Stop and finalize; resolves with the recording once the last chunk went to the host.
async function stopRecording(recId, reason) {
  const rec = recordings.get(recId);
  if (!rec || !isActive(rec)) return rec || null;
  if (rec.state === "awaiting_popup") { await recCancel(rec, "cancelled", reason); return rec; }
  if (rec.state === "starting") { rec.reason = reason; rec.state = "stopping"; return rec; }
  if (rec.state === "recording") {
    rec.state = "stopping";
    rec.reason = reason;
    clearTimeout(rec.maxTimer);
    rec.finished ??= new Promise((res) => { rec.finish = res; });
    toOffscreen({ type: "stop", recId, reason }).then((res) => { if (res?.ok && !res.gone) recDone({ ...res, recId }); },
      () => recDone({ recId, reason, error: "the recorder went away" }));
    await updateBadge(rec.tab);
  }
  let timer;
  const timeout = new Promise((res) => { timer = setTimeout(() => res("timeout"), 20000); });
  const how = await Promise.race([rec.finished, timeout]);
  clearTimeout(timer);
  if (how === "timeout" && isActive(rec)) {
    await recDone({ recId, reason: rec.reason, error: "the recorder didn't finish in 20s" });
  }
  return rec;
}

async function stopAllRecordings(reason, filter = () => true) {
  const active = [...recordings.values()].filter((r) => isActive(r) && filter(r));
  return Promise.all(active.map((r) => stopRecording(r.recId, reason)));
}

// The offscreen recorder finished one (stop, tab closed, chunk not delivered): tell the host, which closes the file.
async function recDone(msg) {
  const rec = recordings.get(msg.recId);
  if (!rec || rec.state === "stopped" || rec.state === "failed") return;
  clearTimeout(rec.maxTimer);
  // The bytes the worker forwarded are what the host got (the recorder may count a chunk that wasn't delivered).
  const bytes = rec.chunks ? rec.bytes : (msg.bytes || 0);
  Object.assign(rec, { state: msg.error && !bytes ? "failed" : "stopped", stoppedAt: Date.now(),
    reason: rec.reason || msg.reason || "stopped", bytes, chunks: rec.chunks || msg.chunks || 0,
    frames: msg.frames ?? rec.frames, ...(msg.error ? { error: String(msg.error).slice(0, 300) } : {}) });
  post({ event: "rec_stopped", recId: rec.recId, tab: rec.tab, origin: rec.origin, state: rec.state, reason: rec.reason,
    bytes: rec.bytes, chunks: rec.chunks, durationMs: rec.startedAt ? rec.stoppedAt - rec.startedAt : 0, mime: rec.mime || null,
    ...(rec.error ? { error: rec.error } : {}) });
  await auditAdd({ op: "record-stop", tab: rec.tab, origin: rec.origin, outcome: rec.reason });
  recNote(rec);
  rec.finish?.(rec);
  await updateBadge(rec.tab);
  await closeOffscreenIfIdle();
}

// Messages from offscreen.html. Chunks are forwarded in order, synchronously (no await before post).
function offscreenMessage(msg) {
  if (msg?.type === "rec_chunk") {
    const rec = recordings.get(msg.recId);
    if (!rec || !["recording", "stopping"].includes(rec.state) || typeof msg.data !== "string" || !Number.isInteger(msg.seq)) {
      return { ok: false, error: "no such recording" };
    }
    if (msg.data.length > Math.ceil(CHUNK_BYTES / 3) * 4 + 4) return { ok: false, error: "chunk too big" };
    if (!post({ event: "rec_chunk", recId: rec.recId, seq: msg.seq, data: msg.data })) {
      stopRecording(rec.recId, "host_disconnected").catch(() => {});
      return { ok: false, error: "chromecli-host isn't connected" };
    }
    rec.chunks = msg.seq + 1;
    rec.bytes += Math.floor(msg.data.length * 3 / 4) - (msg.data.endsWith("==") ? 2 : msg.data.endsWith("=") ? 1 : 0);
    return { ok: true };
  }
  if (msg?.type === "rec_done") { recDone(msg).catch(() => {}); return { ok: true }; }
  return { ok: false, error: "unknown message" };
}

async function recordStop(args) {
  let targets;
  if (args.all) targets = [...recordings.values()].filter(isActive);
  else if (validRecId(args.recId)) targets = recordings.has(args.recId) ? [recordings.get(args.recId)] : [];
  else {
    if (!Number.isInteger(args.tab)) throw new OpError("bad_target", "Give a tab id, a recId, or all: true.");
    const r = recFor(args.tab);
    targets = r ? [r] : [];
  }
  const out = [];
  for (const r of targets) out.push(recView(await stopRecording(r.recId, args.reason === "cli" || !args.reason ? "stopped (chromecli record stop)" : String(args.reason).slice(0, 60))));
  // Already finished ones for that tab (auto-stopped), so the CLI can still convert them.
  const finished = [...recordings.values()].filter((r) => !isActive(r) && !targets.includes(r)
    && (args.all || r.recId === args.recId || r.tab === args.tab)).map((r) => recView(r));
  return { ...(Number.isInteger(args.tab) ? { tab: args.tab } : {}), stopped: out, finished };
}

function recordStatus(args) {
  let rows = [...recordings.values()];
  if (validRecId(args.recId)) rows = rows.filter((r) => r.recId === args.recId);
  if (Number.isInteger(args.tab)) rows = rows.filter((r) => r.tab === args.tab);
  return { recordings: rows.map((r) => recView(r)), active: rows.filter(isActive).length };
}

// Kevin's Record in the popup: the click on the chromecli icon (on that tab) is what gives Chrome's grant.
async function popupRecordStart(recId) {
  const rec = recordings.get(recId);
  if (!rec || rec.state !== "awaiting_popup") return { ok: false, error: "This recording request is no longer waiting." };
  const tab = await chrome.tabs.get(rec.tab).catch(() => null);
  if (!tab) { await recCancel(rec, "cancelled", "tab closed"); return { ok: false, error: "That tab closed." }; }
  try {
    await checkRecordable(tab);
    if (originOf(tab.url || "") !== rec.origin) throw new OpError("not_shared", "The tab left the site it was shared on.");
  } catch (e) { await recCancel(rec, "cancelled", e.code || "refused"); return { ok: false, error: e.message }; }
  let streamId;
  try {
    streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: rec.tab });
  } catch (e) {
    const m = String(e?.message || e);
    return { ok: false, error: GRANT_ERROR.test(m)
      ? "Chrome needs the click on that tab: switch to it, click the chromecli icon there, then Record." : `Chrome refused: ${m}` };
  }
  clearTimeout(rec.waitTimer);
  rec.via = "popup";
  try { await beginCapture(rec, streamId, tab); } catch (e) { return { ok: false, error: e.message }; }
  return { ok: true };
}

// A covered / background tab: a frame from its recording's stream instead of captureVisibleTab.
async function streamShot(tab, rec, level, format, quality) {
  const origin = originOf(tab.url || "");
  let res;
  try { res = await toOffscreen({ type: "frame", recId: rec.recId, format, quality }); } catch (e) { res = { ok: false, error: String(e?.message || e) }; }
  if (!res?.ok) throw new OpError("script_error", `No frame from tab ${tab.id}'s recording: ${res?.error || "no answer"}`, { tab: tab.id, origin });
  const after = await chrome.tabs.get(tab.id).catch(() => null);
  if (!after || originOf(after.url || "") !== origin || recFor(tab.id) !== rec) {
    throw new OpError("not_visible", `Tab ${tab.id} changed during the capture.`, { tab: tab.id, origin });
  }
  return { tab: tab.id, url: tab.url, title: tab.title, origin, share: level, format, source: "stream", recId: rec.recId,
    width: res.width, height: res.height, frameAgeMs: res.frameAgeMs, frames: res.frames, data: res.data };
}

const trunc = (s, n) => (String(s).length > n ? String(s).slice(0, n - 1) + "…" : String(s));

// Stop before the tab shows another origin's pixels (onUpdated only fires at commit).
chrome.webNavigation?.onBeforeNavigate?.addListener((d) => {
  if (d.frameId !== 0) return;
  const rec = recFor(d.tabId);
  if (rec && originOf(d.url) !== rec.origin) stopRecording(rec.recId, "origin changed").catch(() => {});
});
chrome.webNavigation?.onCommitted?.addListener((d) => {
  if (d.frameId !== 0) return;
  const rec = recFor(d.tabId);
  if (rec && originOf(d.url) !== rec.origin) stopRecording(rec.recId, "origin changed").catch(() => {});
});

// ---- popup messages (only from this extension's own pages, never from a tab)
// The confirm window is an extension page in a popup window (so it has sender.tab); it may only read and answer
// its own confirm. No web page can load confirm.html (it is not web-accessible).
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  const url = String(sender.url || "");
  if (sender.id !== chrome.runtime.id || !url.startsWith(chrome.runtime.getURL(""))) return false;
  const fromConfirm = url.startsWith(chrome.runtime.getURL("confirm.html"));
  const fromOptions = url.startsWith(chrome.runtime.getURL("options.html"));
  // 0.12.1: the offscreen recorder only sends chunks and its done note (handled at once, in order).
  if (url.startsWith(chrome.runtime.getURL("offscreen.html"))) {
    if (sender.tab) return false;
    sendResponse(offscreenMessage(msg));
    return false;
  }
  if (msg?.target === "offscreen") return false;
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
        const rec = recFor(Number(id));
        shared.push({ tabId: Number(id), origin: s.origin, level: s.level, at: s.at ?? null,
          title: t?.title || "", favIconUrl: t?.favIconUrl || "", windowId: t?.windowId ?? null,
          ...(rec ? { rec: { ...recView(rec), button: recButton(rec) } } : {}) });
      }
      return { ok: true, tab: tab && { id: tab.id, title: tab.title, url, origin: originOf(url), favIconUrl: tab.favIconUrl || "" },
        shared,
        share: tab ? shares[tab.id] || null : null, deniedReason: tab ? denied(url, st.active) : null,
        paused, pending: st.pending, policyHash: st.active.hash, host: hostStatus, frameActAllow: st.active.frame_act_allow || [],
        shareCount: Object.keys(shares).length, audit: audit.slice(-20).reverse(), version: VERSION,
        confirms: pendingConfirms(), jevConfigured: (await getJev()).configured,
        grants: (await liveGrants()).sort((a, b) => a.createdAt - b.createdAt).map((g) => grantView(g)) };
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
      const ex = denyException(tab.url || "", st.active);
      await auditAdd({ op: `share-${msg.level}`, tab: tab.id, origin, outcome: "ok", ...(ex ? { denyExcept: ex } : {}) });
      await updateBadge(tab.id);
      return { ok: true };
    }
    case "unshare": await revoke(msg.tabId, "unshared"); return { ok: true };
    case "recordStart": return popupRecordStart(String(msg.recId || ""));
    case "recordStop": {
      const rec = recordings.get(String(msg.recId || ""));
      if (!rec || !isActive(rec)) return { ok: false, error: "Not recording." };
      await stopRecording(rec.recId, "stopped by Kevin");
      return { ok: true };
    }
    case "unshareAll": {
      const shares = await getShares();
      for (const id of Object.keys(shares)) await revoke(Number(id), "unshare all");
      return { ok: true };
    }
    case "pause": await setPaused(!!msg.paused); return { ok: true };
    case "revokeGrant": {
      await withLock(async () => { const g = await getGrants(); delete g[String(msg.id)]; await setGrants(g); });
      await auditAdd({ op: "grant-revoke", tab: null, origin: null, outcome: String(msg.id) });
      return { ok: true };
    }
    case "revokeAllGrants": await clearGrants("revoked by Kevin"); return { ok: true };
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
    if (inheriting.has(tabId)) await inheritShare(tabId, changeInfo.url);
  }
  if (changeInfo.url || changeInfo.status) await updateBadge(tabId);
});
chrome.tabs.onCreated?.addListener((tab) => { onTabCreated(tab).catch(() => {}); });
chrome.tabs.onRemoved.addListener(async (tabId) => {
  inheriting.delete(tabId);
  actionWindows.delete(tabId);
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
