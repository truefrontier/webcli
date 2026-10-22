// chromecli 0.11.1: frames. Pure helpers (no chrome.*) for the service worker: which of a tab's frames an op may read,
// where each frame sits in the tab's viewport, the scroll target across frames, the merged read, the skipped list.
// Rule for a frame (webNavigation.getAllFrames): the tab's own origin -> read; a denied URL -> skipped (denied);
// the same registrable site (scheme included) -> read only if Chrome already grants chromecli host access to it, else
// skipped (no_host_permission); any other site -> skipped (other_site). A frame inside a skipped frame is skipped too
// (parent_skipped). about:blank / about:srcdoc / blob: frames take their creator's origin; data: and other opaque
// origins are skipped (opaque_origin). Nothing here widens access: a skipped frame gets no script at all.
import { denied, originOf } from "./policy.js";

export const MAX_FRAMES = 30;

// Public suffixes with two labels that matter for the sites chromecli is used on; anything else counts as one label.
const TWO_LABEL_SUFFIXES = new Set(["co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "com.au", "net.au", "org.au", "co.nz",
  "co.jp", "ne.jp", "or.jp", "com.br", "com.mx", "co.in", "co.za", "com.sg", "com.hk", "com.tw", "com.cn", "co.kr",
  "com.tr", "com.ar", "co.il", "com.my", "com.ph", "com.vn", "co.id", "com.pk", "com.ng", "com.eg", "com.sa", "co.th"]);

// The registrable domain ("eTLD+1") of a hostname: app.signnow.com -> signnow.com. IP addresses and single-label
// hosts (localhost) are their own site.
export function siteOf(hostname) {
  const h = String(hostname || "").toLowerCase().replace(/\.$/, "");
  if (!h || /^\d+(\.\d+){3}$/.test(h) || h.includes(":") || h.startsWith("[") || !h.includes(".")) return h;
  const parts = h.split(".");
  const n = TWO_LABEL_SUFFIXES.has(parts.slice(-2).join(".")) ? 3 : 2;
  return parts.slice(-n).join(".");
}

// Schemeful same-site: same scheme and the same registrable domain (ports don't matter).
export function sameSite(originA, originB) {
  try {
    const a = new URL(originA);
    const b = new URL(originB);
    return a.protocol === b.protocol && siteOf(a.hostname) === siteOf(b.hostname);
  } catch {
    return false;
  }
}

// The origin a frame's document runs as: its URL's, or for about:blank / about:srcdoc its parent's; blob: URLs carry
// their creator's origin. null for opaque origins (data:, javascript:, other schemes).
export function frameOrigin(url, parentOrigin) {
  const u = String(url || "");
  if (u === "" || /^about:(blank|srcdoc)/i.test(u)) return parentOrigin || null;
  if (/^blob:/i.test(u)) return originOf(u.slice(5));
  return originOf(u);
}

// A short label for a URL: origin + path (no query or fragment, where tokens live).
export function shortUrl(url) {
  try {
    const u = new URL(url);
    return /^https?:$/.test(u.protocol) ? u.origin + u.pathname : u.protocol + (u.pathname === "srcdoc" ? "srcdoc" : "");
  } catch {
    return "";
  }
}

// frames: webNavigation.getAllFrames() rows {frameId, parentFrameId, url, errorOccurred?}. Returns
// {allowed: [{frameId, parentFrameId, url, origin, depth}], skipped: [{kind: "iframe", frameId, parentFrameId, origin,
// url, reason}]}, parents before children. hasPermission(origin) -> Promise<bool>.
// 0.11.3: actOrigins (frame_act_allow, passed only for tabs Kevin shared Act): frames from exactly these origins are
// read too (not denied, and only with host access), marked frameAct: true so page actions may target them.
export async function classifyFrames(frames, { tabOrigin, policy, hasPermission, max = MAX_FRAMES, actOrigins = [] }) {
  const rows = (Array.isArray(frames) ? frames : []).filter((f) => f && Number.isInteger(f.frameId));
  const byId = new Map(rows.map((f) => [f.frameId, f]));
  const kids = new Map();
  for (const f of rows) {
    if (f.frameId === 0) continue;
    const p = byId.has(f.parentFrameId) ? f.parentFrameId : 0;
    if (!kids.has(p)) kids.set(p, []);
    kids.get(p).push(f);
  }
  const allowed = [{ frameId: 0, parentFrameId: -1, url: byId.get(0)?.url || "", origin: tabOrigin, depth: 0 }];
  const skipped = [];
  const seen = new Set([0]);
  const queue = [{ id: 0, origin: tabOrigin, ok: true, depth: 0 }];
  while (queue.length) {
    const parent = queue.shift();
    for (const f of (kids.get(parent.id) || []).sort((a, b) => a.frameId - b.frameId)) {
      if (seen.has(f.frameId)) continue;
      seen.add(f.frameId);
      const origin = frameOrigin(f.url, parent.origin);
      const row = { frameId: f.frameId, parentFrameId: parent.id, origin, url: shortUrl(f.url) };
      let reason = null;
      if (!parent.ok) reason = "parent_skipped";
      else if (f.errorOccurred) reason = "load_error";
      else if (!origin) reason = "opaque_origin";
      else if (denied(/^https?:/i.test(f.url || "") ? f.url : `${origin}/`, policy)) reason = "denied";
      else if (actOrigins.includes(origin)) {
        if (origin !== tabOrigin && !(await hasPermission(origin))) reason = "no_host_permission";
      } else if (origin !== tabOrigin) {
        if (!sameSite(origin, tabOrigin)) reason = "other_site";
        else if (!(await hasPermission(origin))) reason = "no_host_permission";
      }
      if (!reason && allowed.length >= max) reason = "too_many_frames";
      if (reason) skipped.push({ kind: "iframe", ...row, reason, ...(actOrigins.includes(origin) ? { frameActAllowed: true } : {}) });
      else allowed.push({ ...row, url: f.url || "", depth: parent.depth + 1, ...(actOrigins.includes(origin) ? { frameAct: true } : {}) });
      queue.push({ id: f.frameId, origin, ok: !reason, depth: parent.depth + 1 });
    }
  }
  return { allowed, skipped };
}

const area = (r) => (r && r.w > 0 && r.h > 0 ? r.w * r.h : 0);
function clip(r, c) {
  if (!r || !c) return null;
  const x = Math.max(r.x, c.x);
  const y = Math.max(r.y, c.y);
  const w = Math.min(r.x + r.w, c.x + c.w) - x;
  const h = Math.min(r.y + r.h, c.y + c.h) - y;
  return { x, y, w: Math.max(0, w), h: Math.max(0, h) };
}
function offset(r, by) {
  return r ? { x: Math.round((by?.x || 0) + r.x), y: Math.round((by?.y || 0) + r.y), w: Math.round(r.w), h: Math.round(r.h) } : null;
}

// Where each frame is in the tab's (top frame's) viewport. reports: Map frameId -> page diag {viewport {w, h},
// iframes [{frameId|null, url, origin, rect {x,y,w,h} in that frame's viewport}]}; frameRows: webNavigation rows.
// Children the page couldn't name (no chrome.runtime.getFrameId) are matched to webNavigation rows by URL, then in
// order. Returns {geo: Map frameId -> {rect (the iframe box, top coords), visible (clipped to every ancestor
// viewport)}, unmatched: page iframes that no listed frame stands for (not loaded yet) as skipped rows}.
export function frameGeometry(reports, frameRows) {
  const geo = new Map();
  const unmatchedEls = [];
  const top = reports.get(0);
  const vp = top?.viewport ? { x: 0, y: 0, w: top.viewport.w, h: top.viewport.h } : null;
  if (vp) geo.set(0, { rect: vp, visible: vp });
  const rowsAll = Array.isArray(frameRows) ? frameRows : [];
  const walk = (pid) => {
    const parent = geo.get(pid);
    const rep = reports.get(pid);
    const rows = rowsAll.filter((f) => f.parentFrameId === pid && f.frameId !== 0);
    if (!parent || !rep || !Array.isArray(rep.iframes)) return;
    const unmatched = new Set(rows.map((f) => f.frameId));
    const assign = (el, id) => {
      const r = el.rect || { x: 0, y: 0, w: 0, h: 0 };
      const rect = { x: parent.rect.x + r.x, y: parent.rect.y + r.y, w: r.w, h: r.h };
      geo.set(id, { rect, visible: clip(rect, parent.visible) });
      unmatched.delete(id);
    };
    let pending = [];
    for (const el of rep.iframes) {
      if (Number.isInteger(el.frameId) && el.frameId > 0 && unmatched.has(el.frameId)) assign(el, el.frameId);
      else pending.push(el);
    }
    pending = pending.filter((el) => {
      const hit = rows.find((f) => unmatched.has(f.frameId) && el.url && (f.url === el.url || shortUrl(f.url) === shortUrl(el.url)));
      if (hit) assign(el, hit.frameId);
      return !hit;
    });
    pending = pending.filter((el) => {
      const next = rows.find((f) => unmatched.has(f.frameId));
      if (next) assign(el, next.frameId);
      return !next;
    });
    for (const el of pending) {
      unmatchedEls.push({ kind: "iframe", frameId: null, parentFrameId: pid, origin: el.origin || null,
        url: el.url ? shortUrl(el.url) : "", rect: offset(el.rect, parent.rect), reason: "not_loaded" });
    }
    for (const f of rows) walk(f.frameId);
  };
  walk(0);
  return { geo, unmatched: unmatchedEls };
}

// The skipped list a reply carries: frames chromecli didn't read (with their box when known), page iframes no frame
// stands for, and per-frame regions it can't read (canvas, plugins, closed shadow roots it couldn't open).
export function skippedList({ frameSkips = [], failed = [], unmatched = [], reports = new Map(), geo = new Map() }) {
  const out = [];
  for (const s of [...frameSkips, ...failed]) {
    const g = geo.get(s.frameId);
    out.push({ ...s, ...(g ? { rect: offset(g.rect) } : {}) });
  }
  out.push(...unmatched);
  for (const [fid, rep] of reports) {
    const at = fid === 0 ? null : geo.get(fid)?.rect;
    for (const r of rep?.regions || []) out.push({ ...r, frameId: fid, rect: offset(r.rect, at) });
  }
  return out.slice(0, 60);
}

// Scroll target across frames. surveys: Map frameId -> pageScroll({survey: true}) result:
//   {viewport {w,h}, doc {scrollHeight, clientHeight, scrolls}, best {path, area, ...} | null}
// geo: frameGeometry().geo. The 0.10.2 rule, across frames: an inner scroller (an element in any frame, or a
// subframe's own document) covering >= 30% of the tab's viewport beats the top window, the largest one wins; else
// the top window if it scrolls; else the largest inner scroller of any size; else the top window.
export function pickScrollTarget(surveys, geo) {
  const top = surveys.get(0);
  const vpArea = top?.viewport ? top.viewport.w * top.viewport.h : 0;
  const cands = [];
  for (const [fid, s] of surveys) {
    if (!s || s.ok === false) continue;
    const g = geo.get(fid);
    const frameArea = s.viewport ? s.viewport.w * s.viewport.h : 0;
    // The share of this frame's viewport the tab shows (geometry unknown: assume all of it).
    const seen = fid === 0 || !g ? 1 : (frameArea ? Math.min(1, area(g.visible) / frameArea) : 0);
    if (fid !== 0 && s.doc && s.doc.scrolls) {
      cands.push({ frameId: fid, path: "document", area: g ? area(g.visible) : frameArea, kind: "document" });
    }
    if (s.best && s.best.path != null) cands.push({ frameId: fid, path: s.best.path, area: Math.round((s.best.area || 0) * seen), kind: "element" });
  }
  cands.sort((a, b) => b.area - a.area || a.frameId - b.frameId);
  const big = cands.find((c) => vpArea && c.area >= 0.3 * vpArea);
  if (big) return big;
  if (top?.doc?.scrolls) return { frameId: 0, path: "document", area: vpArea, kind: "document" };
  if (cands.length && cands[0].area > 0) return cands[0];
  return { frameId: 0, path: "document", area: vpArea, kind: "document" };
}

// One read from several frames: the top frame's content first, then each frame's after a "[frame N: url WxH]" line
// (an HTML comment in md). Elements from subframes get refs "f<frameId>:e<n>" and frame: <frameId>.
export function mergeRead(parts, { as, maxChars, geo = new Map() }) {
  const frames = [];
  let content = "";
  const elements = [];
  let anyElements = false;
  for (const p of parts) {
    const r = p.result || {};
    const text = String(r.content || "").trim();
    const g = geo.get(p.frameId);
    const info = { frameId: p.frameId, origin: p.origin, url: shortUrl(r.url || p.url), chars: text.length,
      ...(g && p.frameId !== 0 ? { rect: offset(g.rect) } : {}) };
    if (p.frameId === 0) {
      content = text;
      info.offset = 0;
    } else if (text) {
      const box = g?.rect ? ` ${Math.round(g.rect.w)}x${Math.round(g.rect.h)}` : "";
      const sep = content ? "\n\n" : "";
      const head = as === "md" ? `${sep}<!-- frame ${p.frameId}: ${info.url}${box} -->\n\n` : `${sep}[frame ${p.frameId}: ${info.url}${box}]\n`;
      content += head + text;
      info.offset = content.length - text.length;
    }
    frames.push(info);
    if (Array.isArray(r.elements)) {
      anyElements = true;
      for (const el of r.elements) elements.push(p.frameId === 0 ? el : { ...el, ref: `f${p.frameId}:${el.ref}`, frame: p.frameId });
    }
  }
  const chars = content.length;
  const truncated = chars > maxChars;
  if (truncated) content = content.slice(0, maxChars);
  return { content, chars, truncated, frames, ...(anyElements ? { elements } : {}) };
}

// "f12:e3" -> {frameId: 12, ref: "e3"}; a top-frame ref ("e3") -> {frameId: 0, ref: "e3"}; else null.
export function parseFrameRef(ref) {
  const m = /^(?:f(\d+):)?(e\d+)$/.exec(String(ref || ""));
  return m ? { frameId: m[1] ? Number(m[1]) : 0, ref: m[2] } : null;
}
