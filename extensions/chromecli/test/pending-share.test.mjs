// 0.16.2: Act / Read picked while Chrome's "Allow / Cancel" site-access prompt is open is applied on Allow, even
// when the popup closed meanwhile (permissions.onAdded, or the popup's next open); Cancel leaves the tab as it was.
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadBackground } from "./harness.mjs";
import { PENDING_SHARE_TTL_MS, makePendingShare, pendingLive } from "../pending-share.js";

const ORIGIN = "https://example.com"; // tab 2 in the harness
const SITE = "https://example.com/*";
const FRAME = "https://app.example.net/*";
const shares = (h) => h.chrome.storage.session._data().shares || {};
const pending = (h) => h.chrome.storage.session._data().pendingShare ?? null;
const audits = async (h) => ((await h.chrome.storage.session.get("audit")).audit || []).map((a) => a.op);
const pick = (h, level = "act", origins = [SITE]) => h.message({ type: "pendingShare", tabId: 2, origin: ORIGIN, level, origins });

test("makePendingShare: the site's pattern first, frame origins kept, a 2-minute life; bad input throws", () => {
  const p = makePendingShare({ tabId: 2, origin: ORIGIN, level: "act", origins: [SITE, FRAME] }, 1000);
  assert.deepEqual(p, { tabId: 2, origin: ORIGIN, level: "act", origins: [SITE, FRAME], at: 1000,
    expiresAt: 1000 + PENDING_SHARE_TTL_MS });
  assert.deepEqual(makePendingShare({ tabId: 2, origin: ORIGIN, level: "read" }, 0).origins, [SITE]);
  assert.ok(pendingLive(p, 1000 + PENDING_SHARE_TTL_MS - 1) && !pendingLive(p, 1000 + PENDING_SHARE_TTL_MS));
  assert.ok(!pendingLive(null));
  for (const bad of [{ tabId: 2, origin: ORIGIN, level: "admin" }, { tabId: "2", origin: ORIGIN, level: "act" },
    { tabId: 2, origin: "file:///etc", level: "act" }, { tabId: 2, origin: ORIGIN, level: "act", origins: ["<all_urls>"] },
    { tabId: 2, origin: ORIGIN, level: "act", origins: ["https://*/*"] }]) {
    assert.throws(() => makePendingShare(bad), undefined, JSON.stringify(bad));
  }
});

test("Allow with the popup gone: permissions.onAdded applies the chosen mode", async () => {
  const h = await loadBackground();
  assert.equal((await pick(h, "act")).ok, true);
  assert.equal(pending(h).level, "act");
  assert.equal(shares(h)[2], undefined, "nothing is shared before Allow");
  h.granted.add(SITE);
  await h.chrome.permissions.onAdded.fire({ origins: [SITE] });
  assert.equal(shares(h)[2].level, "act");
  assert.equal(pending(h), null, "applied once, then cleared");
  assert.ok(h.posted.some((m) => m.event === "share_changed" && m.tab === 2 && m.level === "act" && m.reason === "shared after allow"));
  assert.ok((await audits(h)).includes("share-act"));
});

test("Read works the same way", async () => {
  const h = await loadBackground();
  await pick(h, "read");
  h.granted.add(SITE);
  await h.chrome.permissions.onAdded.fire({ origins: [SITE] });
  assert.equal(shares(h)[2].level, "read");
});

test("Allow seen on the popup's next open (onAdded missed): applied then", async () => {
  const h = await loadBackground();
  await pick(h, "act");
  h.granted.add(SITE); // no onAdded (the worker slept through it)
  const res = await h.message({ type: "applyPendingShare" });
  assert.deepEqual(res, { ok: true, applied: true, level: "act", tabId: 2 });
  assert.equal(shares(h)[2].level, "act");
  assert.equal(pending(h), null);
});

test("Cancel: nothing changes, and the popup's next open drops the choice", async () => {
  const h = await loadBackground();
  await h.share(2, "read"); // already shared read; Kevin clicks Act, then Cancel
  await pick(h, "act", [SITE, FRAME]); // Act also asks for a frame_act_allow site
  const res = await h.message({ type: "applyPendingShare" }); // the popup reopened: the prompt is closed
  assert.deepEqual(res, { ok: true, applied: false });
  assert.equal(shares(h)[2].level, "read", "the site was already granted, but Act asked for more and was cancelled");
  assert.equal(pending(h), null, "dropped");
  h.granted.add(FRAME); // a later, unrelated grant must not resurrect it
  await h.chrome.permissions.onAdded.fire({ origins: [FRAME] });
  assert.equal(shares(h)[2].level, "read");
});

test("Cancel with the popup still open: it clears the choice itself", async () => {
  const h = await loadBackground();
  await pick(h, "act");
  assert.equal((await h.message({ type: "clearPendingShare", tabId: 2 })).ok, true);
  assert.equal(pending(h), null);
  h.granted.add(SITE);
  await h.chrome.permissions.onAdded.fire({ origins: [SITE] });
  assert.equal(shares(h)[2], undefined);
});

test("a grant that doesn't cover every origin asked for waits (the prompt may still be open)", async () => {
  const h = await loadBackground();
  await pick(h, "act", [SITE, FRAME]);
  h.granted.add(SITE);
  await h.chrome.permissions.onAdded.fire({ origins: [SITE] });
  assert.equal(shares(h)[2], undefined);
  assert.equal(pending(h).level, "act", "kept");
  h.granted.add(FRAME);
  await h.chrome.permissions.onAdded.fire({ origins: [FRAME] });
  assert.equal(shares(h)[2].level, "act");
});

test("the popup's own answer (it stayed open): one share, and the choice is cleared", async () => {
  const h = await loadBackground();
  pick(h, "act"); // not awaited, as in the popup
  h.granted.add(SITE);
  assert.equal((await h.message({ type: "share", tabId: 2, origin: ORIGIN, level: "act" })).ok, true);
  await h.message({ type: "state" });
  assert.equal(pending(h), null);
  assert.equal((await audits(h)).filter((op) => op === "share-act").length, 1);
  await h.chrome.permissions.onAdded.fire({ origins: [SITE] }); // a late onAdded does nothing more
  assert.equal((await audits(h)).filter((op) => op === "share-act").length, 1);
});

test("onAdded first, then the still-open popup's own share: applied once", async () => {
  const h = await loadBackground();
  await pick(h, "act");
  h.granted.add(SITE);
  await h.chrome.permissions.onAdded.fire({ origins: [SITE] });
  assert.equal((await h.message({ type: "share", tabId: 2, origin: ORIGIN, level: "act" })).ok, true);
  assert.equal((await audits(h)).filter((op) => op === "share-act").length, 1);
  assert.equal(h.posted.filter((m) => m.event === "share_changed").length, 1);
  // a later, deliberate click shares again as before
  assert.equal((await h.message({ type: "share", tabId: 2, origin: ORIGIN, level: "read" })).ok, true);
  assert.equal(shares(h)[2].level, "read");
});

test("an expired choice is never applied", async () => {
  const h = await loadBackground();
  await pick(h, "act");
  const p = pending(h);
  await h.chrome.storage.session.set({ pendingShare: { ...p, expiresAt: Date.now() - 1 } });
  h.granted.add(SITE);
  await h.chrome.permissions.onAdded.fire({ origins: [SITE] });
  assert.equal(shares(h)[2], undefined);
  assert.equal(pending(h), null);
});

test("the tab moved to another site before Allow: not shared, choice dropped", async () => {
  const h = await loadBackground();
  await pick(h, "act");
  h.tabs.get(2).url = "https://other.example.org/";
  h.granted.add(SITE);
  const res = await h.message({ type: "applyPendingShare" });
  assert.equal(res.ok, false);
  assert.equal(res.applied, false);
  assert.equal(shares(h)[2], undefined);
  assert.equal(pending(h), null);
});

test("already shared at that level: nothing re-posted", async () => {
  const h = await loadBackground();
  await h.share(2, "act");
  await pick(h, "act");
  const res = await h.message({ type: "applyPendingShare" });
  assert.equal(res.already, true);
  assert.equal((await audits(h)).filter((op) => op === "share-act").length, 1);
});

test("bad input and other senders are refused", async () => {
  const h = await loadBackground();
  assert.equal((await h.message({ type: "pendingShare", tabId: 2, origin: ORIGIN, level: "owner" })).ok, false);
  assert.equal((await h.message({ type: "pendingShare", tabId: 2, origin: ORIGIN, level: "act", origins: ["https://*/*"] })).ok, false);
  assert.equal(pending(h), null);
  // a web page (sender.tab), the confirm window, and the options page can't set one
  assert.equal(await h.message({ type: "pendingShare", tabId: 2, origin: ORIGIN, level: "act" }, "popup.html", { id: 7 }), undefined);
  assert.equal(await h.message({ type: "pendingShare", tabId: 2, origin: ORIGIN, level: "act" }, "confirm.html?id=x", { id: 999 }), undefined);
  assert.equal(await h.message({ type: "applyPendingShare" }, "options.html", { id: 5 }), undefined);
  assert.equal(pending(h), null);
});

test("popup: Act / Read still asks Chrome straight from the click, after handing the choice to the worker", async () => {
  const { readFileSync } = await import("node:fs");
  const src = readFileSync(new URL("../popup.js", import.meta.url), "utf8");
  const body = src.slice(src.indexOf("async function share(level)"), src.indexOf("async function unshare()"));
  const at = body.indexOf("await chrome.permissions.request");
  assert.ok(at > 0);
  const before = body.slice(0, at).replace(/\/\/.*$/gm, ""); // code only, up to the request
  assert.ok(before.includes('type: "pendingShare"'), "the choice is sent first");
  assert.ok(!/\bawait\b/.test(before), "nothing is awaited before permissions.request (user gesture)");
  assert.ok(body.includes('type: "clearPendingShare"'), "Cancel clears it");
  assert.ok(src.includes('send({ type: "applyPendingShare" })'), "the popup applies a pending choice when it opens");
});

test("0.16.4: Chrome refusing the request itself (no prompt) is kept in the activity panel; a plain Cancel isn't", async () => {
  const h = await loadBackground();
  await pick(h, "act");
  await h.message({ type: "clearPendingShare", tabId: 2 });
  assert.ok(!(await audits(h)).includes("share-request"));
  await pick(h, "act");
  await h.message({ type: "clearPendingShare", tabId: 2, error: "Only permissions specified in the manifest may be requested." });
  const { audit } = await h.chrome.storage.session.get("audit");
  const row = audit.find((a) => a.op === "share-request");
  assert.equal(row.origin, ORIGIN);
  assert.equal(row.outcome, "refused: Only permissions specified in the manifest may be requested.");
  assert.equal(pending(h), null);
});
