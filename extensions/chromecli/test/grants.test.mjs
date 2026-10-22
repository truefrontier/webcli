// 0.10.0 pre-approval grants: the pure helpers.
import { test } from "node:test";
import assert from "node:assert/strict";
import { amountCheck, canSkip, grantMatches, nameMatches, parseAmounts, validateGrant, grantView, DEFAULT_TTL_SEC, MAX_TTL_SEC } from "../grants.js";

const base = { op: "click", origin: "https://shop.example.com", target: { text: "Place order" }, quote: "go ahead and buy it if it's under $50" };
const now = 1_700_000_000_000;

test("validateGrant: defaults (1 use, 30m), normalizes the origin, keeps the quote", () => {
  const g = validateGrant({ ...base, origin: "https://shop.example.com/cart?x=1" }, { now, id: "g1" });
  assert.equal(g.origin, "https://shop.example.com");
  assert.equal(g.uses, 1); assert.equal(g.usesLeft, 1);
  assert.equal(g.expiresAt, now + DEFAULT_TTL_SEC * 1000);
  assert.equal(g.quote, base.quote);
  assert.deepEqual(g.target, { text: "Place order" });
});

test("validateGrant: refusals", () => {
  const bad = (x, re) => assert.throws(() => validateGrant({ ...base, ...x }, { now }), (e) => e.code === "bad_request" && re.test(e.message));
  bad({ op: "eval" }, /op must be/);
  bad({ origin: "chrome://settings" }, /origin/);
  bad({ quote: "   " }, /quote is required/);
  bad({ quote: "x".repeat(501) }, /over 500/);
  bad({ target: null }, /needs --target-text/);
  bad({ target: { text: "a", selector: "#a" } }, /not both/);
  bad({ ttlSec: MAX_TTL_SEC + 1 }, /ttl/);
  bad({ ttlSec: 5 }, /ttl/);
  bad({ uses: 0 }, /uses/);
  bad({ uses: 21 }, /uses/);
  bad({ maxAmount: -1 }, /positive/);
  bad({ op: "fill", maxAmount: 5 }, /only applies to click/);
  bad({ currency: "USD" }, /need --max-amount/);
  bad({ value: "x" }, /--value only/);
  bad({ op: "navigate" }, /match on origin/);
  bad({ op: "navigate", target: null, urlPrefix: "https://evil.example/" }, /on the grant's origin/);
});

test("validateGrant: tab ops match on origin (+ url prefix)", () => {
  const g = validateGrant({ op: "open", origin: "https://shop.example.com", quote: "open the shop", urlPrefix: "https://shop.example.com/admin" }, { now });
  assert.equal(g.target, null);
  assert.ok(grantMatches(g, { op: "open", origin: g.origin, url: "https://shop.example.com/admin/orders", now }).ok);
  assert.equal(grantMatches(g, { op: "open", origin: g.origin, url: "https://shop.example.com/other", now }).ok, false);
});

test("nameMatches: case-insensitive exact or whole words, never a substring of a word", () => {
  assert.ok(nameMatches("Place order", "place ORDER"));
  assert.ok(nameMatches("Delete", "Delete store"));
  assert.ok(nameMatches("pay now", "Pay now — $49.00"));
  assert.ok(!nameMatches("Delete", "Undelete"));
  assert.ok(!nameMatches("order", "orders"));
  assert.ok(!nameMatches("", "anything"));
  assert.ok(nameMatches("Café", "Le café ouvert"));
});

test("grantMatches: op, origin, name, role, value, selector, expiry, uses", () => {
  const g = validateGrant({ ...base, target: { text: "Place order", role: "button" } }, { now });
  const el = { name: "Place order", role: "button" };
  const ctx = { op: "click", origin: base.origin, element: el, now };
  assert.ok(grantMatches(g, ctx).ok);
  assert.equal(grantMatches(g, { ...ctx, op: "press" }).why, "op");
  assert.equal(grantMatches(g, { ...ctx, origin: "https://other.example.com" }).why, "origin");
  assert.equal(grantMatches(g, { ...ctx, element: { name: "Cancel order", role: "button" } }).why, "target_text");
  assert.equal(grantMatches(g, { ...ctx, element: { name: "Place order", role: "link" } }).why, "role");
  assert.equal(grantMatches(g, { ...ctx, now: g.expiresAt + 1 }).why, "expired_or_used");
  assert.equal(grantMatches({ ...g, usesLeft: 0 }, ctx).why, "expired_or_used");
  const s = validateGrant({ ...base, target: { selector: "#place" } }, { now });
  assert.ok(grantMatches(s, { ...ctx, selectorHit: true }).ok);
  assert.equal(grantMatches(s, { ...ctx, selectorHit: false }).why, "selector");
  const f = validateGrant({ op: "fill", origin: base.origin, target: { text: "Store name" }, value: "as-described-review", quote: "type as-described-review to confirm" }, { now });
  const fctx = { op: "fill", origin: base.origin, element: { name: "Store name" }, now };
  assert.ok(grantMatches(f, { ...fctx, value: "as-described-review" }).ok);
  assert.equal(grantMatches(f, { ...fctx, value: "other" }).why, "value");
  const p = validateGrant({ op: "press", origin: base.origin, target: { text: "Search" }, value: "Enter", quote: "hit enter" }, { now });
  assert.ok(grantMatches(p, { op: "press", origin: base.origin, element: { name: "Search" }, key: "Enter", now }).ok);
});

test("canSkip: never for password / payment / credential / sensitive-name fields or eval", () => {
  assert.ok(canSkip(["submit_button"]));
  assert.ok(canSkip(["destructive_label", "jev:destructive"]));
  for (const r of ["password_field", "payment_field", "credential_field", "sensitive_name", "eval"]) assert.equal(canSkip(["submit_button", r]), false, r);
  assert.equal(canSkip([]), false);
});

test("parseAmounts: symbols, codes, thousands, decimal commas", () => {
  const a = (t) => parseAmounts(t).map((x) => [x.amount, x.currency]);
  assert.deepEqual(a("Total $49.99"), [[49.99, "USD"]]);
  assert.deepEqual(a("Order total: $1,299.00"), [[1299, "USD"]]);
  assert.deepEqual(a("USD 12.5"), [[12.5, "USD"]]);
  assert.deepEqual(a("Total 30.00 EUR"), [[30, "EUR"]]);
  assert.deepEqual(a("€12,50"), [[12.5, "EUR"]]);
  assert.deepEqual(a("£8"), [[8, "GBP"]]);
  assert.deepEqual(a("2 items, no money"), []);
});

test("amountCheck: largest amount in the grant's currency must be <= cap; none found -> confirm", () => {
  const g = validateGrant({ ...base, maxAmount: 49.99 }, { now });
  assert.deepEqual(amountCheck(g, ["Subtotal $40.00", "Shipping $5.00 Total $45.00"]).ok, true);
  assert.equal(amountCheck(g, ["Total $45.00"]).amount, 45);
  const over = amountCheck(g, ["Total $59.00"]);
  assert.equal(over.ok, false); assert.equal(over.why, "over_cap"); assert.equal(over.amount, 59);
  assert.equal(amountCheck(g, ["nothing here"]).why, "no_amount");
  assert.equal(amountCheck(g, ["Total €45.00"]).why, "no_amount", "other currencies don't count");
  assert.equal(amountCheck({ ...g, currency: "CAD" }, ["Total $45.00"]).ok, true, "a bare $ counts for dollar currencies");
  assert.equal(amountCheck(validateGrant(base, { now }), []).ok, true, "no cap -> no amount check");
});

test("grantView never echoes a --value, only its length", () => {
  const f = validateGrant({ op: "fill", origin: base.origin, target: { text: "Store name" }, value: "abc", quote: "q" }, { now });
  const v = grantView(f, now);
  assert.equal(v.value, "<3 chars>");
  assert.equal(v.expiresInSec, DEFAULT_TTL_SEC);
});
