// Clause-aware pre-approval checks (0.11.5). One message from Kevin often approves several steps at once:
//   "Yes, pre-approve chromecli clicks on as-described-review for the next 45 minutes: Install once, Approve on the
//    $0 Starter plan once, ..., and Undo 3 times."
// Before 0.11.5 Jev judged each single grant against that WHOLE message ("does it authorize exactly this grant?"), so
// every grant looked like a fraction of the intent (unclear) or the other items looked like extra scope (overreach).
// Now code splits the quote into a shared preamble (site, store, time window) and item clauses; Jev maps each grant to
// its one best clause (one call for the whole batch), code checks the clause's limits (uses, amount, time window), and
// Jev then scores each grant against the preamble plus ONLY its clause (a second call). Everything fails closed.
// Pure ES module (node --test).
import { AUTHORIZED_FLOOR, DEFAULT_TTL_SEC, MIN_TTL_SEC, SCOPE_CRITERIA, SCOPE_UNTRUSTED, fmtAmount, scopeView } from "./grants.js";

export const MAP_FLOOR = 0.8; // Jev must map a grant to a clause at >= this, else the grant matches no clause (unclear)
export const BATCH_MAX = 20;

const clean = (s) => String(s ?? "").replace(/\s+/g, " ").trim();
const tidy = (s) => clean(s).replace(/^(?:and|then|also|plus)\s+/i, "").replace(/[\s.;,!]+$/, "").trim();

// ---- quote parsing
const QAMOUNT = /(?:[$€£]\s?(\d[\d,]*(?:\.\d+)?))|(?:\b(\d[\d,]*(?:\.\d+)?)\s?(?:dollars|usd|bucks|euros?|eur|pounds|gbp)\b)/gi;
// Every amount a clause states, $0 included ("the $0 Starter plan" is a cap of zero).
export function clauseAmounts(text) {
  const out = [];
  for (const m of String(text || "").matchAll(QAMOUNT)) {
    const n = Number(String(m[1] ?? m[2]).replace(/,/g, ""));
    if (Number.isFinite(n) && n >= 0) out.push(n);
  }
  return out;
}

const WORD_N = { one: 1, two: 2, three: 3, four: 4, five: 5, six: 6, seven: 7, eight: 8, nine: 9, ten: 10 };
// Uses a clause states: once / twice / thrice / N times / Nx / xN / ×N. Several -> the smallest; none -> null.
export function clauseUses(text) {
  const q = ` ${String(text || "").toLowerCase()} `;
  const found = [];
  if (/\b(once|one time|a single time|just one time)\b/.test(q)) found.push(1);
  if (/\btwice\b/.test(q)) found.push(2);
  if (/\bthrice\b/.test(q)) found.push(3);
  for (const m of q.matchAll(/\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s*(?:times|x)\b/g)) found.push(WORD_N[m[1]] ?? Number(m[1]));
  for (const m of q.matchAll(/(?:^|\s)[x×]\s?(\d+)\b/g)) found.push(Number(m[1]));
  const ok = found.filter((n) => Number.isInteger(n) && n >= 1);
  return ok.length ? Math.min(...ok) : null;
}

// A time window: "for the next 45 minutes", "within 2 hours", "for 1h". Seconds or null.
export function windowSec(text) {
  const m = String(text || "").toLowerCase()
    .match(/\b(?:for|within|in|over|during)\s+(?:the\s+)?(?:next\s+)?(\d+)\s*(minutes?|mins?|hours?|hrs?|h|m)\b/)
    || String(text || "").toLowerCase().match(/\bnext\s+(\d+)\s*(minutes?|mins?|hours?|hrs?|h|m)\b/);
  if (!m) return null;
  return Number(m[1]) * (/^h/.test(m[2]) ? 3600 : 60);
}

// The first colon that introduces a list (not "https://", not a time like 10:30).
const LIST_COLON = /:(?!\/\/)(?!\d)/;
const BULLET = /\s(?:[•·▪*-]|\(?\d{1,2}[.)])\s+/g;

function splitItems(rest) {
  // bullets / numbered markers, then ";" and ", " (optionally ", and" / ", then").
  const parts = (` ${rest}`).replace(BULLET, " ; ").split(/\s*;\s*|,\s+(?=\S)/).map(tidy).filter(Boolean);
  // "A once and B 3 times": split on " and " only when both sides state their own uses.
  const out = [];
  for (const p of parts) {
    const bits = p.split(/\s+and\s+/i);
    if (bits.length > 1 && bits.every((b) => clauseUses(b) != null)) out.push(...bits.map(tidy).filter(Boolean));
    else out.push(p);
  }
  return out;
}

// -> { multi, preamble, windowSec, clauses: [{id, text, uses, amount, windowSec}] }. A quote without a list after a
// colon (or with fewer than two items) is one clause: the whole quote, judged as before 0.11.5.
export function splitQuote(quote) {
  const q = clean(quote);
  const m = q.match(LIST_COLON);
  if (m) {
    const preamble = tidy(q.slice(0, m.index));
    const items = splitItems(q.slice(m.index + 1));
    if (preamble && items.length >= 2) {
      const win = windowSec(preamble);
      return { multi: true, preamble, windowSec: win, clauses: items.map((text, i) => clauseOf(text, i, win)) };
    }
  }
  return { multi: false, preamble: "", windowSec: windowSec(q), clauses: [clauseOf(q, 0, windowSec(q))] };
}

function clauseOf(text, i, win) {
  const amounts = clauseAmounts(text);
  const own = windowSec(text);
  return { id: `c${i + 1}`, text, uses: clauseUses(text), amount: amounts.length ? Math.min(...amounts) : null,
    windowSec: own != null && (win == null || own < win) ? own : win };
}

// Small stable hash (FNV-1a) for the per-clause uses ledger key. Not security-relevant: a collision only refuses.
export function quoteKey(quote) {
  let h = 0x811c9dc5;
  for (const ch of clean(quote)) { h ^= ch.codePointAt(0); h = Math.imul(h, 0x01000193) >>> 0; }
  return h.toString(16).padStart(8, "0");
}

// ---- page context (what tab the grant is for): title, URL path, store name. From the browser's tab, labelled as
// untrusted evidence for Jev; the store (if any) also pins the grant to that store's path.
export function pageContext(url, title) {
  let u;
  try { u = new URL(String(url || "")); } catch { return null; }
  if (!["http:", "https:"].includes(u.protocol)) return null;
  const path = u.pathname.slice(0, 200);
  const out = { url: `${u.origin}${path}`, path, ...(clean(title) ? { title: clean(title).slice(0, 120) } : {}) };
  let m = u.hostname === "admin.shopify.com" && path.match(/^\/store\/([a-z0-9][a-z0-9-]{0,62})(?:\/|$)/i);
  if (m) { out.store = m[1].toLowerCase(); out.pathPrefix = `/store/${out.store}`; return out; }
  m = u.hostname.match(/^([a-z0-9][a-z0-9-]{0,62})\.myshopify\.com$/i);
  if (m) out.store = m[1].toLowerCase();
  return out;
}

// ---- Jev call 1: map every grant to one clause.
export function mapBody({ model, split, grants }) {
  const items = Object.fromEntries(split.clauses.map((c) => [c.id, c.text]));
  const state = { preamble: split.preamble, items, grants: {} };
  const questions = {};
  grants.forEach((g, i) => {
    const k = `g${i + 1}`;
    state.grants[k] = scopeView(g);
    const criteria = Object.fromEntries(split.clauses.map((c) => [c.id, `Item ${c.id} ("${c.text}") approves this action on this target.`]));
    criteria.none = "No single item approves this grant's action on this target.";
    questions[`map_${k}`] = { type: "choice", criteria,
      instructions: `\`preamble\` and \`items\` are one message from the user approving several actions; each item is a separate approval. ` +
        `Which ONE item approves grant ${k} in \`grants\` (same action on the same target, read together with \`preamble\`)? ` +
        `Answer none if no item does. ${SCOPE_UNTRUSTED}` };
  });
  return { model, state, questions };
}

function choiceOf(a, keys) {
  if (!a || typeof a.choice !== "string" || !keys.includes(a.choice)) return null;
  let conf = typeof a.confidence === "number" ? a.confidence : null;
  if (conf === null && a.probabilities && typeof a.probabilities[a.choice] === "number") conf = a.probabilities[a.choice];
  return { choice: a.choice, confidence: conf };
}

// -> [{clauseId|null, confidence}] per grant. A missing/unknown answer is a Jev error (fail closed by the caller).
export function parseMap(resp, split, n, { floor = MAP_FLOOR } = {}) {
  const answers = resp && typeof resp === "object" && resp.answers && typeof resp.answers === "object" ? resp.answers : null;
  if (!answers) throw new Error("Jev's response has no answers.");
  const keys = [...split.clauses.map((c) => c.id), "none"];
  return Array.from({ length: n }, (_, i) => {
    const c = choiceOf(answers[`map_g${i + 1}`], keys);
    if (!c) throw new Error(`Jev gave no usable clause for grant ${i + 1}.`);
    const ok = c.choice !== "none" && c.confidence !== null && c.confidence >= floor;
    return { clauseId: ok ? c.choice : null, rawChoice: c.choice, confidence: c.confidence };
  });
}

// ---- deterministic limits of a clause
const STOP = new Set(["the", "a", "an", "to", "of", "for", "on", "in", "my", "and", "or", "it", "this", "that", "with", "click",
  "button", "link", "once", "twice", "times", "all", "at", "x"]);
const words = (s) => String(s || "").toLowerCase().split(/[^\p{L}\p{N}$]+/u).filter((w) => w && !STOP.has(w));

// Does the grant stay within clause c? used: uses of c already spent (ledger + earlier grants in this batch).
// -> { ok, verdict?, reason?, grant } (the grant with its TTL capped to the window when the CLI didn't set one).
export function checkClause(g, c, { used = 0 } = {}) {
  const over = (reason) => ({ ok: false, verdict: "overreach", reason, grant: g });
  if (g.target?.text) {
    const tw = words(g.target.text);
    const cw = new Set(words(c.text));
    if (tw.length && !tw.some((w) => cw.has(w))) {
      return { ok: false, verdict: "unclear", reason: `The target "${g.target.text}" shares no word with the matched item "${c.text}".`, grant: g };
    }
  }
  const allowed = c.uses ?? 1;
  if (g.uses + used > allowed) {
    return over(`"${c.text}" approves ${allowed} use${allowed === 1 ? "" : "s"}${used ? ` (${used} already granted)` : ""}; this grant asks for ${g.uses}.`);
  }
  if (c.amount != null && ["click", "press"].includes(g.op)) {
    if (g.maxAmount == null) return over(`"${c.text}" states ${fmtAmount(c.amount, g.currency || "USD")}; the grant has no --max-amount (add --max-amount ${c.amount}).`);
    if (g.maxAmount > c.amount) return over(`The grant's cap ${fmtAmount(g.maxAmount, g.currency)} is over the ${fmtAmount(c.amount, g.currency)} in "${c.text}".`);
  }
  let out = g;
  if (c.windowSec != null) {
    if (g.ttlExplicit && g.ttlSec > c.windowSec) {
      return over(`The grant lasts ${Math.round(g.ttlSec / 60)}m; the quote's window is ${Math.round(c.windowSec / 60)}m.`);
    }
    if (!g.ttlExplicit) {
      const ttl = Math.max(MIN_TTL_SEC, c.windowSec);
      out = { ...g, ttlSec: ttl, expiresAt: g.createdAt + ttl * 1000 };
    }
  } else if (!g.ttlExplicit && g.ttlSec !== DEFAULT_TTL_SEC) {
    out = { ...g, ttlSec: DEFAULT_TTL_SEC, expiresAt: g.createdAt + DEFAULT_TTL_SEC * 1000 };
  }
  return { ok: true, grant: out };
}

// ---- Jev call 2: each grant against the preamble plus ONLY its clause.
export const ITEM_NOTE = "The instruction is the preamble of a longer message plus ONE item from it; the message approved " +
  "several actions as separate items, and the other items are approved separately. Judge this grant against this item only.";

export function itemInstruction(split, c) {
  return split.preamble ? `${split.preamble}: ${c.text}` : c.text;
}

export function clauseScopeBody({ model, split, pairs }) {
  const state = { approvals: {} };
  const questions = {};
  for (const { key, grant, clause } of pairs) {
    state.approvals[key] = { instruction: itemInstruction(split, clause), grant: scopeView(grant) };
    questions[`scope_${key}`] = { type: "choice", criteria: { ...SCOPE_CRITERIA },
      instructions: `Does \`approvals.${key}.instruction\` clearly authorize exactly the pre-approval in \`approvals.${key}.grant\`? ` +
        `${ITEM_NOTE} ${SCOPE_UNTRUSTED}` };
  }
  return { model, state, questions };
}

// -> { verdict, rawVerdict, confidence, ok, reason }
export function parseClauseScope(resp, key, { floor = AUTHORIZED_FLOOR } = {}) {
  const answers = resp && typeof resp === "object" && resp.answers && typeof resp.answers === "object" ? resp.answers : null;
  const c = choiceOf(answers?.[`scope_${key}`], Object.keys(SCOPE_CRITERIA));
  if (!c) throw new Error(`Jev gave no usable scope verdict for ${key}.`);
  const ok = c.choice === "authorized" && c.confidence !== null && c.confidence >= floor;
  const pct = c.confidence === null ? "?" : c.confidence.toFixed(2);
  const reason = ok ? `Jev: the quote's item authorizes this grant (${pct}).`
    : c.choice === "authorized" ? `Jev leans "authorized" but only at ${pct} (needs >= ${floor}); treated as unclear.`
    : c.choice === "overreach" ? `Jev: the grant is broader than its item in the quote (overreach, ${pct}).`
    : `Jev: the item doesn't clearly authorize this grant (unclear, ${pct}).`;
  return { verdict: ok ? "authorized" : c.choice === "authorized" ? "unclear" : c.choice, rawVerdict: c.choice, confidence: c.confidence, ok, reason };
}
