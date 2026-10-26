// 0.16.4: what the popup asks Chrome for when Kevin shares a tab must be requestable under this manifest.
// Chrome rejects (throws, no prompt) any origin that no SINGLE optional_host_permissions entry contains;
// grantedCovers emulates that rule (URLPatternSet::ContainsPattern / URLPattern::Contains).
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { grantedCovers } from "./harness.mjs";
import { expandSchemes, loosenRequest, shareDeniedMessage, shareRequest } from "../share-request.js";
import { makePendingShare } from "../pending-share.js";

const manifest = JSON.parse(readFileSync(new URL("../manifest.json", import.meta.url), "utf8"));
const optional = new Set(manifest.optional_host_permissions);
const requestable = (origins) => origins.every((o) => grantedCovers(optional, o));
const FRAMES = ["https://app.example.net"];

test("Chrome's rule, as emulated: *://*.apple.com/* is NOT requestable under http://*/* + https://*/* (the 0.16.3 bug)", () => {
  assert.equal(requestable(["*://*.apple.com/*"]), false);
  assert.equal(requestable(["https://*.apple.com/*", "http://*.apple.com/*"]), true);
});

test("every share request is requestable: apex, subdomains, http, multi-tenant, IP, localhost, Read and Act", () => {
  const tabs = ["https://apple.com", "https://www.apple.com", "https://developer.apple.com", "https://appstoreconnect.apple.com",
    "http://intranet.example.com", "https://alice.github.io", "https://a.vercel.app", "https://www.bbc.co.uk",
    "http://127.0.0.1:8765", "http://localhost:3000", "https://example.com:8443"];
  for (const origin of tabs) for (const level of ["read", "act"]) {
    const req = shareRequest(origin, level, FRAMES);
    assert.ok(req.origins.length > 0, origin);
    assert.ok(requestable(req.origins), `${origin} ${level}: ${req.origins.join(" ")}`);
    assert.ok(!req.origins.some((o) => o.startsWith("*")), `${origin}: no * scheme`);
    // The worker's pending share accepts the same list (0.16.2 Allow-closes-popup path).
    assert.doesNotThrow(() => makePendingShare({ tabId: 1, origin, level, origins: req.origins }), origin);
  }
});

test("apple.com apex and its subdomains ask for the same https://*.apple.com/*, which covers the apex too", () => {
  for (const o of ["https://apple.com", "https://www.apple.com", "https://developer.apple.com"]) {
    assert.deepEqual(shareRequest(o, "read").origins, ["https://*.apple.com/*"], o);
  }
  const granted = new Set(["https://*.apple.com/*"]);
  for (const o of ["https://apple.com/*", "https://www.apple.com/*", "https://appstoreconnect.apple.com/*"]) assert.ok(grantedCovers(granted, o), o);
  assert.equal(grantedCovers(granted, "http://apple.com/*"), false);
});

test("Act adds the frame_act_allow origins; Read doesn't", () => {
  assert.deepEqual(shareRequest("https://www.apple.com", "act", FRAMES).origins, ["https://*.apple.com/*", "https://app.example.net/*"]);
  assert.deepEqual(shareRequest("https://www.apple.com", "read", FRAMES).origins, ["https://*.apple.com/*"]);
});

test("the denied message names the real domain (no 'frame_act_allow app' text), and a refused request says so", () => {
  const act = shareRequest("https://www.apple.com", "act", FRAMES);
  assert.equal(shareDeniedMessage(act), "Chrome didn't grant access to *.apple.com (and app.example.net for its app frames); not shared.");
  assert.equal(shareDeniedMessage(shareRequest("https://apple.com", "read", FRAMES)), "Chrome didn't grant access to *.apple.com; not shared.");
  assert.equal(shareDeniedMessage(shareRequest("http://127.0.0.1:8765", "read")), "Chrome didn't grant access to 127.0.0.1; not shared.");
  const refused = shareDeniedMessage(shareRequest("https://apple.com", "read"), new Error("Only permissions specified in the manifest may be requested."));
  assert.equal(refused, "Chrome refused the request for *.apple.com: Only permissions specified in the manifest may be requested.; not shared.");
  for (const m of [shareDeniedMessage(act), refused]) assert.ok(!/frame_act_allow/.test(m));
});

test("accepting a policy that adds a host pattern requests http + https (was *://, also refused by Chrome)", () => {
  const origins = loosenRequest([{ kind: "act_allow_added", value: "*.example.com" }, { kind: "read_allow_added", value: "https://docs.example.org" },
    { kind: "deny_removed", value: "x" }]);
  assert.deepEqual(origins, ["http://*.example.com/*", "https://*.example.com/*", "https://docs.example.org/*"]);
  assert.ok(requestable(origins));
  assert.deepEqual(expandSchemes(["*://a.example/*", "https://a.example/*"]), ["http://a.example/*", "https://a.example/*"]);
});

test("popup.js requests exactly shareRequest's origins, with nothing awaited before chrome.permissions.request", () => {
  const src = readFileSync(new URL("../popup.js", import.meta.url), "utf8");
  const body = src.slice(src.indexOf("async function share(level) {"), src.indexOf("try { allowed = await chrome.permissions.request({ origins })"));
  assert.ok(body.includes("shareRequest(tab.origin, level"), "uses shareRequest");
  assert.ok(body.length > 0 && !/\bawait\b/.test(body), "no await before the request (user gesture)");
  assert.ok(!src.includes("frame_act_allow app sites"), "old message gone");
});
