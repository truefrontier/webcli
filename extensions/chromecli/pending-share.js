// The share Kevin picked in the popup while Chrome's "Allow / Cancel" site-access prompt is open (0.16.2).
// The popup can close while that prompt has focus, so the code after permissions.request may never run; the service
// worker keeps the choice in chrome.storage.session and applies it once Chrome grants every origin asked for
// (permissions.onAdded, the popup's next open, or the request's own answer). Cancel never applies it.
// Pure helpers; background.js does the storage and the share.
import { permissionPattern } from "./policy.js";
import { registrableDomain } from "./psl.js";

export const PENDING_SHARE_TTL_MS = 2 * 60 * 1000; // long enough to read the prompt, short enough not to linger
// 0.16.3: "https://*.site/*" too (a share asks for its whole site); a wildcard never sits on a public suffix
// (*.com, *.github.io). 0.16.4: http or https only: Chrome refuses "*://" against this manifest.
const PATTERN = /^https?:\/\/(\*\.)?([a-z0-9.-]+)(:\d+)?\/\*$/i;

// {tabId, origin, level, origins, at, expiresAt} from the popup's message, or throws (bad input).
export function makePendingShare(msg, now = Date.now()) {
  const level = msg?.level;
  if (!["read", "act"].includes(level)) throw new Error("bad level");
  if (!Number.isInteger(msg.tabId)) throw new Error("bad tab");
  const origin = String(msg.origin || "");
  const main = permissionPattern(origin);
  if (!main) throw new Error("bad origin");
  const origins = [...new Set([main, ...(Array.isArray(msg.origins) ? msg.origins : [])].map(String))];
  const ok = (o) => { const m = PATTERN.exec(o); return !!m && (!m[1] || registrableDomain(m[2]) !== null); };
  if (!origins.every(ok)) throw new Error("bad origins");
  return { tabId: msg.tabId, origin, level, origins, at: now, expiresAt: now + PENDING_SHARE_TTL_MS };
}

export const pendingLive = (p, now = Date.now()) => !!p && typeof p.expiresAt === "number" && now < p.expiresAt;
