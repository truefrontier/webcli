// Pure view model for the in-browser confirm window (no DOM, no chrome.*), so node --test can check it.
import { reasonLabel } from "./gate.js";
import { recordHeadline, recordRows } from "./recorder.js";

const VERB = { click: "Click", type: "Type into", fill: "Fill", select: "Choose in", press: "Press", eval: "Run JavaScript in",
  open: "Open", navigate: "Go to" };

const quote = (s) => `“${s}”`;
const trunc = (s, n) => (String(s).length > n ? String(s).slice(0, n - 1) + "…" : String(s));

export function hostOf(origin) {
  try { return new URL(origin).host || String(origin || ""); } catch { return String(origin || ""); }
}

// The headline: "Click “Place order”", "Fill “Password”", "Press Enter in “Email”", "Open example.org".
export function headline(info) {
  const op = info?.op;
  const el = info?.element;
  const name = el?.name ? quote(trunc(el.name, 60)) : el ? `this ${el.tag}` : "";
  if (op === "open" || op === "navigate") return `${VERB[op]} ${hostOf(info.origin)}`;
  if (op === "press") return el ? `Press ${info.key || "a key"} in ${name}` : `Press ${info.key || "a key"}`;
  if (op === "eval") return `Run JavaScript on ${hostOf(info.origin)}`;
  if (op === "record") return recordHeadline(info); // 0.12.1
  return `${VERB[op] || op} ${name}`.trim();
}

export function elementLine(el) {
  if (!el) return null;
  return [el.tag && `<${el.tag}>`, el.role && el.role !== el.tag ? el.role : null, el.type && el.tag === "input" ? `type=${el.type}` : null]
    .filter(Boolean).join(" · ");
}

// Rows for the details card: [label, value, kind?]. kind: "mono" | "masked" | "code".
export function detailRows(info) {
  const rows = [];
  if (!info) return rows;
  rows.push(["Origin", String(info.origin || ""), "mono"]);
  if (info.frameOrigin) rows.push(["Inside frame", `${info.frameOrigin} (frame ${info.frameId})`, "mono"]);
  if (info.title) rows.push(["Tab", trunc(info.title, 120)]);
  if (info.op === "record") { rows.push(...recordRows(info)); return rows; }
  if (info.op === "navigate" && info.from) rows.push(["From", hostOf(info.from), "mono"]);
  if (info.url && (info.op === "open" || info.op === "navigate")) rows.push(["URL", trunc(info.url, 300), "mono"]);
  if (info.element) {
    rows.push(["Element", elementLine(info.element)]);
    if (info.element.name && info.element.name.length > 60) rows.push(["Name", trunc(info.element.name, 160)]);
  }
  if (info.value) rows.push(["Value", info.value.masked ? `${info.value.text}  (${info.value.chars} chars, hidden)` : info.value.text, info.value.masked ? "masked" : "mono"]);
  if (info.key) rows.push(["Key", info.key, "mono"]);
  // 0.10.0: a pre-approval matched the action but not its amount cap, so Kevin decides.
  if (info.grant) {
    const g = info.grant;
    const money = (n) => { try { return new Intl.NumberFormat("en-US", { style: "currency", currency: g.currency || "USD" }).format(n); } catch { return `${n} ${g.currency || ""}`.trim(); } };
    rows.push(["Pre-approval", quote(trunc(g.quote || "", 200))]);
    if (g.maxAmount != null) rows.push(["Amount", `${g.amount != null ? money(g.amount) : "couldn't read one"} (cap ${money(g.maxAmount)})`, "mono"]);
    if (g.why) rows.push(["Why asking", g.why]);
  }
  return rows;
}

export function reasonItems(reasons) {
  return (reasons || []).map((r) => ({ code: String(r), label: reasonLabel(r) }));
}

export function opIcon(op) {
  return { click: "click", press: "keys", type: "keys", fill: "keys", select: "list", open: "globe", navigate: "globe",
    eval: "code", record: "rec" }[op] || "click";
}

export function countdown(expiresAt, now) {
  const s = Math.max(0, Math.ceil((Number(expiresAt) - Number(now)) / 1000));
  return { seconds: s, label: `${s}s`, urgent: s <= 10 };
}

// 0.16.2: the confirm window opens focused while Kevin may be typing in another app. No button has focus, a key never
// picks a button (Enter / Space on a Tab-focused button included), and for the first CONFIRM_ARM_MS both buttons are
// disabled and every key and click is ignored, Escape included. After that only a pointer click (or Escape = Deny) counts.
export const CONFIRM_ARM_MS = 1000;

// What one input does: "allow", "deny", or "ignore". kind: "key" | "click"; detail: the click's MouseEvent.detail
// (0 = synthesized by Enter / Space on a focused button); sinceMs: time since the window appeared.
export function confirmInput({ kind, button, key, detail = 1, sinceMs = 0 } = {}) {
  if (!(sinceMs >= CONFIRM_ARM_MS)) return "ignore";
  if (kind === "key") return key === "Escape" ? "deny" : "ignore";
  if (kind === "click" && detail > 0) return button === "allow" ? "allow" : button === "deny" ? "deny" : "ignore";
  return "ignore";
}

// How the answer was given, sent with confirmAnswer so the host log can say (via, ms after the window appeared,
// and how many keys / clicks were ignored before it).
export function answerHow(via, sinceMs, ignored = {}) {
  return { via, afterMs: Math.max(0, Math.round(Number(sinceMs) || 0)), ignoredKeys: ignored.keys | 0, ignoredClicks: ignored.clicks | 0 };
}
