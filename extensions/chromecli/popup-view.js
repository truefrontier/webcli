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
  act: "Agents can read and act here (badge A). Risky actions ask you first.",
};

// The Jev row: on when a TypeSafe key is saved in options (the key itself never reaches the popup).
export function jevView(configured) {
  return configured
    ? { on: true, chip: "On", sub: "Finds elements and classifies clicks; can only add prompts" }
    : { on: false, chip: "Off", sub: "Heuristics only. Add a TypeSafe key for smarter finding" };
}

// Agent actions waiting for Kevin's Allow / Deny in the confirm window.
export function confirmsLabel(n) {
  const k = Number(n) || 0;
  if (k <= 0) return "";
  return k === 1 ? "1 agent action waits for your OK" : `${k} agent actions wait for your OK`;
}

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

// ---- 0.10.0 pre-approvals
const GRANT_VERB = { click: "Click", fill: "Fill", type: "Type into", select: "Choose in", press: "Press", navigate: "Go to",
  open: "Open", close: "Close" };

// "Click “Place order” · ≤ $49.99" — what one grant allows.
export function grantScope(g) {
  const t = g?.target || {};
  const what = t.text != null ? `“${t.text}”${t.role ? ` (${t.role})` : ""}` : t.selector != null ? t.selector
    : g?.urlPrefix ? g.urlPrefix.replace(/^https?:\/\//, "") : hostOf(g?.origin || "");
  const verb = g?.op === "press" && g?.value ? `Press ${g.value} in` : GRANT_VERB[g?.op] || g?.op || "";
  let cap = "";
  if (g?.maxAmount != null) {
    try { cap = ` · ≤ ${new Intl.NumberFormat("en-US", { style: "currency", currency: g.currency || "USD" }).format(g.maxAmount)}`; }
    catch { cap = ` · ≤ ${g.maxAmount} ${g.currency || ""}`.trimEnd(); }
  }
  return `${verb} ${what}${cap}`.trim();
}

// "1 use left · 28m left" / "3 uses left · 1h 5m left" / "expired".
export function grantMeta(g, now = Date.now()) {
  const s = Math.max(0, Math.round((Number(g?.expiresAt) - now) / 1000));
  if (!s || !(g?.usesLeft > 0)) return "expired";
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const left = h ? `${h}h${m ? ` ${m}m` : ""}` : m ? `${m}m` : `${s}s`;
  return `${g.usesLeft} use${g.usesLeft === 1 ? "" : "s"} left · ${left} left`;
}

export function grantsSub(enabled, count) {
  if (!enabled) return "Off: every risky action asks you here";
  return count ? `${count} active · Pause revokes all` : "On: agents may record a “yes” you give in chat";
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
