// Pre-approval grants (0.10.0): Kevin tells an agent, in his own words, that one specific action is OK ("buy it if
// it's under $50", "delete the as-described-review store"). The agent records that as a grant; when the action
// comes up and would otherwise wait for Kevin's Allow, a matching grant answers for him once.
// Pure helpers; background.js keeps grants in chrome.storage.session (gone when Chrome restarts) and only accepts new
// ones while Kevin's popup toggle "Allow agent pre-approvals" is on.

export const GRANT_OPS = ["click", "fill", "type", "select", "press", "navigate", "open", "close"];
export const PAGE_GRANT_OPS = ["click", "fill", "type", "select", "press"];
export const DEFAULT_TTL_SEC = 30 * 60;
export const MAX_TTL_SEC = 24 * 60 * 60;
export const MIN_TTL_SEC = 60;
export const MAX_USES = 20;
export const QUOTE_MAX = 500;
// Gate reasons no grant can answer: secrets and payment fields, eval. (Denied origins never get this far.)
export const NEVER_SKIPPABLE = new Set(["password_field", "payment_field", "credential_field", "sensitive_name", "eval"]);

export class GrantError extends Error {
  constructor(code, message) { super(message); this.code = code; }
}

const clean = (s) => String(s ?? "").replace(/\s+/g, " ").trim();
const httpOrigin = (u) => {
  try { const x = new URL(String(u)); return ["http:", "https:"].includes(x.protocol) ? x.origin : null; } catch { return null; }
};

// Validate and normalize what the CLI sent (grant.add). Throws GrantError("bad_request", ...).
export function validateGrant(input, { now = Date.now(), id } = {}) {
  const bad = (m) => { throw new GrantError("bad_request", m); };
  const a = input && typeof input === "object" ? input : {};
  const op = String(a.op || "");
  if (!GRANT_OPS.includes(op)) bad(`op must be one of ${GRANT_OPS.join(", ")}.`);
  const origin = httpOrigin(a.origin);
  if (!origin) bad("origin must be an http(s) origin like https://shop.example.com.");
  const quote = clean(a.quote);
  if (!quote) bad("quote is required: Kevin's exact words approving this action.");
  if (quote.length > QUOTE_MAX) bad(`quote is over ${QUOTE_MAX} characters.`);
  const t = a.target && typeof a.target === "object" ? a.target : {};
  let target = null;
  if (t.text != null && clean(t.text)) target = { text: clean(t.text).slice(0, 200), ...(t.role ? { role: clean(t.role).toLowerCase().slice(0, 40) } : {}) };
  else if (t.selector != null && clean(t.selector)) target = { selector: String(t.selector).slice(0, 500) };
  if (t.text != null && t.selector != null) bad("Give --target-text or --selector, not both.");
  if (PAGE_GRANT_OPS.includes(op) && !target) bad(`A ${op} grant needs --target-text NAME [--role R] or --selector S.`);
  if (!PAGE_GRANT_OPS.includes(op) && target) bad(`${op} grants match on origin (and --url-prefix), not an element.`);
  const g = { id: id || `g${Math.random().toString(16).slice(2, 8)}`, op, origin, target, quote, createdAt: now };
  if (a.value != null) {
    if (!["fill", "type", "select", "press"].includes(op)) bad("--value only applies to fill, type, select, and press (the key).");
    g.value = String(a.value);
  }
  if (a.urlPrefix != null) {
    if (!["navigate", "open"].includes(op)) bad("--url-prefix only applies to navigate and open.");
    if (httpOrigin(a.urlPrefix) !== origin) bad("--url-prefix must be on the grant's origin.");
    g.urlPrefix = String(a.urlPrefix);
  }
  if (a.maxAmount != null) {
    const m = Number(a.maxAmount);
    if (!Number.isFinite(m) || m <= 0) bad("--max-amount must be a positive number.");
    if (!["click", "press"].includes(op)) bad("--max-amount only applies to click and press.");
    g.maxAmount = Math.round(m * 100) / 100;
    g.currency = clean(a.currency || "USD").toUpperCase();
    if (!/^[A-Z]{3}$/.test(g.currency)) bad("--currency must be a 3-letter code like USD.");
    if (a.amountSelector != null && clean(a.amountSelector)) g.amountSelector = String(a.amountSelector).slice(0, 500);
  } else if (a.currency != null || a.amountSelector != null) {
    bad("--currency and --amount-selector need --max-amount.");
  }
  const uses = a.uses == null ? 1 : Number(a.uses);
  if (!Number.isInteger(uses) || uses < 1 || uses > MAX_USES) bad(`--uses must be 1-${MAX_USES}.`);
  const ttl = a.ttlSec == null ? DEFAULT_TTL_SEC : Number(a.ttlSec);
  if (!Number.isFinite(ttl) || ttl < MIN_TTL_SEC || ttl > MAX_TTL_SEC) bad("--ttl must be between 1m and 24h.");
  g.uses = uses;
  g.usesLeft = uses;
  g.expiresAt = now + Math.round(ttl) * 1000;
  return g;
}

export const isLive = (g, now = Date.now()) => !!g && g.usesLeft > 0 && g.expiresAt > now;

// Accessible name / text match: case-insensitive exact, or the whole words appear in the name.
export function nameMatches(want, name) {
  const w = clean(want).toLowerCase();
  const n = clean(name).toLowerCase();
  if (!w || !n) return false;
  if (n === w) return true;
  const esc = w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`(^|[^\\p{L}\\p{N}])${esc}($|[^\\p{L}\\p{N}])`, "u").test(n);
}

// Can any grant answer these gate reasons? Not if one of them is never skippable.
export const canSkip = (reasons) => Array.isArray(reasons) && reasons.length > 0 && !reasons.some((r) => NEVER_SKIPPABLE.has(r));

// Does grant g cover this action? ctx: {op, origin, now, element?, value?, key?, url?, selectorHit?}
export function grantMatches(g, ctx) {
  if (!isLive(g, ctx.now ?? Date.now())) return { ok: false, why: "expired_or_used" };
  if (g.op !== ctx.op) return { ok: false, why: "op" };
  if (g.origin !== ctx.origin) return { ok: false, why: "origin" };
  if (PAGE_GRANT_OPS.includes(g.op)) {
    const el = ctx.element;
    if (!el) return { ok: false, why: "no_element" };
    if (g.target?.text != null) {
      if (!nameMatches(g.target.text, el.name)) return { ok: false, why: "target_text" };
      if (g.target.role && String(el.role || "").toLowerCase() !== g.target.role) return { ok: false, why: "role" };
    } else if (g.target?.selector != null) {
      if (ctx.selectorHit !== true) return { ok: false, why: "selector" };
    } else {
      return { ok: false, why: "target" };
    }
    if (g.value != null) {
      const v = g.op === "press" ? ctx.key : ctx.value;
      if (String(v ?? "") !== g.value) return { ok: false, why: "value" };
    }
  } else if (g.urlPrefix && !String(ctx.url || "").startsWith(g.urlPrefix)) {
    return { ok: false, why: "url_prefix" };
  }
  return { ok: true };
}

// Money amounts in page text: "$1,299.00", "USD 49.99", "49.99 USD", "€12,50", "£8".
const SYMBOL = { "$": "USD", "US$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "C$": "CAD", "A$": "AUD", "CA$": "CAD", "AU$": "AUD" };
const NUM = String.raw`(\d{1,3}(?:[,.\s]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)`;
const PRE = new RegExp(String.raw`(US\$|CA\$|AU\$|C\$|A\$|\$|€|£|¥|\b[A-Z]{3}\b)\s?` + NUM, "g");
const POST = new RegExp(NUM + String.raw`\s?(\b[A-Z]{3}\b|€|£)`, "g");

function toNumber(raw) {
  let s = raw.replace(/\s/g, "");
  const lastSep = Math.max(s.lastIndexOf("."), s.lastIndexOf(","));
  if (lastSep >= 0 && s.length - lastSep - 1 <= 2) {
    s = s.slice(0, lastSep).replace(/[.,]/g, "") + "." + s.slice(lastSep + 1);
  } else {
    s = s.replace(/[.,]/g, "");
  }
  const n = Number(s);
  return Number.isFinite(n) ? n : null;
}

const CODES = new Set(["USD", "EUR", "GBP", "JPY", "CAD", "AUD", "NZD", "CHF", "SEK", "NOK", "DKK", "MXN", "INR", "BRL"]);
export function parseAmounts(text) {
  const t = String(text || "");
  const out = [];
  for (const m of t.matchAll(PRE)) {
    const cur = SYMBOL[m[1]] || (CODES.has(m[1]) ? m[1] : null);
    if (!cur) continue;
    const n = toNumber(m[2]);
    if (n != null) out.push({ amount: n, currency: cur, symbol: m[1] });
  }
  for (const m of t.matchAll(POST)) {
    const cur = SYMBOL[m[2]] || (CODES.has(m[2]) ? m[2] : null);
    if (!cur) continue;
    const n = toNumber(m[1]);
    if (n != null) out.push({ amount: n, currency: cur, symbol: m[2] });
  }
  return out;
}

const DOLLARS = new Set(["USD", "CAD", "AUD", "NZD", "MXN"]);
// The amount check for a grant with --max-amount: the LARGEST amount found in `texts` (in the grant's currency; a
// bare "$" counts for any dollar currency) must be <= the cap. No amount found -> not ok (Kevin confirms).
export function amountCheck(g, texts) {
  if (g.maxAmount == null) return { ok: true, amount: null };
  const found = (texts || []).flatMap(parseAmounts).filter((a) =>
    a.currency === g.currency || (a.symbol === "$" && DOLLARS.has(g.currency)));
  if (!found.length) return { ok: false, amount: null, why: "no_amount" };
  const max = Math.max(...found.map((a) => a.amount));
  return { ok: max <= g.maxAmount, amount: max, currency: g.currency, ...(max > g.maxAmount ? { why: "over_cap" } : {}) };
}

export function fmtAmount(n, currency) {
  if (n == null) return "";
  try { return new Intl.NumberFormat("en-US", { style: "currency", currency }).format(n); } catch { return `${n} ${currency}`; }
}

// What the popup and `grant list` show (no secrets in grants by design; values are digests).
export function grantView(g, now = Date.now()) {
  return {
    id: g.id, op: g.op, origin: g.origin, target: g.target, quote: g.quote,
    ...(g.value != null ? { value: `<${g.value.length} chars>` } : {}),
    ...(g.urlPrefix ? { urlPrefix: g.urlPrefix } : {}),
    ...(g.maxAmount != null ? { maxAmount: g.maxAmount, currency: g.currency, ...(g.amountSelector ? { amountSelector: g.amountSelector } : {}) } : {}),
    uses: g.uses, usesLeft: g.usesLeft, expiresAt: g.expiresAt, expiresInSec: Math.max(0, Math.round((g.expiresAt - now) / 1000)),
    createdAt: g.createdAt,
  };
}
