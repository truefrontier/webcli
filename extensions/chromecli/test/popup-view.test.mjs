import { test } from "node:test";
import assert from "node:assert/strict";
import { auditRow, hostOf, levelOf, loosenLabel, monogram, safeFavicon, shortcutKeys, statusOf } from "../popup-view.js";

test("status pill: paused wins, then connected, else disconnected", () => {
  assert.equal(statusOf({ paused: true, host: { connected: true } }).key, "paused");
  assert.equal(statusOf({ paused: false, host: { connected: true } }).label, "Connected");
  assert.equal(statusOf({ host: { connected: false } }).key, "disconnected");
  assert.equal(statusOf(null).key, "disconnected");
});

test("share level maps to off / read / act", () => {
  assert.equal(levelOf(null), "off");
  assert.equal(levelOf({ level: "read" }), "read");
  assert.equal(levelOf({ level: "act" }), "act");
  assert.equal(levelOf({ level: "bogus" }), "off");
});

test("shortcut key caps", () => {
  assert.deepEqual(shortcutKeys("Command+Shift+Period"), ["⌘", "⇧", "."]);
  assert.deepEqual(shortcutKeys("Ctrl+Shift+Period"), ["Ctrl", "⇧", "."]);
  assert.deepEqual(shortcutKeys("⌘⇧."), ["⌘", "⇧", "."]);
  assert.deepEqual(shortcutKeys(""), []);
  assert.deepEqual(shortcutKeys(undefined), []);
});

test("audit rows: time, host-only target, outcome tone", () => {
  const r = auditRow({ at: "2026-10-22T09:41:07-05:00", op: "read", tab: 4, origin: "https://www.github.com", outcome: "ok" });
  assert.deepEqual(r, { time: "09:41:07", op: "read", target: "tab 4 www.github.com", outcome: "ok", tone: "ok" });
  assert.equal(auditRow({ at: "", op: "read", tab: 4, origin: null, outcome: "origin_blocked" }).tone, "bad");
  assert.equal(auditRow({ op: "read", outcome: "paused" }).tone, "bad");
  assert.equal(auditRow({ op: "unshare", tab: 2, outcome: "origin changed" }).tone, "neutral");
  assert.equal(auditRow({ op: "policy-accept", tab: null, outcome: "abc123" }).tone, "neutral");
  assert.equal(auditRow({ op: "list", tab: null, origin: null, outcome: "ok" }).target, "");
});

test("pending labels, hosts, monograms, favicons", () => {
  assert.equal(loosenLabel({ kind: "read_allow_added" }), "Allow read");
  assert.equal(loosenLabel({ kind: "deny_removed" }), "Remove deny");
  assert.equal(loosenLabel({ kind: "something_new" }), "something new");
  assert.equal(hostOf("https://example.com:8443"), "example.com:8443");
  assert.equal(hostOf("not a url"), "not a url");
  assert.equal(monogram("Hello", "https://www.github.com"), "G");
  assert.equal(monogram("hello", null), "H");
  assert.equal(monogram("", ""), "?");
  assert.equal(safeFavicon("https://github.com/favicon.ico"), "https://github.com/favicon.ico");
  assert.equal(safeFavicon("data:image/png;base64,AAAA"), "data:image/png;base64,AAAA");
  assert.equal(safeFavicon("chrome://favicon/x"), null);
  assert.equal(safeFavicon("javascript:alert(1)"), null);
  assert.equal(safeFavicon(undefined), null);
});
