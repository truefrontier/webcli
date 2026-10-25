// 0.16.3: a share follows its tab across hosts of the same site (registrable domain, by the Public Suffix List) and
// keeps its level; another site, a denied host or path, or https -> http still ends it, as before.
import { test } from "node:test";
import assert from "node:assert/strict";
import { registrableDomain, sameSite, siteOf, sitePermissionPattern } from "../psl.js";
import { PSL_VERSION } from "../psl-data.js";
import { carrySite } from "../shares.js";
import { DEFAULT_POLICY } from "../policy.js";
import { makePendingShare } from "../pending-share.js";
import { siteNote } from "../popup-view.js";
import { loadBackground } from "./harness.mjs";

test("registrableDomain: eTLD+1 from the bundled list", () => {
  assert.match(PSL_VERSION, /^\d{4}-\d{2}-\d{2}/);
  const cases = {
    "developer.apple.com": "apple.com", "appstoreconnect.apple.com": "apple.com", "apple.com": "apple.com",
    "appleid.apple.com": "apple.com", "accounts.google.com": "google.com", "Developer.Apple.COM.": "apple.com",
    "www.example.co.uk": "example.co.uk", "a.b.example.com.au": "example.com.au",
    // multi-tenant suffixes (the list's private section): each tenant is its own site
    "alice.github.io": "alice.github.io", "x.alice.github.io": "alice.github.io",
    "my-app.vercel.app": "my-app.vercel.app", "preview.my-app.vercel.app": "my-app.vercel.app",
    "blog.pages.dev": "blog.pages.dev", "shop.herokuapp.com": "shop.herokuapp.com",
    // wildcard and exception rules (*.ck, !www.ck)
    "www.ck": "www.ck", "a.b.ck": "a.b.ck",
    // no site: a public suffix itself, an IP, a single label, junk
    "github.io": null, "vercel.app": null, "co.uk": null, "com": null, "b.ck": null,
    "127.0.0.1": null, "localhost": null, "": null, "exa mple.com": null, "[::1]": null,
  };
  for (const [host, want] of Object.entries(cases)) assert.equal(registrableDomain(host), want, host);
});

test("sameSite: one registrable domain, http(s), never https -> http", () => {
  assert.ok(sameSite("https://appstoreconnect.apple.com", "https://developer.apple.com"));
  assert.ok(sameSite("https://developer.apple.com", "https://apple.com:8443"));
  assert.ok(sameSite("http://example.com", "https://www.example.com"), "upgrade is fine");
  assert.ok(!sameSite("https://example.com", "http://www.example.com"), "downgrade isn't");
  assert.ok(!sameSite("https://alice.github.io", "https://bob.github.io"));
  assert.ok(!sameSite("https://a.vercel.app", "https://b.vercel.app"));
  assert.ok(!sameSite("https://a.pages.dev", "https://b.pages.dev"));
  assert.ok(!sameSite("https://example.com", "https://evil.example.org"));
  assert.ok(!sameSite("https://example.co.uk", "https://other.co.uk"));
  assert.ok(!sameSite("http://127.0.0.1:8765", "http://127.0.0.1:9000"), "no site, no carry");
  assert.ok(!sameSite("https://apple.com", "chrome://newtab"));
  assert.equal(siteOf("https://developer.apple.com/account"), "apple.com");
});

test("sitePermissionPattern: *://*.site/*, or the exact host without a site", () => {
  assert.equal(sitePermissionPattern("https://appstoreconnect.apple.com"), "*://*.apple.com/*");
  assert.equal(sitePermissionPattern("https://alice.github.io"), "*://*.alice.github.io/*");
  assert.equal(sitePermissionPattern("http://127.0.0.1:8765"), "http://127.0.0.1/*");
});

test("carrySite: the deny list wins; another site, bot-opened, or same origin don't carry", () => {
  const share = { origin: "https://appstoreconnect.apple.com", level: "act" };
  assert.equal(carrySite(share, "https://developer.apple.com/account", DEFAULT_POLICY), "apple.com");
  assert.equal(carrySite(share, "https://appleid.apple.com/sign-in", DEFAULT_POLICY), null, "denied host");
  assert.equal(carrySite(share, "https://developer.apple.com/checkout", DEFAULT_POLICY), null, "denied path");
  assert.equal(carrySite(share, "https://example.com/", DEFAULT_POLICY), null);
  assert.equal(carrySite(share, "https://appstoreconnect.apple.com/apps/2", DEFAULT_POLICY), null, "same origin: kept, not carried");
  assert.equal(carrySite({ ...share, botOpened: true }, "https://developer.apple.com/", DEFAULT_POLICY), null);
  assert.equal(carrySite({ origin: "https://mail.google.com", level: "read" }, "https://accounts.google.com/", DEFAULT_POLICY), null);
});

test("pendingShare accepts a site pattern, never one on a public suffix", () => {
  const base = { tabId: 2, origin: "https://appstoreconnect.apple.com", level: "act" };
  assert.ok(makePendingShare({ ...base, origins: ["*://*.apple.com/*"] }).origins.includes("*://*.apple.com/*"));
  for (const bad of ["*://*.com/*", "*://*.github.io/*", "*://*.co.uk/*", "*://*/*"]) {
    assert.throws(() => makePendingShare({ ...base, origins: [bad] }), undefined, bad);
  }
});

test("popup: the site line", () => {
  assert.equal(siteNote("apple.com", true), "Shared across *.apple.com (follows this tab between apple.com hosts)");
  assert.equal(siteNote("apple.com", false), "Read / Act covers *.apple.com");
  assert.equal(siteNote(null, true), "");
});

// ---- in the service worker
const ASC = "https://appstoreconnect.apple.com";
const DEV = "https://developer.apple.com";
async function appleTab(h, level = "act", { pattern = "*://*.apple.com/*" } = {}) {
  h.tabs.set(5, { id: 5, windowId: 10, active: true, url: `${ASC}/apps`, title: "App Store Connect", status: "complete", index: 5 });
  h.granted.add(pattern);
  assert.equal((await h.message({ type: "share", tabId: 5, origin: ASC, level })).ok, true);
}
const go = async (h, tabId, url) => { h.tabs.get(tabId).url = url; await h.chrome.tabs.onUpdated.fire(tabId, { url }); };
const shareOf = async (h, tabId) => ((await h.chrome.storage.session.get("shares")).shares || {})[tabId] || null;
const audits = async (h) => (await h.chrome.storage.session.get("audit")).audit || [];

test("appstoreconnect -> developer -> back: still shared, same level, logged", async () => {
  const h = await loadBackground();
  await appleTab(h, "act");
  await go(h, 5, `${DEV}/account/resources/identifiers/list`);
  let s = await shareOf(h, 5);
  assert.equal(s.level, "act");
  assert.equal(s.origin, DEV);
  assert.equal(s.site, "apple.com");
  const ev = h.posted.filter((m) => m.event === "share_changed" && m.tab === 5);
  assert.deepEqual(ev.at(-1), { event: "share_changed", tab: 5, origin: DEV, level: "act", reason: "same site", from: ASC, site: "apple.com" });
  const a = (await audits(h)).at(-1);
  assert.equal(a.op, "share-carried");
  assert.equal(a.from, ASC);
  assert.equal(a.site, "apple.com");
  await go(h, 5, `${ASC}/apps/123/testflight`);
  s = await shareOf(h, 5);
  assert.equal(s.origin, ASC);
  assert.equal(s.level, "act");
  // the popup shows the site
  const st = await h.message({ type: "state", tabId: 5 });
  assert.equal(st.tab.site, "apple.com");
  assert.equal(st.shared.find((x) => x.tabId === 5).site, "apple.com");
});

test("Read is kept as Read", async () => {
  const h = await loadBackground();
  await appleTab(h, "read");
  await go(h, 5, `${DEV}/`);
  assert.equal((await shareOf(h, 5)).level, "read");
});

test("leaving the site still ends the share", async () => {
  const h = await loadBackground();
  await appleTab(h, "act");
  await go(h, 5, `${DEV}/account`);
  await go(h, 5, "https://example.com/");
  assert.equal(await shareOf(h, 5), null);
  assert.ok(h.posted.some((m) => m.event === "share_changed" && m.tab === 5 && m.level === null && m.reason === "origin changed"));
});

test("a denied host or path inside the site ends the share (the deny list wins)", async () => {
  let h = await loadBackground();
  await appleTab(h, "act");
  await go(h, 5, "https://appleid.apple.com/sign-in");
  assert.equal(await shareOf(h, 5), null);
  assert.ok(h.posted.some((m) => m.event === "share_changed" && m.tab === 5 && m.level === null && /^denied \(/.test(m.reason)));
  h = await loadBackground();
  await appleTab(h, "act");
  await go(h, 5, `${DEV}/checkout/pay`);
  assert.equal(await shareOf(h, 5), null);
});

test("https -> http on the same site ends the share", async () => {
  const h = await loadBackground();
  await appleTab(h, "act");
  await go(h, 5, "http://developer.apple.com/");
  assert.equal(await shareOf(h, 5), null);
});

test("a share made before 0.16.3 (only its own host granted) ends cleanly, saying how to fix it", async () => {
  const h = await loadBackground();
  await appleTab(h, "act", { pattern: "https://appstoreconnect.apple.com/*" });
  await go(h, 5, `${DEV}/account`);
  assert.equal(await shareOf(h, 5), null);
  const ev = h.posted.filter((m) => m.event === "share_changed" && m.tab === 5).at(-1);
  assert.equal(ev.level, null);
  assert.equal(ev.reason, "origin changed (no access to developer.apple.com; share it again to cover *.apple.com)");
});

test("multi-tenant hosts are separate sites: alice.github.io -> bob.github.io ends the share", async () => {
  const h = await loadBackground();
  h.tabs.set(6, { id: 6, windowId: 10, active: true, url: "https://alice.github.io/", title: "", status: "complete", index: 6 });
  h.granted.add("*://*.alice.github.io/*");
  assert.equal((await h.message({ type: "share", tabId: 6, origin: "https://alice.github.io", level: "act" })).ok, true);
  await go(h, 6, "https://docs.alice.github.io/");
  assert.equal((await shareOf(h, 6)).level, "act", "a host under the same tenant is the same site");
  await go(h, 6, "https://bob.github.io/");
  assert.equal(await shareOf(h, 6), null);
  h.tabs.set(7, { id: 7, windowId: 10, active: true, url: "https://a.vercel.app/", title: "", status: "complete", index: 7 });
  h.granted.add("*://*.a.vercel.app/*");
  await h.message({ type: "share", tabId: 7, origin: "https://a.vercel.app", level: "read" });
  await go(h, 7, "https://b.vercel.app/");
  assert.equal(await shareOf(h, 7), null);
});

test("agent navigate within the site keeps Kevin's Act share (not a bot tab); another site is as before", async () => {
  const h = await loadBackground();
  await appleTab(h, "act");
  const r = await h.call("tab.navigate", { tab: 5, url: `${DEV}/account`, expect: { origin: ASC } });
  assert.equal(r.ok, true, JSON.stringify(r));
  assert.equal(r.result.crossOrigin, true);
  assert.equal(r.result.sameSite, true);
  assert.equal(r.result.site, "apple.com");
  assert.equal(r.result.botOpened, false);
  assert.equal(r.result.share, "act");
  assert.equal(r.result.gated, false);
  assert.equal(h.windowsCreated.filter((w) => w.type === "popup").length, 0);
  assert.equal((await shareOf(h, 5)).origin, DEV);
  // onto a denied host of the same site: refused before anything happens
  const d = await h.call("tab.navigate", { tab: 5, url: "https://appleid.apple.com/" });
  assert.equal(d.error.code, "origin_blocked");
  assert.equal((await shareOf(h, 5)).level, "act");
  // to another site: gated, and the tab becomes bot-opened as before
  const id = h.start("tab.navigate", { tab: 5, url: "https://example.net/x" });
  const cid = await h.confirmWindow();
  await h.answer(cid, "allow");
  const x = await h.reply(id, 12000);
  assert.equal(x.result.botOpened, true);
  assert.equal(x.result.sameSite, undefined);
});
