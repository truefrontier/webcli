// Pure view helpers for the popup (no DOM, no chrome.*), so node --test can check them.

// Header pill: paused wins over the connection state (agents can't do anything while paused).
export function statusOf(state) {
  if (state?.paused) return { key: "paused", label: "Paused" };
  if (state?.host?.connected) return { key: "connected", label: "Connected" };
  return { key: "disconnected", label: "Disconnected" };
}

// The current share level as the segmented control's value.
export function levelOf(share) {
  return share?.level === "act" ? "act" : share?.level === "read" ? "read" : "off";
}

export const LEVEL_HINT = {
  off: "Not shared. Agents can't see this tab.",
  read: "Agents can read this tab (badge R).",
  act: "Agents can read this tab (badge A). Actions come later.",
};

// "Command+Shift+Period" / "⌘⇧." / "Ctrl+Shift+Period" -> key caps for <kbd>.
const KEY_NAMES = { Command: "⌘", MacCtrl: "⌃", Ctrl: "Ctrl", Alt: "⌥", Option: "⌥", Shift: "⇧", Period: ".",
  Comma: ",", Space: "Space" };
export function shortcutKeys(shortcut) {
  const s = String(shortcut || "").trim();
  if (!s) return [];
  if (!s.includes("+")) return [...s]; // Chrome on macOS reports "⌘⇧." already
  return s.split("+").map((k) => KEY_NAMES[k] || k);
}

// Pending policy loosening entry -> readable label.
const KIND_LABEL = { read_allow_added: "Allow read", act_allow_added: "Allow act", deny_removed: "Remove deny",
  implicit_active: "Implicit active tab", history_enabled: "History" };
export function loosenLabel(l) {
  return KIND_LABEL[l?.kind] || String(l?.kind || "").replace(/_/g, " ");
}

// Audit entry -> { time, op, target, outcome, tone }.
export function auditRow(a) {
  const time = String(a?.at || "").slice(11, 19);
  const target = [a?.tab != null ? `tab ${a.tab}` : "", a?.origin ? hostOf(a.origin) : ""].filter(Boolean).join(" ");
  const outcome = String(a?.outcome ?? "");
  // Ops log "ok" or an error code (not_shared, origin_blocked, ...); unshares log a reason; policy acks a hash.
  const tone = outcome === "ok" ? "ok" : /^[a-z]+(?:_[a-z]+)+$|^paused$/.test(outcome) ? "bad" : "neutral";
  return { time, op: String(a?.op || ""), target, outcome, tone };
}

// "https://www.example.com:8443" -> "www.example.com:8443"; non-URLs pass through.
export function hostOf(origin) {
  try { return new URL(origin).host || String(origin); } catch { return String(origin || ""); }
}

// One letter for the favicon fallback tile.
export function monogram(title, origin) {
  const h = hostOf(origin || "").replace(/^www\./, "");
  const c = (h || String(title || "")).trim().charAt(0);
  return c ? c.toUpperCase() : "?";
}

// Only show favicons Chrome already resolved to http(s)/data URLs (never chrome:// or extension URLs).
export function safeFavicon(url) {
  return typeof url === "string" && /^(?:https?:|data:image\/)/i.test(url) ? url : null;
}
