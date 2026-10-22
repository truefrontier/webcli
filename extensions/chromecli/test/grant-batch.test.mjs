// 0.11.5: clause-aware pre-approvals. One quote that approves several steps is split into a preamble + item clauses;
// Jev maps each grant to one clause (one call), code checks the clause's uses / amount / window, and Jev scores each
// grant against the preamble plus only its clause. Mocked Jev with recorded answers (fixtures/); never the real API.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { loadBackground } from "./harness.mjs";
import { splitQuote, pageContext, checkClause, clauseUses, mapBody } from "../clauses.js";
import { validateGrant, scopeView } from "../grants.js";

const FX = JSON.parse(readFileSync(new URL("./fixtures/grant-batch-as-described.json", import.meta.url)));
const KEY = "tsk_test_grant_batch_0123456789";
const ADMIN = "https://admin.shopify.com";
const clone = (x) => structuredClone(x);
const targetOf = (view) => (String(view?.target || "").match(/named "([^"]+)"/) || [])[1];

// The mock answers by grant target from the fixture; h.jevCalls keeps every body. `over` patches answers.
function mockJev(h, { map = {}, scope = {}, single = null, fail = null } = {}) {
  h.jevCalls = [];
  globalThis.fetch = async (url, init) => {
    const body = JSON.parse(init.body);
    h.jevCalls.push(body);
    if (fail) { if (fail instanceof Error) throw fail; return { ok: false, status: fail, json: async () => ({}) }; }
    const answers = {};
    for (const [id, q] of Object.entries(body.questions)) {
      if (id.startsWith("map_")) {
        const t = targetOf(body.state.grants[id.slice(4)]);
        answers[id] = map[t] ?? FX.map[t] ?? { type: "choice", choice: "none", confidence: 0.9 };
      } else if (id.startsWith("scope_")) {
        const t = targetOf(body.state.approvals[id.slice(6)].grant);
        answers[id] = scope[t] ?? FX.scope[t];
      } else if (id === "scope") {
        answers[id] = single ?? FX.fieldReport.quoteA_Install;
      } else if (q.type === "noul") {
        answers[id] = { type: "noul", noul: 0 };
      }
    }
    return { ok: true, status: 200, json: async () => ({ answers }) };
  };
}

async function setup(jev = {}) {
  const h = await loadBackground();
  h.tabs.set(4, { id: 4, windowId: 10, active: false, url: FX.tab.url, title: FX.tab.title, status: "complete", index: 3 });
  await h.chrome.storage.local.set({ typesafeApiKey: KEY });
  assert.deepEqual(await h.share(4, "act"), { ok: true });
  mockJev(h, jev);
  return h;
}
const batch = (h, grants, extra = {}) => h.call("grant.add_batch", { quote: FX.quoteB, userApproved: true, grants, ...extra });
const listed = async (h) => (await h.call("grant.list")).result.grants;

test("splitQuote: quote (b) -> preamble with the 45m window + 7 clauses with their uses and the $0 cap", () => {
  const s = splitQuote(FX.quoteB);
  assert.equal(s.multi, true);
  assert.equal(s.preamble, "Yes, pre-approve chromecli clicks on as-described-review for the next 45 minutes");
  assert.equal(s.windowSec, 45 * 60);
  assert.deepEqual(s.clauses.map((c) => [c.text, c.uses]), [
    ["Install once", 1], ["Approve on the $0 Starter plan once", 1], ["Scan products once", 1],
    ["Use the title or Use the size once", 1], ["Use the description for all 3 once", 1], ["Apply to 3 products once", 1],
    ["Undo 3 times", 3]]);
  assert.equal(s.clauses[1].amount, 0);
  assert.ok(s.clauses.every((c) => c.windowSec === 2700));
  // no list after a colon: one clause, the whole quote (the pre-0.11.5 check)
  for (const q of [FX.quoteA, "yes, place the order if it's under $50", "open https://example.com/a: ok", "at 10:30 click Save once"]) {
    const x = splitQuote(q);
    assert.equal(x.multi, false, q);
    assert.equal(x.clauses.length, 1);
  }
  assert.equal(clauseUses("Undo x3"), 3);
  assert.equal(clauseUses("Undo twice"), 2);
  assert.equal(clauseUses("Apply"), null);
  // bullets and "A once and B 3 times"
  assert.deepEqual(splitQuote("ok for the store: - Save once - Publish once").clauses.map((c) => c.text), ["Save once", "Publish once"]);
  assert.deepEqual(splitQuote("ok: Save once and Undo 3 times").clauses.map((c) => c.text), ["Save once", "Undo 3 times"]);
});

test("pageContext: the store name from admin.shopify.com/store/NAME pins the path; the title is kept as evidence", () => {
  const c = pageContext(`${FX.tab.url}?x=secret#h`, FX.tab.title);
  assert.deepEqual(c, { url: FX.tab.url, path: "/store/as-described-review/apps/as-described", title: FX.tab.title,
    store: "as-described-review", pathPrefix: "/store/as-described-review" });
  assert.equal(pageContext("https://shop-1.myshopify.com/cart", "").store, "shop-1");
  assert.equal(pageContext("chrome://settings", "x"), null);
});

test("the grant summary Jev sees has the tab title, store, frame origin, target, $0 cap, uses and TTL; page text labelled untrusted", () => {
  const g = validateGrant({ ...FX.grants[6], quote: FX.quoteB, userApproved: true }, { now: 1 });
  g.context = pageContext(FX.tab.url, FX.tab.title);
  const v = scopeView(g);
  assert.equal(v.site, ADMIN);
  assert.equal(v.page.store, "as-described-review");
  assert.equal(v.page.tabTitle, FX.tab.title);
  assert.equal(v.page.onlyPagesUnder, `${ADMIN}/store/as-described-review`);
  assert.match(v.page.note, /untrusted/);
  assert.match(v.insideEmbeddedFrameFrom, /asdescribed\.truefrontier\.app/);
  assert.equal(v.target, 'the element named "Undo" (role button)');
  assert.equal(v.uses, 3);
  const a = validateGrant({ ...FX.grants[1], quote: FX.quoteB, userApproved: true }, { now: 1 });
  assert.match(scopeView(a).spendingCap, /^\$0\.00 \(free only/);
  const body = mapBody({ model: "jev-latest", split: splitQuote(FX.quoteB), grants: [a] });
  assert.deepEqual(Object.keys(body.questions.map_g1.criteria), ["c1", "c2", "c3", "c4", "c5", "c6", "c7", "none"]);
  assert.match(body.questions.map_g1.instructions, /untrusted evidence/);
});

test("quote (b) + its 7 grants: all authorized in 2 Jev calls, each scored against the preamble + its own clause only", async () => {
  const h = await setup();
  const r = await batch(h, clone(FX.grants), { tab: 4 });
  assert.equal(r.ok, true, JSON.stringify(r));
  assert.equal(r.result.added, 7);
  assert.equal(r.result.refused, 0);
  assert.equal(r.result.preamble, "Yes, pre-approve chromecli clicks on as-described-review for the next 45 minutes");
  assert.deepEqual(r.result.results.map((x) => [x.verdict, x.clause.id, x.stored]),
    ["c1", "c2", "c3", "c4", "c5", "c6", "c7"].map((c) => ["authorized", c, true]));
  assert.equal(h.jevCalls.length, 2, "one mapping call + one scoring call for the whole batch");
  const [mapCall, scoreCall] = h.jevCalls;
  assert.deepEqual(Object.keys(mapCall.state.items), ["c1", "c2", "c3", "c4", "c5", "c6", "c7"]);
  assert.equal(mapCall.state.grants.g1.page.store, "as-described-review");
  // the scorer sees one item per grant, never the other six
  assert.equal(scoreCall.state.approvals.g7.instruction,
    "Yes, pre-approve chromecli clicks on as-described-review for the next 45 minutes: Undo 3 times");
  assert.equal(scoreCall.state.approvals.g2.instruction.includes("Install"), false);
  assert.match(scoreCall.questions.scope_g1.instructions, /ONE item/);
  assert.equal(JSON.stringify(h.jevCalls).includes(KEY), false);
  // stored with the clause, the store path pin, the $0 cap and the 45m window
  const gs = await listed(h);
  assert.equal(gs.length, 7);
  const undo = gs.find((g) => g.target.text === "Undo");
  assert.equal(undo.usesLeft, 3);
  assert.equal(undo.clause.text, "Undo 3 times");
  assert.equal(undo.pathPrefix, "/store/as-described-review");
  assert.equal(gs.find((g) => g.target.text === "Approve").maxAmount, 0);
  for (const g of gs) assert.ok(g.expiresInSec > 44 * 60 && g.expiresInSec <= 45 * 60, `${g.target.text}: ${g.expiresInSec}`);
  // the log events carry the matched clause
  const added = h.posted.filter((m) => m.event === "grant_added");
  assert.equal(added.length, 7);
  assert.equal(added.find((m) => m.op === "click" && m.clause === "Undo 3 times").verdict, "authorized");
});

test("without --tab the one shared tab on the origin gives the context", async () => {
  const h = await setup();
  const r = await batch(h, clone(FX.grants).slice(0, 1));
  assert.equal(r.ok, true, JSON.stringify(r));
  assert.equal(h.jevCalls[0].state.grants.g1.page.store, "as-described-review");
});

test("Undo with 4 uses is overreach (the clause says 3 times): all or nothing stores nothing", async () => {
  const h = await setup();
  const gs = clone(FX.grants);
  gs[6].uses = 4;
  const r = await batch(h, gs, { tab: 4 });
  assert.equal(r.ok, false);
  assert.equal(r.error.code, "grant_overreach");
  assert.match(r.error.message, /1 of 7 grants refused; nothing was granted \(all or nothing/);
  const undo = r.error.results.find((x) => x.index === 7);
  assert.equal(undo.verdict, "overreach");
  assert.match(undo.reason, /"Undo 3 times" approves 3 uses; this grant asks for 4/);
  assert.deepEqual(await listed(h), []);
  assert.equal(h.jevCalls.length, 2, "the over-limit grant isn't sent for scoring");
  assert.equal(h.jevCalls[1].state.approvals.g7, undefined);
});

test("--partial stores the authorized ones and reports the rest", async () => {
  const h = await setup();
  const gs = clone(FX.grants);
  gs[6].uses = 4;
  const r = await batch(h, gs, { tab: 4, partial: true });
  assert.equal(r.ok, true, JSON.stringify(r));
  assert.equal(r.result.added, 6);
  assert.equal(r.result.refused, 1);
  assert.deepEqual(r.result.results.filter((x) => !x.stored).map((x) => [x.index, x.verdict]), [[7, "overreach"]]);
  assert.equal((await listed(h)).length, 6);
  assert.ok(h.posted.some((m) => m.event === "grant_refused" && m.clause === "Undo 3 times"));
});

test("a grant that matches no clause is refused as unclear; with --partial the rest are stored", async () => {
  const h = await setup();
  const extra = { op: "click", origin: ADMIN, frameOrigin: FX.frameOrigin, target: { text: "Delete app", role: "button" } };
  const r = await batch(h, [...clone(FX.grants), extra], { tab: 4 });
  assert.equal(r.error.code, "grant_unclear");
  assert.equal(r.error.results[7].verdict, "unclear");
  assert.match(r.error.results[7].reason, /no item in the quote approves this grant/);
  assert.deepEqual(await listed(h), []);
  const h2 = await setup();
  const r2 = await batch(h2, [...clone(FX.grants), extra], { tab: 4, partial: true });
  assert.equal(r2.result.added, 7);
  assert.equal(r2.result.refused, 1);
});

test("a clause's uses are a budget: two grants on one clause, or a second batch with the same quote, can't exceed it", async () => {
  const h = await setup();
  const undo = clone(FX.grants[6]);
  const r = await batch(h, [{ ...undo, uses: 2 }, { ...undo, uses: 2 }], { tab: 4, partial: true });
  assert.deepEqual(r.result.results.map((x) => x.verdict), ["authorized", "overreach"]);
  assert.match(r.result.results[1].reason, /2 already granted/);
  const again = await batch(h, [{ ...undo, uses: 2 }], { tab: 4 });
  assert.equal(again.error.code, "grant_overreach", "only 1 Undo use is left of the 3");
  assert.equal((await batch(h, [{ ...undo, uses: 1 }], { tab: 4 })).ok, true);
});

test("TTL: no --ttl -> capped at the preamble's 45m; an explicit longer --ttl is overreach; shorter is kept", async () => {
  const h = await setup();
  const g = clone(FX.grants[0]);
  let r = await batch(h, [{ ...g, ttlSec: 7200 }], { tab: 4 });
  assert.equal(r.error.code, "grant_overreach");
  assert.match(r.error.results[0].reason, /lasts 120m; the quote's window is 45m/);
  r = await batch(h, [{ ...g, ttlSec: 600 }], { tab: 4 });
  assert.equal(r.ok, true, JSON.stringify(r));
  assert.ok(r.result.results[0].grant.expiresInSec <= 600);
  const h2 = await setup();
  r = await batch(h2, [g], { tab: 4 });
  assert.equal(r.result.results[0].grant.expiresInSec, 2700);
});

test("Approve: no cap or a cap above the clause's $0 is overreach", async () => {
  for (const patch of [{ maxAmount: undefined, currency: undefined }, { maxAmount: 0.01 }]) {
    const h = await setup();
    const g = { ...clone(FX.grants[1]), ...patch };
    if (g.maxAmount === undefined) { delete g.maxAmount; delete g.currency; }
    const r = await batch(h, [g], { tab: 4 });
    assert.equal(r.error.code, "grant_overreach", JSON.stringify(r));
    assert.match(r.error.results[0].reason, /\$0\.00/);
  }
});

test("Jev's per-clause verdict still decides: overreach / below 0.8 refuse; low mapping confidence is unclear", async () => {
  let h = await setup({ scope: { Approve: { type: "choice", choice: "overreach", confidence: 0.85 } } });
  let r = await batch(h, clone(FX.grants), { tab: 4 });
  assert.equal(r.error.code, "grant_overreach");
  h = await setup({ scope: { Install: { type: "choice", choice: "authorized", confidence: 0.79 } } });
  r = await batch(h, clone(FX.grants), { tab: 4 });
  assert.equal(r.error.code, "grant_unclear");
  assert.match(r.error.results[0].reason, /only at 0\.79/);
  h = await setup({ map: { Install: { type: "choice", choice: "c1", confidence: 0.6 } } });
  r = await batch(h, clone(FX.grants), { tab: 4 });
  assert.equal(r.error.code, "grant_unclear");
  assert.equal(h.jevCalls.length, 2);
});

test("quote (a) 'i approve screencast clicks' is still refused (one clause, judged as a whole: 0.77 < 0.8)", async () => {
  const h = await setup();
  const r = await h.call("grant.add", { ...clone(FX.grants[0]), quote: FX.quoteA, userApproved: true, tab: 4 });
  assert.equal(r.ok, false);
  assert.equal(r.error.code, "grant_unclear");
  assert.match(r.error.message, /only at 0\.77/);
  assert.equal(h.jevCalls[0].state.instruction, FX.quoteA);
  assert.equal(h.jevCalls[0].state.grant.page.store, "as-described-review", "context is sent in the single check too");
  const b = await batch(h, clone(FX.grants), { quote: FX.quoteA, tab: 4 });
  assert.equal(b.error.code, "grant_unclear");
  assert.equal(b.error.results.length, 7);
  assert.ok(b.error.results.every((x) => x.verdict === "unclear"));
  assert.deepEqual(await listed(h), []);
});

test("grant.add with the multi-item quote uses the clause check too", async () => {
  const h = await setup();
  const r = await h.call("grant.add", { ...clone(FX.grants[6]), quote: FX.quoteB, userApproved: true, tab: 4 });
  assert.equal(r.ok, true, JSON.stringify(r));
  assert.equal(r.result.clause.text, "Undo 3 times");
  assert.equal(r.result.jevCheck.verdict, "authorized");
  assert.equal(r.result.grant.usesLeft, 3);
});

test("fail closed: Jev down, malformed, or no key -> nothing stored; batch validation and size limits", async () => {
  for (const fail of [new TypeError("fetch failed"), 503]) {
    const h = await setup({ fail });
    const r = await batch(h, clone(FX.grants), { tab: 4, partial: true });
    assert.equal(r.error.code, "jev_error", JSON.stringify(r));
    assert.deepEqual(await listed(h), []);
  }
  const h = await setup({ map: { Install: { type: "choice", choice: "c99", confidence: 0.99 } } });
  assert.equal((await batch(h, clone(FX.grants), { tab: 4 })).error.code, "jev_error");
  const h2 = await loadBackground();
  const r = await h2.call("grant.add_batch", { quote: FX.quoteB, userApproved: true, grants: clone(FX.grants) });
  assert.equal(r.error.code, "jev_not_configured");
  assert.equal((await batch(h, [])).error.code, "bad_request");
  assert.equal((await batch(h, Array(21).fill(FX.grants[0]))).error.code, "bad_request");
  assert.equal((await batch(h, [{ ...FX.grants[0], op: "eval" }])).error.code, "bad_request");
  assert.equal((await h.call("grant.add_batch", { quote: FX.quoteB, grants: clone(FX.grants) })).error.code, "missing_user_approval");
  assert.equal((await h.call("grant.add_batch", { quote: FX.quoteB, userApproved: true, grants: clone(FX.grants) }, { confirm: false })).error.code, "bad_request");
  assert.equal((await batch(h, clone(FX.grants), { tab: 2 })).error.code, "not_shared");
});

test("a store-pinned grant covers only that store's pages", async () => {
  const { grantMatches } = await import("../grants.js");
  const g = validateGrant({ ...FX.grants[0], quote: FX.quoteB, userApproved: true }, { now: 1 });
  g.pathPrefix = "/store/as-described-review";
  const el = { name: "Install", role: "button" };
  const ctx = (url) => ({ op: "click", origin: ADMIN, now: 2, element: el, url });
  assert.equal(grantMatches(g, ctx(`${ADMIN}/store/as-described-review/oauth/install`)).ok, true);
  assert.equal(grantMatches(g, ctx(`${ADMIN}/store/as-described-review`)).ok, true);
  assert.equal(grantMatches(g, ctx(`${ADMIN}/store/other-store/oauth/install`)).why, "path");
  assert.equal(grantMatches(g, ctx(`${ADMIN}/store/as-described-review-2/x`)).why, "path");
  assert.equal(grantMatches(g, ctx(undefined)).why, "path");
});

test("checkClause: target words must appear in the matched item", () => {
  const s = splitQuote(FX.quoteB);
  const g = validateGrant({ ...FX.grants[0], target: { text: "Delete store" }, quote: FX.quoteB, userApproved: true }, { now: 1 });
  const r = checkClause(g, s.clauses[0]);
  assert.equal(r.verdict, "unclear");
});

test("end to end: the stored Install grant answers the gated click on the store's tab, once", async () => {
  const install = { tag: "button", role: "button", name: "Install", type: "submit", inForm: true, defaultSubmit: true };
  const h = await setup();
  h.page = (tabId, spec) => {
    if (spec.mode === "inspect") return { ok: true, element: install, fingerprint: "fp:i", selectorHits: {}, amountTexts: {} };
    if (spec.mode === "do") return { ok: true, element: install, dispatched: true, readBack: true };
    return { ok: true, element: install, fingerprint: "fp:i" };
  };
  const r = await batch(h, clone(FX.grants), { tab: 4 });
  assert.equal(r.ok, true, JSON.stringify(r));
  const id = r.result.results[0].grant.id;
  const c = await h.call("page.click", { tab: 4, target: { selector: "#install" }, expect: { origin: ADMIN } });
  assert.equal(c.ok, true, JSON.stringify(c));
  assert.equal(c.result.confirmedBy, `grant:${id}`);
  assert.equal(h.windowsCreated.filter((w) => w.type === "popup").length, 0);
  assert.equal((await listed(h)).some((g) => g.id === id), false, "used up");
});
