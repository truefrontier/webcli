// gate.js (spec 6.7): what's always gated, and what isn't.
import { test } from "node:test";
import assert from "node:assert/strict";
import { displayValue, gateReasons, isSensitiveField, reasonLabel, SUBMIT_WORDS } from "../gate.js";

const btn = (name, extra = {}) => ({ tag: "button", role: "button", name, type: "button", inForm: false, ...extra });
const input = (extra = {}) => ({ tag: "input", role: "textbox", name: "", type: "text", inForm: true, isField: true, ...extra });

test("eval is always gated", () => {
  assert.deepEqual(gateReasons("eval", null), ["eval"]);
});

test("submit buttons in a form are gated (default type and type=submit)", () => {
  assert.ok(gateReasons("click", btn("Continue", { type: "submit", inForm: true, defaultSubmit: true })).includes("submit_button"));
  assert.ok(gateReasons("click", btn("Continue", { type: "", inForm: true })).includes("submit_button"));
  assert.deepEqual(gateReasons("click", { tag: "input", type: "submit", value: "Go", inForm: true }), ["submit_input"]);
  assert.deepEqual(gateReasons("click", { tag: "input", type: "image", inForm: false }), ["submit_input"]);
});

test("'Place order', 'Delete', and the other submit words are gated anywhere", () => {
  assert.deepEqual(gateReasons("click", btn("Place order", { type: "submit", inForm: true, defaultSubmit: true })), ["submit_button", "submit_word:order"]);
  assert.deepEqual(gateReasons("click", btn("Delete account")), ["submit_word:delete"]);
  assert.deepEqual(gateReasons("click", { tag: "a", role: "link", name: "Sign out" }), ["submit_word:signout"]);
  assert.deepEqual(gateReasons("click", { tag: "div", role: "button", name: "", title: "Publish now" }), ["submit_word:publish"]);
  for (const w of ["buy", "purchase", "order", "pay", "checkout", "send", "post", "publish", "tweet", "reply", "submit",
    "confirm", "delete", "remove", "destroy", "transfer", "withdraw", "subscribe", "unsubscribe", "accept", "agree",
    "approve", "sign out", "signout", "log out", "logout", "merge", "deploy"]) {
    assert.ok(SUBMIT_WORDS.test(`Please ${w} now`), w);
    assert.equal(gateReasons("click", btn(w[0].toUpperCase() + w.slice(1))).length, 1, w);
  }
});

test("plain links, tabs, and non-submit buttons are not gated", () => {
  assert.deepEqual(gateReasons("click", { tag: "a", role: "link", name: "Next", inForm: true }), []);
  assert.deepEqual(gateReasons("click", { tag: "a", role: "tab", name: "Settings" }), []);
  assert.deepEqual(gateReasons("click", btn("Show more")), []);
  assert.deepEqual(gateReasons("click", btn("Next", { type: "button", inForm: true })), []);
  // word boundaries: "Postcode", "Repayment schedule", "Orders" don't match a whole word
  assert.deepEqual(gateReasons("click", { tag: "a", role: "link", name: "Postcode help" }), []);
  assert.deepEqual(gateReasons("click", { tag: "a", role: "link", name: "Repayments" }), []);
});

test("Enter in a form field is gated; Enter outside a form, or Escape, is not", () => {
  assert.deepEqual(gateReasons("press", input({ type: "email" }), { key: "Enter" }), ["enter_in_form"]);
  assert.deepEqual(gateReasons("press", input({ tag: "textarea", type: "" }), { key: "Enter" }), ["enter_in_form"]);
  assert.deepEqual(gateReasons("press", input({ inForm: false }), { key: "Enter" }), []);
  assert.deepEqual(gateReasons("press", input(), { key: "Escape" }), []);
  // Enter on a submit-like button activates it like a click
  assert.deepEqual(gateReasons("press", btn("Send"), { key: "Enter" }), ["submit_word:send"]);
});

test("type / fill into password, cc-*, credential autocomplete, or card-ish names is gated", () => {
  assert.deepEqual(gateReasons("fill", input({ type: "password" })), ["password_field"]);
  assert.deepEqual(gateReasons("type", input({ autocomplete: "cc-number" })), ["payment_field"]);
  assert.deepEqual(gateReasons("fill", input({ autocomplete: "cc-csc" })), ["payment_field"]);
  assert.deepEqual(gateReasons("fill", input({ autocomplete: "one-time-code" })), ["credential_field"]);
  assert.deepEqual(gateReasons("fill", input({ autocomplete: "new-password" })), ["credential_field"]);
  assert.deepEqual(gateReasons("fill", input({ type: "password", autocomplete: "current-password" })), ["password_field", "credential_field"]);
  for (const nm of ["card_number", "cvv", "cvc2", "iban", "routingNumber", "ssn"]) {
    assert.deepEqual(gateReasons("fill", input({ nameAttr: nm })), ["sensitive_name"], nm);
  }
  assert.deepEqual(gateReasons("fill", input({ label: "Card number" })), ["sensitive_name"]);
  assert.deepEqual(gateReasons("fill", input({ type: "email", autocomplete: "email", nameAttr: "email" })), []);
  assert.deepEqual(gateReasons("select", { tag: "select", nameAttr: "country" }), []);
});

test("open / navigate outside the allowlists is gated", () => {
  assert.deepEqual(gateReasons("open", null, { allowlisted: false }), ["origin_not_allowlisted"]);
  assert.deepEqual(gateReasons("navigate", null, { allowlisted: false }), ["origin_not_allowlisted"]);
  assert.deepEqual(gateReasons("open", null, { allowlisted: true }), []);
});

test("unknown ops fail closed", () => {
  assert.deepEqual(gateReasons("teleport", null), ["unknown_op"]);
});

test("values are masked for sensitive fields only", () => {
  const pw = input({ type: "password" });
  const d = displayValue("hunter2", pw);
  assert.equal(d.masked, true);
  assert.ok(!d.text.includes("hunter2"));
  assert.equal(d.chars, 7);
  assert.equal(isSensitiveField(pw), true);
  assert.deepEqual(displayValue("kevin@example.com", input({ type: "email" })), { masked: false, text: "kevin@example.com", chars: 17 });
  assert.equal(displayValue("x".repeat(300), input()).text.length, 201);
  assert.equal(displayValue(null, input()), null);
});

test("reason labels read well", () => {
  assert.match(reasonLabel("submit_word:order"), /order/);
  assert.match(reasonLabel("password_field"), /Password/);
  assert.match(reasonLabel("jev:purchase"), /Jev/);
});
