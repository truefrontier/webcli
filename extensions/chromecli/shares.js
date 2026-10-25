// Share bookkeeping: a share is (tabId, origin, level). Pure helpers; background.js keeps the map in
// chrome.storage.session (cleared when Chrome restarts).
import { denied, originOf } from "./policy.js";
import { sameSite, siteOf } from "./psl.js";

export const LEVELS = ["read", "act"];

// What to do with an existing share when its tab's URL changes: null (keep) or a revoke reason.
export function revokeReason(share, newUrl, policy) {
  if (!share) return null;
  const origin = originOf(newUrl);
  if (!origin) return "left http(s)";
  if (origin !== share.origin) return "origin changed";
  const why = denied(newUrl, policy);
  return why ? `denied (${why})` : null;
}

// 0.16.3: a share Kevin made follows its tab to another host of the same site (registrable domain, by the Public
// Suffix List: developer.apple.com <-> appstoreconnect.apple.com), keeping its level. Returns the site, or null when
// the move ends the share as before: another site, https -> http, a denied URL (the deny list always wins), a
// bot-opened tab's auto-share, or no change of origin at all. The caller still needs Chrome's host permission.
export function carrySite(share, newUrl, policy) {
  if (!share || share.botOpened) return null;
  const origin = originOf(newUrl);
  if (!origin || origin === share.origin || !sameSite(share.origin, origin)) return null;
  if (denied(newUrl, policy)) return null;
  return siteOf(origin);
}

// Can this share level do `need` ("read" | "act")?
export function levelAllows(level, need) {
  return need === "read" ? LEVELS.includes(level) : level === "act";
}

export function badgeFor(share, paused) {
  if (paused) return { text: "II", color: "#777777" };
  if (!share) return { text: "", color: "#000000" };
  return share.level === "act" ? { text: "A", color: "#E8710A" } : { text: "R", color: "#1A73E8" };
}
