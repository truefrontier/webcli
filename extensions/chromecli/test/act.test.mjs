// P2 tab actions and P3 page actions in the service worker, with a mocked chrome.* and a fake page.
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadBackground } from "./harness.mjs";

const FORM = "http://127.0.0.1:8765";
const EXPECT = { origin: FORM };
const els = {
  place: { tag: "button", role: "button", name: "Place order", type: "submit", inForm: true, defaultSubmit: true, outerHTML: '<button id="place" type="submit">Place order</button>' },
  del: { tag: "button", role: "button", name: "Delete account", type: "button", inForm: true, defaultSubmit: false, outerHTML: '<button id="delete" type="button">Delete account</button>' },
  next: { tag: "a", role: "link", name: "Next", type: "", inForm: true, outerHTML: '<a id="next" href="nav.html">Next</a>' },
  email: { tag: "input", role: "textbox", name: "Email", type: "email", id: "email", nameAttr: "email", autocomplete: "email", inForm: true, isField: true, outerHTML: '<input id="email" type="email">' },
  password: { tag: "input", role: "textbox", name: "Password", type: "password", id: "password", nameAttr: "password", autocomplete: "current-password", inForm: true, isField: true, outerHTML: '<input id="password" type="password">' },
};
const bySel = { "#place": els.place, "#delete": els.del, "#next": els.next, "#email": els.email, "#password": els.password };

// A fake page: describe returns the element; do returns dispatched + readBack (configurable).
function fakePage(opts = {}) {
  return (tabId, spec, name) => {
    if (name === "pageProbe") return { ok: true, found: !!opts.textFound };
    const el = spec.target?.selector ? bySel[spec.target.selector] : spec.target?.text === "Next" ? els.next : null;
    if (!el && spec.op !== "press") return { ok: false, code: "element_not_found", message: "No visible element matches the target." };
    if (spec.mode !== "do") return { ok: true, element: el, fingerprint: `fp:${el?.name}` };
    return { ok: true, element: el, dispatched: true, readBack: opts.readBack ?? true };
  };
}

async function actTab(h, tab = 1) {
  assert.deepEqual(await h.share(tab, "act"), { ok: true });
}

// ---- P2
test("tab and page actions need confirm: true (the CLI's --confirm)", async () => {
  const h = await loadBackground();
  for (const op of ["tab.open", "tab.close", "tab.focus", "tab.navigate", "page.click", "page.fill"]) {
    const r = await h.call(op, { tab: 1, url: "https://example.com/" }, { confirm: false });
    assert.equal(r.error.code, "bad_request", op);
  }
  assert.equal(h.tabsCreated.length + h.tabsRemoved.length + h.injected.length, 0);
});

test("open an act_allow origin: no confirm, background tab, auto Share-act, botOpened", async () => {
  const h = await loadBackground();
  await h.policy({ act_allow: [FORM] });
  h.granted.add("http://127.0.0.1/*");
  const r = await h.call("tab.open", { url: `${FORM}/form.html` });
  assert.equal(r.ok, true, JSON.stringify(r));
  assert.equal(r.result.share, "act");
  assert.equal(r.result.botOpened, true);
  assert.equal(r.result.gated, false);
  assert.equal(h.windowsCreated.filter((w) => w.type === "popup").length, 0);
  assert.equal(h.tabsCreated[0].active, false);
  const list = (await h.call("tabs.list")).result;
  assert.ok(list.some((t) => t.id === r.result.tab && t.share === "act" && t.botOpened));
});

test("open a read_allow origin: auto Share-read", async () => {
  const h = await loadBackground();
  await h.policy({ read_allow: ["https://docs.python.org"] });
  h.granted.add("https://docs.python.org/*");
  const r = await h.call("tab.open", { url: "https://docs.python.org/3/" });
  assert.equal(r.result.share, "read");
});

test("open outside both allowlists: in-browser confirm; Allow -> opens unshared (metadata only)", async () => {
  const h = await loadBackground();
  const id = h.start("tab.open", { url: "https://example.org/" });
  const cid = await h.confirmWindow();
  const info = await h.message({ type: "confirmGet", id: cid }, `confirm.html?id=${cid}`, { id: 999 });
  assert.equal(info.ok, true);
  assert.deepEqual(info.info.reasons, ["origin_not_allowlisted"]);
  assert.equal(h.tabsCreated.length, 0, "nothing opens before Allow");
  await h.answer(cid, "allow");
  const r = await h.reply(id);
  assert.equal(r.ok, true, JSON.stringify(r));
  assert.equal(r.result.share, null);
  assert.equal(r.result.confirmedInBrowser, true);
  const list = (await h.call("tabs.list")).result;
  assert.ok(list.some((t) => t.id === r.result.tab && t.share === null && t.botOpened));
  assert.equal((await h.call("tabs.read", { tab: r.result.tab })).error.code, "not_shared");
});

test("open: Deny -> declined, nothing opened; closing the window is a Deny too", async () => {
  const h = await loadBackground();
  let id = h.start("tab.open", { url: "https://example.org/" });
  let cid = await h.confirmWindow(1);
  await h.answer(cid, "deny");
  assert.equal((await h.reply(id)).error.code, "declined");
  id = h.start("tab.open", { url: "https://example.org/" });
  await h.confirmWindow(2);
  const win = h.windowsCreated.filter((w) => w.type === "popup")[1];
  await h.chrome.windows.onRemoved.fire(win.id);
  assert.equal((await h.reply(id)).error.code, "declined");
  assert.equal(h.tabsCreated.length, 0);
});

test("open / navigate to a denied origin -> origin_blocked, no confirm", async () => {
  const h = await loadBackground();
  assert.equal((await h.call("tab.open", { url: "https://bank.chase.com/" })).error.code, "origin_blocked");
  assert.equal((await h.call("tab.open", { url: "chrome://settings" })).error.code, "origin_blocked");
  await actTab(h, 2);
  assert.equal((await h.call("tab.navigate", { tab: 2, url: "https://accounts.google.com/" })).error.code, "origin_blocked");
  assert.equal(h.windowsCreated.length, 0);
});

test("close: refused for a human-opened read-shared or unshared tab; allowed for act-shared and bot-opened", async () => {
  const h = await loadBackground();
  await h.share(2, "read");
  assert.equal((await h.call("tab.close", { tab: 2 })).error.code, "not_act_shared");
  assert.equal((await h.call("tab.close", { tab: 1 })).error.code, "not_shared");
  assert.deepEqual(h.tabsRemoved, []);
  await actTab(h, 1);
  assert.equal((await h.call("tab.close", { tab: 1 })).ok, true);
  await h.policy({ act_allow: [FORM] });
  h.granted.add("http://127.0.0.1/*");
  const opened = (await h.call("tab.open", { url: `${FORM}/nav.html` })).result.tab;
  await h.message({ type: "unshare", tabId: opened });
  assert.equal((await h.call("tab.close", { tab: opened })).ok, true, "bot-opened, even unshared");
  assert.deepEqual(h.tabsRemoved, [1, opened]);
});

test("navigate to a non-allowlisted origin is gated; cross-origin makes it bot-opened", async () => {
  const h = await loadBackground();
  await actTab(h, 2);
  const id = h.start("tab.navigate", { tab: 2, url: "https://example.net/x" });
  const cid = await h.confirmWindow();
  assert.equal(h.tabsUpdated.length, 0);
  await h.answer(cid, "allow");
  const r = await h.reply(id, 12000);
  assert.equal(r.ok, true, JSON.stringify(r));
  assert.equal(r.result.crossOrigin, true);
  assert.equal(r.result.botOpened, true);
  assert.equal(r.result.share, null);
  assert.deepEqual(r.result.reasons, ["origin_not_allowlisted"]);
});

test("navigate needs act-share or bot-opened, and honors expect", async () => {
  const h = await loadBackground();
  await h.share(2, "read");
  assert.equal((await h.call("tab.navigate", { tab: 2, url: "https://example.com/b" })).error.code, "not_act_shared");
  await actTab(h, 2);
  const r = await h.call("tab.navigate", { tab: 2, url: "https://example.com/b", expect: { origin: "https://other.com" } });
  assert.equal(r.error.code, "url_mismatch");
});

test("focus brings an act-shared tab forward", async () => {
  const h = await loadBackground();
  await actTab(h, 2);
  const r = await h.call("tab.focus", { tab: 2 });
  assert.equal(r.ok, true);
  assert.ok(h.tabsUpdated.some((u) => u.id === 2 && u.active));
});

// ---- P3
test("page actions: missing expect -> missing_expect; wrong origin / prefix -> url_mismatch; read share -> not_act_shared", async () => {
  const h = await loadBackground({ page: fakePage() });
  await h.share(1, "read");
  assert.equal((await h.call("page.click", { tab: 1, target: { selector: "#next" }, expect: EXPECT })).error.code, "not_act_shared");
  await actTab(h, 1);
  assert.equal((await h.call("page.click", { tab: 1, target: { selector: "#next" } })).error.code, "missing_expect");
  assert.equal((await h.call("page.click", { tab: 1, target: { selector: "#next" }, expect: { origin: "https://x.com" } })).error.code, "url_mismatch");
  assert.equal((await h.call("page.click", { tab: 1, target: { selector: "#next" }, expect: { urlPrefix: `${FORM}/other` } })).error.code, "url_mismatch");
  assert.equal(h.injected.length, 0);
});

test("page actions: target must be exactly one; --find is P4", async () => {
  const h = await loadBackground({ page: fakePage() });
  await actTab(h, 1);
  assert.equal((await h.call("page.click", { tab: 1, target: {}, expect: EXPECT })).error.code, "bad_target");
  assert.equal((await h.call("page.click", { tab: 1, target: { selector: "a", ref: "e1" }, expect: EXPECT })).error.code, "bad_target");
  const f = await h.call("page.click", { tab: 1, target: { find: "the next link" }, expect: EXPECT });
  assert.equal(f.error.code, "bad_target");
  assert.match(f.error.message, /Jev \(P4\)/);
});

test("click a plain link: no prompt, dispatched in the ISOLATED world", async () => {
  const h = await loadBackground({ page: fakePage() });
  await actTab(h, 1);
  const r = await h.call("page.click", { tab: 1, target: { text: "Next" }, expect: EXPECT });
  assert.equal(r.ok, true, JSON.stringify(r));
  assert.equal(r.result.gated, false);
  assert.equal(r.result.dispatched, true);
  assert.equal(h.windowsCreated.length, 0);
  assert.ok(h.injected.every((i) => i.world === "ISOLATED"));
  assert.deepEqual(h.injected.map((i) => i.spec.mode), ["describe", "do"]);
});

test("click 'Place order': confirm; Allow -> dispatched; --verify-text passes", async () => {
  const h = await loadBackground({ page: fakePage({ textFound: true }) });
  await actTab(h, 1);
  const id = h.start("page.click", { tab: 1, target: { selector: "#place" }, expect: EXPECT, verify: { text: "submitted" } });
  const cid = await h.confirmWindow();
  const got = await h.message({ type: "confirmGet", id: cid }, `confirm.html?id=${cid}`, { id: 999 });
  assert.deepEqual(got.info.reasons, ["submit_button", "submit_word:order"]);
  assert.equal(got.info.element.name, "Place order");
  assert.equal(h.injected.filter((i) => i.spec?.mode === "do").length, 0, "nothing dispatched before Allow");
  await h.answer(cid, "allow");
  const r = await h.reply(id, 5000);
  assert.equal(r.ok, true, JSON.stringify(r));
  assert.equal(r.result.confirmedInBrowser, true);
  assert.equal(r.result.verified, true);
});

test("click 'Delete account' (type=button) is gated by its label; Deny -> declined", async () => {
  const h = await loadBackground({ page: fakePage() });
  await actTab(h, 1);
  const id = h.start("page.click", { tab: 1, target: { selector: "#delete" }, expect: EXPECT });
  const cid = await h.confirmWindow();
  await h.answer(cid, "deny");
  const r = await h.reply(id);
  assert.equal(r.error.code, "declined");
  assert.deepEqual(r.error.reasons, ["submit_word:delete"]);
  assert.equal(h.injected.filter((i) => i.spec?.mode === "do").length, 0);
});

test("fill email: no prompt, read-back verified; fill password: confirm with a masked value", async () => {
  const h = await loadBackground({ page: fakePage() });
  await actTab(h, 1);
  const r = await h.call("page.fill", { tab: 1, target: { selector: "#email" }, value: "kevin@example.com", expect: EXPECT });
  assert.equal(r.ok, true);
  assert.equal(r.result.verified, true);
  assert.equal(h.windowsCreated.length, 0);
  const id = h.start("page.fill", { tab: 1, target: { selector: "#password" }, value: "hunter2-secret", expect: EXPECT });
  const cid = await h.confirmWindow();
  const got = await h.message({ type: "confirmGet", id: cid }, `confirm.html?id=${cid}`, { id: 999 });
  assert.equal(got.info.value.masked, true);
  assert.ok(!JSON.stringify(got).includes("hunter2-secret"));
  assert.deepEqual(got.info.reasons, ["password_field", "credential_field"]);
  await h.answer(cid, "deny");
  assert.equal((await h.reply(id)).error.code, "declined");
  // the typed value never goes back to the host
  assert.ok(!JSON.stringify(h.posted).includes("hunter2-secret"));
  assert.ok(!JSON.stringify(h.posted).includes("kevin@example.com"));
});

test("failed read-back after dispatch -> sent_unverified (exit 8)", async () => {
  const h = await loadBackground({ page: fakePage({ readBack: false }) });
  await actTab(h, 1);
  const r = await h.call("page.fill", { tab: 1, target: { selector: "#email" }, value: "x@y.z", expect: EXPECT });
  assert.equal(r.error.code, "sent_unverified");
  assert.equal(r.error.dispatched, true);
  assert.deepEqual(r.error.failed, ["read_back"]);
});

test("press Enter in a form field is gated; Escape is not", async () => {
  const h = await loadBackground({ page: fakePage() });
  await actTab(h, 1);
  const ok = await h.call("page.press", { tab: 1, key: "Escape", target: { selector: "#email" }, expect: EXPECT });
  assert.equal(ok.ok, true);
  const id = h.start("page.press", { tab: 1, key: "Enter", target: { selector: "#email" }, expect: EXPECT });
  const cid = await h.confirmWindow();
  const got = await h.message({ type: "confirmGet", id: cid }, `confirm.html?id=${cid}`, { id: 999 });
  assert.deepEqual(got.info.reasons, ["enter_in_form"]);
  await h.answer(cid, "deny");
  assert.equal((await h.reply(id)).error.code, "declined");
});

test("eval is disabled (eval_disabled), after the usual preconditions", async () => {
  const h = await loadBackground({ page: fakePage() });
  await actTab(h, 1);
  assert.equal((await h.call("page.eval", { tab: 1, code: "1+1" })).error.code, "missing_expect");
  assert.equal((await h.call("page.eval", { tab: 1, code: "1+1", expect: EXPECT })).error.code, "eval_disabled");
  assert.equal(h.injected.length, 0);
});

test("confirms queue one at a time; Pause closes the pending one and cancels the queue (paused)", async () => {
  const h = await loadBackground({ page: fakePage() });
  await actTab(h, 1);
  const a = h.start("page.click", { tab: 1, target: { selector: "#place" }, expect: EXPECT });
  const b = h.start("page.click", { tab: 1, target: { selector: "#delete" }, expect: EXPECT });
  await h.confirmWindow(1);
  await new Promise((r) => setTimeout(r, 30));
  assert.equal(h.windowsCreated.filter((w) => w.type === "popup").length, 1, "one confirm window at a time");
  const st = await h.message({ type: "state", tabId: 1 });
  assert.equal(st.confirms, 2);
  await h.message({ type: "pause", paused: true });
  assert.equal((await h.reply(a)).error.code, "paused");
  assert.equal((await h.reply(b)).error.code, "paused");
  assert.equal(h.windowsRemoved.length, 1);
  assert.equal(h.injected.filter((i) => i.spec?.mode === "do").length, 0);
});

test("no answer before the deadline -> confirm_timeout", async () => {
  const h = await loadBackground({ page: fakePage() });
  await actTab(h, 1);
  const id = h.start("page.click", { tab: 1, target: { selector: "#place" }, expect: EXPECT }, { deadlineMs: Date.now() + 3300 });
  await h.confirmWindow();
  const r = await h.reply(id, 3000);
  assert.equal(r.error.code, "confirm_timeout");
  assert.equal(h.windowsRemoved.length, 1);
});

test("preconditions are re-checked after Allow (tab moved -> url_mismatch, nothing dispatched)", async () => {
  const h = await loadBackground({ page: fakePage() });
  await actTab(h, 1);
  const id = h.start("page.click", { tab: 1, target: { selector: "#place" }, expect: { urlPrefix: `${FORM}/form.html` } });
  const cid = await h.confirmWindow();
  h.tabs.get(1).url = `${FORM}/nav.html`; // same origin: share kept, but the prefix no longer holds
  await h.answer(cid, "allow");
  const r = await h.reply(id);
  assert.equal(r.error.code, "url_mismatch");
  assert.equal(h.injected.filter((i) => i.spec?.mode === "do").length, 0);
});

test("only confirm.html may answer, and only confirm messages; a web page can't", async () => {
  const h = await loadBackground({ page: fakePage() });
  await actTab(h, 1);
  const id = h.start("page.click", { tab: 1, target: { selector: "#place" }, expect: EXPECT });
  const cid = await h.confirmWindow();
  // from a tab with a web URL: ignored
  let answered = false;
  for (const f of h.chrome.runtime.onMessage.fns) {
    assert.equal(f({ type: "confirmAnswer", id: cid, answer: "allow" }, { id: h.chrome.runtime.id, tab: { id: 1 }, url: `${FORM}/form.html` }, () => { answered = true; }), false);
  }
  // confirm.html can't share tabs
  assert.equal(await h.message({ type: "share", tabId: 2, level: "act" }, `confirm.html?id=${cid}`, { id: 999 }), undefined);
  // the popup can't answer a confirm either (it has no window for it), but the wrong id is refused anyway
  assert.equal((await h.answer("wrong-id", "allow")).ok, false);
  assert.equal(answered, false);
  await h.answer(cid, "deny");
  assert.equal((await h.reply(id)).error.code, "declined");
});

test("wait: read share is enough; times out with timeout", async () => {
  const h = await loadBackground({ page: fakePage({ textFound: false }) });
  await h.share(1, "read");
  const r = await h.call("page.wait", { tab: 1, text: "submitted", timeoutMs: 300 }, { confirm: false });
  assert.equal(r.error.code, "timeout");
  const h2 = await loadBackground({ page: fakePage({ textFound: true }) });
  await h2.share(1, "read");
  assert.equal((await h2.call("page.wait", { tab: 1, text: "submitted", timeoutMs: 300 }, { confirm: false })).result.found, true);
});
