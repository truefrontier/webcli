import { test } from "node:test";
import assert from "node:assert/strict";
import { auditRow, hostOf, jevView, levelOf, loosenLabel, monogram, safeFavicon, shortcutKeys, statusOf } from "../popup-view.js";

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

test("confirmsLabel", async () => {
  const { confirmsLabel } = await import("../popup-view.js");
  assert.equal(confirmsLabel(0), "");
  assert.equal(confirmsLabel(1), "1 agent action waits for your OK");
  assert.equal(confirmsLabel(3), "3 agent actions wait for your OK");
});

test("Jev row: on/off from jevConfigured only", () => {
  assert.equal(jevView(true).chip, "On");
  assert.equal(jevView(true).on, true);
  assert.equal(jevView(false).chip, "Off");
  assert.match(jevView(false).sub, /TypeSafe key/);
});

test("0.10.0 pre-approval rows: scope, uses/expiry, subtitle", async () => {
  const { grantScope, grantMeta, grantsSub } = await import("../popup-view.js");
  assert.equal(grantScope({ op: "click", origin: "https://shop.example.com", target: { text: "Place order" }, maxAmount: 49.99, currency: "USD" }),
    "Click “Place order” · ≤ $49.99");
  assert.equal(grantScope({ op: "click", origin: "https://a.example", target: { text: "Delete", role: "button" } }), "Click “Delete” (button)");
  assert.equal(grantScope({ op: "fill", origin: "https://a.example", target: { selector: "#name" } }), "Fill #name");
  assert.equal(grantScope({ op: "press", origin: "https://a.example", target: { text: "Search" }, value: "Enter" }), "Press Enter in “Search”");
  assert.equal(grantScope({ op: "open", origin: "https://a.example", urlPrefix: "https://a.example/admin" }), "Open a.example/admin");
  assert.equal(grantScope({ op: "navigate", origin: "https://a.example" }), "Go to a.example");
  const now = 1_000_000;
  assert.equal(grantMeta({ usesLeft: 1, expiresAt: now + 28 * 60_000 }, now), "1 use left · 28m left");
  assert.equal(grantMeta({ usesLeft: 3, expiresAt: now + 65 * 60_000 }, now), "3 uses left · 1h 5m left");
  assert.equal(grantMeta({ usesLeft: 1, expiresAt: now - 1 }, now), "expired");
  assert.match(grantsSub(0), /^None/);
  assert.match(grantsSub(2), /^2 active/);
});

test("0.10.1: Jev verdict chip on grant rows; the popup has no pre-approvals toggle", async () => {
  const { grantJev } = await import("../popup-view.js");
  assert.equal(grantJev({}), null);
  assert.deepEqual(grantJev({ jev: { verdict: "authorized", confidence: 0.94, narrowed: [] } }).label, "Jev ✓ 0.94");
  const n = grantJev({ jev: { verdict: "authorized", confidence: 0.9, narrowed: [{ field: "maxAmount" }, { field: "uses" }] } });
  assert.equal(n.label, "Jev ✓ 0.90 · narrowed");
  assert.match(n.title, /narrowed cap, uses/);
  const fs = await import("node:fs");
  const html = fs.readFileSync(new URL("../popup.html", import.meta.url), "utf8");
  const js = fs.readFileSync(new URL("../popup.js", import.meta.url), "utf8");
  assert.doesNotMatch(html, /grants-toggle|Allow agent pre-approvals|role="switch" aria-checked="false" aria-labelledby="grants/);
  assert.doesNotMatch(js, /setGrantsEnabled|grantsEnabled/);
});
