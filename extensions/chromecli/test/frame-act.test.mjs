// chromecli 0.11.3: acting inside allowlisted iframes (frame_act_allow) and narrow deny exceptions (deny_except),
// end to end through the service worker: pending until Kevin's Accept, the same gates as the top frame, grants.
import { test } from "node:test";
import assert from "node:assert/strict";
import { DEFAULT_DENY } from "../policy.js";
import { loadBackground } from "./harness.mjs";

const TOP = "http://127.0.0.1:8765";
const APP = "https://asdescribed.truefrontier.app";
const BANK = "https://bank.chase.com";
const FRAMES = [
  { frameId: 0, parentFrameId: -1, url: `${TOP}/form.html` },
  { frameId: 5, parentFrameId: 0, url: `${APP}/embedded?shop=x` },
  { frameId: 6, parentFrameId: 0, url: "https://other.example.net/widget" },
  { frameId: 7, parentFrameId: 0, url: `${BANK}/pay` },
];
const EL = {
  scan: { tag: "button", role: "button", name: "Scan", type: "button", inForm: false, defaultSubmit: false },
  install: { tag: "button", role: "button", name: "Install app", type: "button", inForm: false, defaultSubmit: false },
};
const byRef = { e1: EL.scan, e2: EL.install };

function page(tabId, spec, name, frameId) {
  if (name === "pageProbe") return { ok: true, found: true };
  if (name === "extractPage") {
    return { ok: true, url: frameId ? `${APP}/embedded` : `${TOP}/form.html`, title: "x", content: frameId ? "As Described" : "Top",
      chars: 3, truncated: false, ...(spec.elements ? { elements: frameId ? [{ ref: "e1", tag: "button", name: "Scan" }] : [] } : {}),
      diag: { viewport: { w: 1200, h: 900 }, iframes: [], regions: [] } };
  }
  const el = frameId === 0 ? null : byRef[spec.target?.ref];
  if (!el) return { ok: false, code: "element_not_found", message: "No element matches." };
  if (spec.mode === "inspect") return { ok: true, element: el, fingerprint: `fp:${el.name}`, selectorHits: {}, amountTexts: {} };
  if (spec.mode !== "do") return { ok: true, element: el, fingerprint: `fp:${el.name}` };
  return { ok: true, element: el, dispatched: true, readBack: true };
}

// Push a policy WITHOUT accepting it (what the host does); returns the stored state.
async function push(h, p) {
  const pol = { implicit_active: "off", history: true, confirm_timeout: 60, read_allow: [], act_allow: [],
    deny: [...DEFAULT_DENY], hash: `x${Math.random()}`, ...p };
  await h.port.onMessage.fire({ event: "policy", policy: pol });
  await new Promise((r) => setTimeout(r, 10));
  return (await h.chrome.storage.local.get("policyState")).policyState;
}

async function setup({ list = [APP, BANK], accept = true, perm = true } = {}) {
  const h = await loadBackground({ page });
  globalThis.fetch = async () => ({ ok: false, status: 503, json: async () => ({}) }); // Jev down -> heuristics
  if (accept) await h.policy({ frame_act_allow: list });
  else await push(h, { frame_act_allow: list });
  h.frames.set(1, FRAMES);
  if (perm) { h.granted.add(`${APP}/*`); h.granted.add(`${BANK}/*`); h.granted.add("https://other.example.net/*"); }
  assert.deepEqual(await h.share(1, "act"), { ok: true });
  return h;
}
const dos = (h) => h.injected.filter((i) => i.spec?.mode === "do");
const popups = (h) => h.windowsCreated.filter((w) => w.type === "popup").length;

test("frame_act_allow: a listed frame on an act tab is acted in (frame 5, its origin reported)", async () => {
  const h = await setup();
  const r = await h.call("page.click", { tab: 1, target: { ref: "f5:e1" }, expect: { origin: TOP } });
  assert.equal(r.ok, true, JSON.stringify(r));
  assert.equal(r.result.frameId, 5);
  assert.equal(r.result.frameOrigin, APP);
  assert.equal(popups(h), 0, "Scan is not a risky click");
  assert.deepEqual(dos(h).map((i) => i.frameId), [5]);
  const stat = await h.message({ type: "state", tabId: 1 });
  assert.deepEqual(stat.frameActAllow, [APP, BANK]);
});

test("frame_act_allow: a risky click in the frame still gates (confirm window shows the frame); deny = nothing done", async () => {
  const h = await setup();
  const id = h.start("page.click", { tab: 1, target: { ref: "f5:e2" }, expect: { origin: TOP } });
  const cid = await h.confirmWindow();
  const info = await h.message({ type: "confirmGet", id: cid }, `confirm.html?id=${cid}`, { id: 999 });
  assert.equal(JSON.stringify(info).includes(APP), true, JSON.stringify(info));
  await h.answer(cid, "deny");
  assert.equal((await h.reply(id)).error.code, "declined");
  assert.equal(dos(h).length, 0);
  // --confirm path (approve) acts in the frame
  const id2 = h.start("page.click", { tab: 1, target: { ref: "f5:e2" }, expect: { origin: TOP } });
  await h.answer(await h.confirmWindow(2), "allow");
  const r2 = await h.reply(id2);
  assert.equal(r2.ok, true, JSON.stringify(r2));
  assert.deepEqual(dos(h).map((i) => i.frameId), [5]);
});

test("frame_act_allow: unlisted frames, denied listed frames, read/bot tabs and missing permission are refused", async () => {
  const h = await setup();
  const other = await h.call("page.click", { tab: 1, target: { ref: "f6:e1" }, expect: { origin: TOP } });
  assert.deepEqual([other.error.code, other.error.reason], ["bad_target", "frame_ref"]);
  const bank = await h.call("page.click", { tab: 1, target: { ref: "f7:e1" }, expect: { origin: TOP } });
  assert.deepEqual([bank.error.code, bank.error.reason, bank.error.frameReason], ["bad_target", "frame_ref", "denied"]);
  assert.equal(dos(h).length, 0);
  await h.share(1, "read");
  const read = await h.call("page.click", { tab: 1, target: { ref: "f5:e1" }, expect: { origin: TOP } });
  assert.notEqual(read.ok, true);
  const h2 = await setup({ perm: false });
  const np = await h2.call("page.click", { tab: 1, target: { ref: "f5:e1" }, expect: { origin: TOP } });
  assert.equal(np.error.code, "permission_needed", JSON.stringify(np));
  assert.equal(dos(h2).length, 0);
});

test("frame_act_allow: adding is pending until Kevin's Accept (policy_pending); removing applies at once", async () => {
  const h = await setup({ accept: false });
  const st = (await h.chrome.storage.local.get("policyState")).policyState;
  assert.deepEqual(st.active.frame_act_allow, []);
  assert.ok(st.pending.loosening.some((l) => l.kind === "frame_act_allow_added" && l.value === APP));
  const r = await h.call("page.click", { tab: 1, target: { ref: "f5:e1" }, expect: { origin: TOP } });
  assert.equal(r.error.code, "policy_pending", JSON.stringify(r));
  assert.equal(dos(h).length, 0);
  await h.message({ type: "acceptPolicy" });
  assert.equal((await h.call("page.click", { tab: 1, target: { ref: "f5:e1" }, expect: { origin: TOP } })).ok, true);
  const after = await push(h, { frame_act_allow: [] });
  assert.equal(after.pending, null);
  const gone = await h.call("page.click", { tab: 1, target: { ref: "f5:e1" }, expect: { origin: TOP } });
  assert.deepEqual([gone.error.code, gone.error.reason], ["bad_target", "frame_ref"]);
});

test("grants: --frame-origin matches only that frame's target; a top-frame grant never covers a frame", async () => {
  const h = await setup();
  await h.chrome.storage.local.set({ typesafeApiKey: "tsk_test_frame_act_0123456789" });
  const AUTH = { type: "choice", choice: "authorized", confidence: 0.93, probabilities: { authorized: 0.93, overreach: 0.05, unclear: 0.02 } };
  h.scope = [];
  globalThis.fetch = async (url, init) => {
    const body = JSON.parse(init.body);
    if (!body.questions?.scope) return { ok: false, status: 503, json: async () => ({}) };
    h.scope.push(body);
    const answers = {};
    for (const [id, q] of Object.entries(body.questions)) answers[id] = q.type === "choice" ? AUTH : { type: "noul", noul: 0 };
    return { ok: true, status: 200, json: async () => ({ answers }) };
  };
  const quote = "approved to install the app in As Described";
  const top = await h.call("grant.add", { op: "click", origin: TOP, target: { text: "install app" }, quote, userApproved: true });
  assert.equal(top.ok, true, JSON.stringify(top));
  const id = h.start("page.click", { tab: 1, target: { ref: "f5:e2" }, expect: { origin: TOP } });
  await h.answer(await h.confirmWindow(), "deny");
  assert.equal((await h.reply(id)).error.code, "declined", "a top-frame grant doesn't cover the frame");
  const bad = await h.call("grant.add", { op: "navigate", origin: TOP, frameOrigin: APP, quote, userApproved: true });
  assert.notEqual(bad.ok, true);
  const denied = await h.call("grant.add", { op: "click", origin: TOP, frameOrigin: BANK, target: { text: "install app" }, quote, userApproved: true });
  assert.notEqual(denied.ok, true, "no grant for a denied frame origin");
  const g = await h.call("grant.add", { op: "click", origin: TOP, frameOrigin: APP, target: { text: "install app" }, quote, userApproved: true, uses: 2 });
  assert.equal(g.ok, true, JSON.stringify(g));
  assert.equal(g.result.grant.frameOrigin, APP);
  assert.ok(JSON.stringify(h.scope.at(-1)).includes("asdescribed.truefrontier.app"), "Jev's scope check sees the frame");
  const r = await h.call("page.click", { tab: 1, target: { ref: "f5:e2" }, expect: { origin: TOP } });
  assert.equal(r.ok, true, JSON.stringify(r));
  assert.equal(r.result.confirmedBy, `grant:${g.result.grant.id}`);
  assert.equal(r.result.frameOrigin, APP);
  assert.equal(popups(h), 1, "only the earlier declined one");
});

// ---- deny_except ----
const INSTALL = "https://admin.shopify.com/store/kk-dev/oauth/install?client_id=abc";
const EXC = "https://admin.shopify.com/store/*/oauth/install";

async function shopify({ accept }) {
  const h = await loadBackground({ page: (t, spec, name) => (name === "pageProbe" ? { ok: true, found: true }
    : spec.mode === "do" ? { ok: true, element: EL.install, dispatched: true, readBack: true }
      : { ok: true, element: EL.install, fingerprint: "fp:install", selectorHits: {}, amountTexts: {} }) });
  globalThis.fetch = async () => ({ ok: false, status: 503, json: async () => ({}) });
  h.tabs.set(4, { id: 4, windowId: 10, active: false, url: INSTALL, title: "Install", status: "complete", index: 3 });
  const p = { deny: [...DEFAULT_DENY, "path:/oauth"], deny_except: [EXC] };
  if (accept) { await push(h, { deny: p.deny }); await h.policy(p); } else { await h.policy({ deny: p.deny }); await push(h, p); }
  return h;
}

test("deny_except: pending until Accept (the install page stays denied), then shared Act; Install still gates; audited", async () => {
  const h = await shopify({ accept: false });
  const st = (await h.chrome.storage.local.get("policyState")).policyState;
  assert.ok(st.pending.loosening.some((l) => l.kind === "deny_except_added" && l.value === EXC));
  const before = await h.share(4, "act");
  assert.notEqual(before.ok, true, "still denied while pending");
  await h.message({ type: "acceptPolicy" });
  assert.deepEqual(await h.share(4, "act"), { ok: true });
  const id = h.start("page.click", { tab: 4, target: { selector: "#install" }, expect: { origin: "https://admin.shopify.com" } });
  const cid = await h.confirmWindow();
  await h.answer(cid, "allow");
  const r = await h.reply(id);
  assert.equal(r.ok, true, JSON.stringify(r));
  assert.equal(r.result.gated, true);
  assert.equal(r.result.denyExcept, EXC, "the result names the exception");
  assert.ok(r.result.reasons.some((x) => /install/.test(x)), JSON.stringify(r.result.reasons));
  const audit = JSON.stringify(await h.chrome.storage.local.get(null));
  assert.ok(audit.includes(EXC), "the audit names the exception");
  // removing it is tightening: applies at once, the tab is denied again
  const after = await push(h, { deny: [...DEFAULT_DENY, "path:/oauth"], deny_except: [] });
  assert.equal(after.pending, null);
  const again = await h.call("page.click", { tab: 4, target: { selector: "#install" }, expect: { origin: "https://admin.shopify.com" } });
  assert.notEqual(again.ok, true);
});

test("frame_act_allow: tabs an agent opened (bot tabs) never act in frames", async () => {
  const h = await setup();
  await h.policy({ frame_act_allow: [APP, BANK], act_allow: [TOP] });
  const o = await h.call("tab.open", { url: `${TOP}/form.html` });
  assert.equal(o.ok, true, JSON.stringify(o));
  const id = [...h.tabs.keys()].at(-1); assert.ok(id >= 100, JSON.stringify(o.result));
  h.tabs.get(id).status = "complete";
  h.frames.set(id, FRAMES);
  const r = await h.call("page.click", { tab: id, target: { ref: "f5:e1" }, expect: { origin: TOP } });
  assert.deepEqual([r.error?.code, r.error?.reason, r.error?.tabKind], ["bad_target", "frame_ref", "bot"], JSON.stringify(r));
  assert.equal(dos(h).length, 0);
});

test("popup + confirm wording: plain words for the frame entry; the confirm names the frame", async () => {
  const { loosenWords, grantScope } = await import("../popup-view.js");
  assert.match(loosenWords({ kind: "frame_act_allow_added", value: APP }),
    /Let agents act inside asdescribed\.truefrontier\.app frames on act-shared tabs/);
  assert.match(grantScope({ op: "click", target: { text: "Scan" }, origin: TOP, frameOrigin: APP }), /in asdescribed\.truefrontier\.app frame/);
  const { detailRows } = await import("../confirm-view.js");
  const rows = detailRows({ op: "page.click", origin: TOP, frameId: 5, frameOrigin: APP });
  assert.ok(rows.some((r) => r[0] === "Inside frame" && r[1].includes(APP)));
});
