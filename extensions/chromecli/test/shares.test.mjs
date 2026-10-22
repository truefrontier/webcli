import { test } from "node:test";
import assert from "node:assert/strict";
import { DEFAULT_POLICY } from "../policy.js";
import { badgeFor, levelAllows, revokeReason } from "../shares.js";

const share = { origin: "https://example.com", level: "read" };

test("same-origin navigation keeps the share", () => {
  assert.equal(revokeReason(share, "https://example.com/other?q=1", DEFAULT_POLICY), null);
});

test("cross-origin navigation revokes it", () => {
  assert.equal(revokeReason(share, "https://evil.example.org/", DEFAULT_POLICY), "origin changed");
  assert.equal(revokeReason(share, "http://example.com/", DEFAULT_POLICY), "origin changed");
  assert.equal(revokeReason(share, "chrome://newtab/", DEFAULT_POLICY), "left http(s)");
});

test("navigating to a denied path on the same origin revokes it", () => {
  assert.match(revokeReason(share, "https://example.com/checkout", DEFAULT_POLICY), /^denied/);
});

test("levels", () => {
  assert.ok(levelAllows("read", "read"));
  assert.ok(levelAllows("act", "read"));
  assert.ok(!levelAllows("read", "act"));
  assert.ok(levelAllows("act", "act"));
});

test("badges", () => {
  assert.deepEqual(badgeFor(share, false), { text: "R", color: "#1A73E8" });
  assert.equal(badgeFor({ ...share, level: "act" }, false).text, "A");
  assert.equal(badgeFor(share, true).text, "II");
  assert.equal(badgeFor(null, false).text, "");
});
