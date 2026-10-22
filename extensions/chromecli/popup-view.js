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
  deny_except_added: "Deny exception", frame_act_allow_added: "Act in frames", implicit_active: "Implicit active tab", history_enabled: "History" };
export function loosenLabel(l) {
  return KIND_LABEL[l?.kind] || String(l?.kind || "").replace(/_/g, " ");
}

// 0.11.3: a deny_except pattern in plain words for the pending card, e.g. "https://admin.shopify.com/store/*/oauth/install"
// -> "Lets agents into admin.shopify.com/store/(any store)/oauth/install, any query, despite the /oauth deny ...".
export function exceptWords(pattern) {
  const p = String(pattern || "");
  const m = /^https:\/\/([^/]+)(\/.*)$/i.exec(p);
  if (!m) return p;
  const segs = m[2].split("/").slice(1);
  const path = segs.map((g, i) => g === "*" ? `(any ${segs[i - 1] ? segs[i - 1].replace(/s$/, "") : "one part"})`
    : g.includes("*") ? `${g.replace(/\*/g, "…")} (… = anything within this part)` : g).join("/");
  return `Agents may open and use https://${m[1]}/${path} (any query string), even though a path deny such as /oauth `
    + "covers it. Only this exact path shape on this one host; every other deny rule still applies, and clicks there "
    + "still ask you first.";
}

// Plain-words detail line for a pending loosening entry ("" when the label + value already say it).
export function loosenWords(l) {
  if (l?.kind === "deny_except_added") return exceptWords(l.value);
  if (l?.kind === "frame_act_allow_added") return frameActWords(l.value);
  return "";
}

// 0.11.3: a frame_act_allow origin in plain words.
export function frameActWords(origin) {
  return `Let agents act inside ${hostOf(origin)} frames on act-shared tabs (tabs you set to Act yourself). `
    + "Clicks and typing there get the same checks as the page itself: risky ones still ask you first. "
    + "Accepting also asks Chrome for access to that site.";
}

// Audit entry -> { time, op, target, outcome, tone }.
export function auditRow(a) {
  const time = String(a?.at || "").slice(11, 19);
  const target = [a?.tab != null ? `tab ${a.tab}` : "", a?.origin ? hostOf(a.origin) : ""].filter(Boolean).join(" ");
  const outcome = String(a?.outcome ?? "");
  // Ops log "ok" or an error code (not_shared, origin_blocked, ...); unshares log a reason; policy acks a hash.
  const tone = outcome === "ok" ? "ok" : /^[a-z]+(?:_[a-z]+)+$|^paused$/.test(outcome) ? "bad" : "neutral";
  // 0.11.3: a deny exception let this URL through
  const shown = a?.denyExcept ? `${outcome} · via deny exception` : outcome;
  return { time, op: String(a?.op || ""), target, outcome: shown, tone, ...(a?.denyExcept ? { denyExcept: String(a.denyExcept) } : {}) };
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
  const inFrame = g?.frameOrigin ? ` in ${hostOf(g.frameOrigin)} frame` : "";
  return `${verb} ${what}${inFrame}${cap}`.trim();
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

// 0.10.1: no toggle. The subtitle says what's active (or how one gets added).
export function grantsSub(count) {
  return count ? `${count} active · Pause revokes all` : "None. Agents add one only from your words in chat";
}

// Jev's scope verdict on a grant: "Jev ✓ 0.94" (+ "· narrowed").
export function grantJev(g) {
  const j = g?.jev;
  if (!j?.verdict) return null;
  const c = typeof j.confidence === "number" ? j.confidence.toFixed(2) : "";
  const narrowed = (j.narrowed || []).map((n) => ({ maxAmount: "cap", ttlSec: "time" })[n.field] || n.field);
  if (j.verdict !== "authorized") return { tone: "bad", label: `Jev ${j.verdict}`, title: "Jev didn't authorize this grant" };
  return { tone: "ok", label: `Jev ✓ ${c}${narrowed.length ? " · narrowed" : ""}`.replace(/\s+·/, " ·").trim(),
    title: `Jev: your words authorize this grant (${c})${narrowed.length ? `; narrowed ${narrowed.join(", ")} to what you said` : ""}` };
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
