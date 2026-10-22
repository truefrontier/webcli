// Pre-approval grants in the service worker: --user-approved + Jev scope check (0.10.1), add/list/revoke, matching
// right before dispatch.
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadBackground } from "./harness.mjs";

const FORM = "http://127.0.0.1:8765";
const EXPECT = { origin: FORM };
const QUOTE = "yes, place the order if it's under $50";
const els = {
  place: { tag: "button", role: "button", name: "Place order", type: "submit", inForm: true, defaultSubmit: true },
  del: { tag: "button", role: "button", name: "Delete store", type: "button", inForm: true, defaultSubmit: false },
  password: { tag: "input", role: "textbox", name: "Password", type: "password", id: "password", nameAttr: "password", autocomplete: "current-password", inForm: true, isField: true },
  card: { tag: "input", role: "textbox", name: "Card number", type: "text", id: "cc", nameAttr: "cardnumber", autocomplete: "cc-number", inForm: true, isField: true },
  storeName: { tag: "input", role: "textbox", name: "Type the store name", type: "text", id: "sn", nameAttr: "confirm", autocomplete: "", inForm: true, isField: true },
};
const bySel = { "#place": els.place, "#delete": els.del, "#password": els.password, "#cc": els.card, "#sn": els.storeName };

function fakePage(opts = {}) {
  return (tabId, spec, name) => {
    if (name === "pageProbe") return { ok: true, found: true };
    const el = bySel[spec.target?.selector];
    if (!el) return { ok: false, code: "element_not_found", message: "No visible element matches the target." };
    if (spec.mode === "inspect") {
      const selectorHits = Object.fromEntries((spec.selectors || []).map((s) => [s, bySel[s] === el]));
      const amountTexts = {};
      for (const s of spec.amountSelectors || []) amountTexts[s] = opts.amountBySel?.[s] || [];
      if (spec.autoAmount) amountTexts._auto = opts.autoTexts || [];
      return { ok: true, element: el, fingerprint: opts.inspectFp || `fp:${el.name}`, selectorHits, amountTexts };
    }
    if (spec.mode !== "do") return { ok: true, element: el, fingerprint: `fp:${el.name}` };
    return { ok: true, element: el, dispatched: true, readBack: true };
  };
}

const KEY = "tsk_test_grants_worker_0123456789";
const AUTH = { type: "choice", choice: "authorized", confidence: 0.93, probabilities: { authorized: 0.93, overreach: 0.05, unclear: 0.02 } };
// Mock Jev: the grant scope question gets `scope`, narrowing questions get `narrow` (default: no narrowing).
// Any other Jev call (click classification) fails, so page actions fall back to the heuristics in act tabs.
function mockJev(h, scope = AUTH, narrow = 0) {
  h.scopeCalls = [];
  globalThis.fetch = async (url, init) => {
    const body = JSON.parse(init.body);
    if (!body.questions?.scope) return { ok: false, status: 503, json: async () => ({}) };
    h.scopeCalls.push(body);
    if (scope instanceof Error) throw scope;
    if (typeof scope === "number") return { ok: false, status: scope, json: async () => ({}) };
    const answers = {};
    for (const [id, q] of Object.entries(body.questions)) answers[id] = q.type === "choice" ? scope : { type: "noul", noul: narrow };
    return { ok: true, status: 200, json: async () => ({ answers }) };
  };
}

async function setup(opts = {}, { key = KEY, scope = AUTH, narrow = 0 } = {}) {
  const h = await loadBackground({ page: fakePage(opts) });
  if (key) await h.chrome.storage.local.set({ typesafeApiKey: key });
  assert.deepEqual(await h.share(1, "act"), { ok: true });
  mockJev(h, scope, narrow);
  return h;
}
const add = (h, g) => h.call("grant.add", { quote: QUOTE, origin: FORM, userApproved: true, ...g });
const dos = (h) => h.injected.filter((i) => i.spec?.mode === "do").length;
const popups = (h) => h.windowsCreated.filter((w) => w.type === "popup").length;

test("0.10.1: no toggle; grant.add needs userApproved: true (else missing_user_approval) and --confirm", async () => {
  const h = await setup();
  assert.equal((await h.message({ type: "state", tabId: 1 })).grantsEnabled, undefined);
  for (const userApproved of [undefined, false, "true", 1]) {
    const r = await add(h, { op: "click", target: { text: "Place order" }, userApproved });
    assert.equal(r.error.code, "missing_user_approval");
  }
  assert.equal(h.scopeCalls.length, 0, "Jev isn't asked without --user-approved");
  assert.equal((await h.call("grant.add", { op: "click" }, { confirm: false })).error.code, "bad_request", "grant.add needs --confirm");
  // the old popup message does nothing now
  assert.equal((await h.message({ type: "setGrantsEnabled", enabled: true })).ok, false);
  const ok = await add(h, { op: "click", target: { text: "Place order" } });
  assert.equal(ok.ok, true, JSON.stringify(ok));
  assert.equal(ok.result.jevCheck.verdict, "authorized");
  assert.equal(ok.result.grant.jev.verdict, "authorized");
  assert.equal(h.scopeCalls.length, 1);
});

test("Jev scope check: the quote is the instruction; the grant (not the page) is what's judged", async () => {
  const h = await setup();
  await add(h, { op: "click", target: { text: "Place order" } });
  const body = h.scopeCalls[0];
  assert.equal(body.state.instruction, QUOTE);
  assert.equal(body.state.grant.target, 'the element named "Place order"');
  assert.equal(body.state.grant.site, FORM);
  assert.deepEqual(Object.keys(body.questions.scope.criteria), ["authorized", "overreach", "unclear"]);
  assert.match(body.questions.scope.instructions, /untrusted evidence/);
  assert.equal(JSON.stringify(body).includes(KEY), false);
  // a fill value is never shown to Jev
  await add(h, { op: "fill", target: { selector: "#sn" }, value: "secret-store-name", quote: "type secret-store-name in the store name box" });
  assert.equal(JSON.stringify(h.scopeCalls[1].state.grant).includes("secret-store-name"), false);
});

test("Jev refuses overreach / unclear (exit-9 codes), low confidence counts as unclear; nothing stored", async () => {
  for (const [scope, code] of [
    [{ type: "choice", choice: "overreach", confidence: 0.9 }, "grant_overreach"],
    [{ type: "choice", choice: "unclear", confidence: 0.7 }, "grant_unclear"],
    [{ type: "choice", choice: "authorized", confidence: 0.79 }, "grant_unclear"],
  ]) {
    const h = await setup({}, { scope });
    const r = await add(h, { op: "click", target: { text: "Place order" }, maxAmount: 100 });
    assert.equal(r.ok, false);
    assert.equal(r.error.code, code);
    assert.match(r.error.message, /^Jev/);
    assert.equal(r.error.jevCheck.verdict, code.slice(6));
    assert.deepEqual((await h.call("grant.list")).result.grants, []);
    assert.ok(h.posted.some((m) => m.event === "grant_refused" && m.verdict === code.slice(6) && m.quote === QUOTE));
  }
});

test("Jev narrows the grant to the quote's cap / uses / time when it agrees (>= 0.8)", async () => {
  const h = await setup({}, { narrow: 0.95 });
  const r = await add(h, { op: "click", target: { text: "Place order" }, maxAmount: 100, uses: 5, ttlSec: 3600,
    quote: "place the order once if it's under $50, within 10 minutes" });
  assert.equal(r.ok, true, JSON.stringify(r));
  assert.equal(r.result.grant.maxAmount, 50);
  assert.equal(r.result.grant.uses, 1);
  assert.equal(r.result.grant.usesLeft, 1);
  assert.ok(r.result.grant.expiresInSec <= 600);
  assert.deepEqual(r.result.jevCheck.narrowed.map((n) => n.field).sort(), ["maxAmount", "ttlSec", "uses"]);
  const h2 = await setup({}, { narrow: 0.5 });
  const r2 = await add(h2, { op: "click", target: { text: "Place order" }, maxAmount: 100, quote: "place it if it's under $50" });
  assert.equal(r2.result.grant.maxAmount, 100, "Jev unsure about narrowing: unchanged");
});

test("fail closed: no Jev key (jev_not_configured), Jev down or erroring (jev_error); nothing granted", async () => {
  const h = await setup({}, { key: null });
  assert.equal((await add(h, { op: "click", target: { text: "Place order" } })).error.code, "jev_not_configured");
  for (const scope of [new TypeError("fetch failed"), 503, { type: "choice", choice: "maybe" }]) {
    const h2 = await setup({}, { scope });
    const r = await add(h2, { op: "click", target: { text: "Place order" } });
    assert.equal(r.error.code, "jev_error", JSON.stringify(r));
    assert.deepEqual((await h2.call("grant.list")).result.grants, []);
  }
});

test("eval can never be pre-approved", async () => {
  const h = await setup();
  const r = await add(h, { op: "eval", target: { selector: "body" } });
  assert.equal(r.error.code, "bad_request");
  assert.match(r.error.message, /eval/);
});

test("a matching grant skips the confirm once, reports confirmedBy, notifies, then is used up", async () => {
  const h = await setup();
  const a = await add(h, { op: "click", target: { text: "place order" } });
  assert.equal(a.ok, true, JSON.stringify(a));
  const gid = a.result.grant.id;
  assert.equal(a.result.grant.usesLeft, 1);
  const r = await h.call("page.click", { tab: 1, target: { selector: "#place" }, expect: EXPECT });
  assert.equal(r.ok, true, JSON.stringify(r));
  assert.equal(r.result.confirmedBy, `grant:${gid}`);
  assert.equal(r.result.grant.quote, QUOTE);
  assert.equal(r.result.grant.usesLeft, 0);
  assert.equal(r.result.confirmedInBrowser, false);
  assert.equal(r.result.gated, true, "it was still a gated action");
  assert.equal(popups(h), 0);
  assert.equal(dos(h), 1);
  assert.equal(h.notes.length, 1);
  assert.match(h.notes[0].message, /Agent used your pre-approval: click Place order on 127\.0\.0\.1:8765/);
  // used up: the next one asks Kevin
  assert.deepEqual((await h.call("grant.list")).result.grants, []);
  const id = h.start("page.click", { tab: 1, target: { selector: "#place" }, expect: EXPECT });
  const cid = await h.confirmWindow();
  await h.answer(cid, "deny");
  assert.equal((await h.reply(id)).error.code, "declined");
});

test("no match -> normal confirm: other origin, other op, other name, wrong selector, expired", async () => {
  const h = await setup();
  await add(h, { op: "click", target: { text: "Cancel order" } });
  await add(h, { op: "press", target: { text: "Place order" } });
  await add(h, { op: "click", origin: "https://example.com", target: { text: "Place order" } });
  await add(h, { op: "click", target: { selector: "#delete" } });
  const id = h.start("page.click", { tab: 1, target: { selector: "#place" }, expect: EXPECT });
  const cid = await h.confirmWindow();
  await h.answer(cid, "allow");
  const r = await h.reply(id, 5000);
  assert.equal(r.result.confirmedInBrowser, true);
  assert.equal(r.result.confirmedBy, undefined);
  assert.equal((await h.call("grant.list")).result.grants.length, 4, "nothing consumed");
});

test("selector grants match the same element; uses > 1 count down", async () => {
  const h = await setup();
  await add(h, { op: "click", target: { selector: "#delete" }, uses: 2 });
  for (const left of [1, 0]) {
    const r = await h.call("page.click", { tab: 1, target: { selector: "#delete" }, expect: EXPECT });
    assert.equal(r.result.grant.usesLeft, left, JSON.stringify(r));
  }
  assert.equal(popups(h), 0);
});

test("--max-amount: under the cap skips; over the cap or no amount shows the confirm with the grant + amount", async () => {
  let h = await setup({ autoTexts: ["Subtotal $40.00 Shipping $4.99", "Order total $44.99"] });
  await add(h, { op: "click", target: { text: "Place order" }, maxAmount: 49.99 });
  let r = await h.call("page.click", { tab: 1, target: { selector: "#place" }, expect: EXPECT });
  assert.equal(r.result.grant.amount, 44.99, JSON.stringify(r));
  assert.match(h.notes[0].message, /\(\$44\.99\)/);

  h = await setup({ autoTexts: ["Order total $59.00"] });
  await add(h, { op: "click", target: { text: "Place order" }, maxAmount: 49.99 });
  let id = h.start("page.click", { tab: 1, target: { selector: "#place" }, expect: EXPECT });
  let cid = await h.confirmWindow();
  const got = await h.message({ type: "confirmGet", id: cid }, `confirm.html?id=${cid}`, { id: 999 });
  assert.equal(got.info.grant.amount, 59);
  assert.equal(got.info.grant.maxAmount, 49.99);
  assert.equal(got.info.grant.quote, QUOTE);
  await h.answer(cid, "deny");
  assert.equal((await h.reply(id)).error.code, "declined");
  assert.equal((await h.call("grant.list")).result.grants.length, 1, "not consumed");

  h = await setup({ amountBySel: { "#total": ["$12.00"] }, autoTexts: ["Total $99.00"] });
  await add(h, { op: "click", target: { text: "Place order" }, maxAmount: 20, amountSelector: "#total" });
  r = await h.call("page.click", { tab: 1, target: { selector: "#place" }, expect: EXPECT });
  assert.equal(r.result.grant.amount, 12, "--amount-selector wins over the best-effort read");

  h = await setup({ autoTexts: ["no prices here"] });
  await add(h, { op: "click", target: { text: "Place order" }, maxAmount: 20 });
  id = h.start("page.click", { tab: 1, target: { selector: "#place" }, expect: EXPECT });
  cid = await h.confirmWindow();
  const g2 = await h.message({ type: "confirmGet", id: cid }, `confirm.html?id=${cid}`, { id: 999 });
  assert.equal(g2.info.grant.amount, null);
  await h.answer(cid, "deny");
  await h.reply(id);
});

test("never skippable: password and card fields still ask Kevin even with a matching grant", async () => {
  const h = await setup();
  await add(h, { op: "fill", target: { selector: "#password" } });
  await add(h, { op: "fill", target: { selector: "#cc" } });
  for (const sel of ["#password", "#cc"]) {
    const id = h.start("page.fill", { tab: 1, target: { selector: sel }, value: "secret-value-1", expect: EXPECT });
    const cid = await h.confirmWindow(sel === "#password" ? 1 : 2);
    await h.answer(cid, "deny");
    assert.equal((await h.reply(id)).error.code, "declined", sel);
  }
  assert.equal(h.injected.filter((i) => i.spec?.mode === "inspect").length, 0, "no grant lookup at all");
  assert.equal((await h.call("grant.list")).result.grants.length, 2);
});

test("fill grant with --value: only that exact value; the value never goes back to the host", async () => {
  const h = await setup();
  const a = await add(h, { op: "fill", target: { text: "Type the store name" }, value: "as-described-review" });
  assert.equal(a.result.grant.value, "<19 chars>");
  // a plain text field isn't gated, so the grant isn't needed (and isn't consumed)
  const r = await h.call("page.fill", { tab: 1, target: { selector: "#sn" }, value: "as-described-review", expect: EXPECT });
  assert.equal(r.result.gated, false);
  assert.equal((await h.call("grant.list")).result.grants.length, 1);
  assert.ok(!JSON.stringify(h.posted).includes("as-described-review"));
});

test("the element changing between describe and inspect voids the grant (Kevin confirms)", async () => {
  const h = await setup({ inspectFp: "fp:other" });
  await add(h, { op: "click", target: { selector: "#place" } });
  const id = h.start("page.click", { tab: 1, target: { selector: "#place" }, expect: EXPECT });
  const cid = await h.confirmWindow();
  await h.answer(cid, "deny");
  await h.reply(id);
  assert.equal((await h.call("grant.list")).result.grants.length, 1);
});

test("grants for denied origins are refused; tab-op grants cover open/navigate", async () => {
  const h = await setup();
  assert.equal((await add(h, { op: "click", origin: "https://bank.chase.com", target: { text: "Pay" } })).error.code, "origin_blocked");
  assert.equal((await add(h, { op: "open", origin: "https://example.org", quote: "open example.org" })).ok, true);
  const r = await h.call("tab.open", { url: "https://example.org/x" });
  assert.equal(r.ok, true, JSON.stringify(r));
  assert.match(r.result.confirmedBy, /^grant:/);
  assert.equal(popups(h), 0);
  assert.equal((await h.call("tab.open", { url: "https://bank.chase.com/" })).error.code, "origin_blocked");
});

test("Pause revokes every grant; revoke by id / all", async () => {
  let h = await setup();
  await add(h, { op: "click", target: { text: "Place order" } });
  await add(h, { op: "click", target: { text: "Delete store" } });
  await h.message({ type: "pause", paused: true });
  await h.message({ type: "pause", paused: false });
  assert.equal((await h.call("grant.list")).result.grants.length, 0);
  assert.ok(h.posted.some((m) => m.event === "grants_cleared" && m.reason === "paused"));

  h = await setup();
  const a = await add(h, { op: "click", target: { text: "Place order" } });
  await add(h, { op: "click", target: { text: "Delete store" } });
  assert.equal((await h.call("grant.revoke", { id: a.result.grant.id })).result.revoked, 1);
  assert.equal((await h.call("grant.revoke", { id: "nope" })).error.code, "bad_target");
  const st = await h.message({ type: "state", tabId: 1 });
  assert.equal(st.grants.length, 1);
  assert.equal(st.grants[0].quote, QUOTE);
  await h.message({ type: "revokeGrant", id: st.grants[0].id });
  assert.equal((await h.message({ type: "state", tabId: 1 })).grants.length, 0);
  await add(h, { op: "click", target: { text: "Place order" } });
  assert.equal((await h.call("grant.revoke", { all: true })).result.revoked, 1);
});

test("grants live in storage.session (gone on browser restart); nothing in storage.local", async () => {
  const h = await setup();
  await add(h, { op: "click", target: { text: "Place order" } });
  assert.equal(Object.keys(h.chrome.storage.session._data().grants).length, 1);
  assert.equal(h.chrome.storage.local._data().grants, undefined);
  assert.equal(h.chrome.storage.local._data().grantsEnabled, undefined);
});
