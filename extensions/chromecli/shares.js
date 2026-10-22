// Share bookkeeping: a share is (tabId, origin, level). Pure helpers; background.js keeps the map in
// chrome.storage.session (cleared when Chrome restarts).
import { denied, originOf } from "./policy.js";

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

// Can this share level do `need` ("read" | "act")?
export function levelAllows(level, need) {
  return need === "read" ? LEVELS.includes(level) : level === "act";
}

export function badgeFor(share, paused) {
  if (paused) return { text: "II", color: "#777777" };
  if (!share) return { text: "", color: "#000000" };
  return share.level === "act" ? { text: "A", color: "#E8710A" } : { text: "R", color: "#1A73E8" };
}
