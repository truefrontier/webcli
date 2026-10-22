import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  DEFAULT_DENY, DEFAULT_POLICY, acceptPending, allowToPermission, applyIncoming, denied, denyException, exceptError,
  exceptMatches, loosening, matches, originOf, patternKind, pendingAffects, permissionPattern, rejectPending, tighten, validPolicy,
} from "../policy.js";

const cases = JSON.parse(readFileSync(new URL("./policy_cases.json", import.meta.url)));
const base = () => ({ implicit_active: "off", history: true, confirm_timeout: 60, read_allow: [], act_allow: [],
  deny: [...DEFAULT_DENY], hash: "h0" });
function build(over, hash) {
  const p = { ...base(), ...over, hash };
  if (over.deny_add) { p.deny = [...p.deny, ...over.deny_add]; delete p.deny_add; }
  if (over.deny_remove) { p.deny = p.deny.filter((d) => !over.deny_remove.includes(d)); delete p.deny_remove; }
  return p;
}

test("pattern kinds", () => {
  for (const [p, kind] of cases.kinds) assert.equal(patternKind(p), kind, p);
});

test("pattern matching", () => {
  for (const [p, url, want] of cases.match) assert.equal(matches(p, url), want, `${p} ~ ${url}`);
});

test("default deny (built-in + defaults)", () => {
  for (const [url, want] of cases.denied_default) assert.equal(!!denied(url, DEFAULT_POLICY), want, url);
});

test("built-in deny can't be removed by config", () => {
  const p = { ...base(), deny: [] };
  for (const [url, want] of cases.builtin_with_empty_deny) assert.equal(!!denied(url, p), want, url);
});

test("loosening is flagged, tightening is not", () => {
  for (const c of cases.loosening) {
    const kinds = loosening(build(c.old, "a"), build(c.new, "b")).map((l) => l.kind);
    assert.deepEqual(kinds, c.kinds, c.name);
  }
});

test("applyIncoming holds loosening as pending and applies tightening at once", () => {
  const inc = build({ deny: DEFAULT_DENY.filter((d) => d !== "*.chase.com").concat(["*.mybank.com"]),
    read_allow: ["https://docs.python.org"] }, "h1");
  const { state, accepted } = applyIncoming({ active: DEFAULT_POLICY, pending: null }, inc);
  assert.equal(accepted, false);
  assert.ok(state.pending);
  // tightening (mybank) is in force; the removed chase deny and the new allow are not
  assert.ok(denied("https://www.mybank.com/", state.active));
  assert.ok(denied("https://secure.chase.com/", state.active));
  assert.deepEqual(state.active.read_allow, []);
  assert.ok(pendingAffects(state, { url: "https://secure.chase.com/" }));
  assert.ok(pendingAffects(state, { url: "https://docs.python.org/3/" }));
  assert.ok(!pendingAffects(state, { url: "https://example.com/" }));
  const acc = acceptPending(state);
  assert.equal(acc.pending, null);
  assert.ok(!denied("https://secure.chase.com/", acc.active));
  const rej = rejectPending(state);
  assert.equal(rej.pending, null);
  assert.ok(denied("https://secure.chase.com/", rej.active));
});

test("a pure tightening is accepted immediately", () => {
  const inc = build({ deny_add: ["*.mybank.com"], history: false }, "h2");
  const { state, accepted } = applyIncoming({ active: DEFAULT_POLICY, pending: null }, inc);
  assert.equal(accepted, true);
  assert.equal(state.pending, null);
  assert.equal(state.active.history, false);
});

test("tighten keeps the stricter of each setting", () => {
  const t = tighten(build({ implicit_active: "read", read_allow: ["https://a.com"] }, "x"),
    build({ read_allow: ["https://a.com", "https://b.com"], history: false }, "y"));
  assert.equal(t.implicit_active, "off");
  assert.equal(t.history, false);
  assert.deepEqual(t.read_allow, ["https://a.com"]);
});

test("validPolicy rejects junk from the host", () => {
  assert.ok(validPolicy(base()));
  assert.ok(!validPolicy(null));
  assert.ok(!validPolicy({ ...base(), deny: ["chrome://x"] }));
  assert.ok(!validPolicy({ ...base(), implicit_active: "act" }));
  assert.ok(!validPolicy({ ...base(), history: "yes" }));
});

test("origins and permission patterns", () => {
  assert.equal(originOf("https://Example.com:443/a?b"), "https://example.com");
  assert.equal(originOf("http://127.0.0.1:8765/x"), "http://127.0.0.1:8765");
  assert.equal(originOf("chrome://settings"), null);
  assert.equal(permissionPattern("http://127.0.0.1:8765"), "http://127.0.0.1/*");
  assert.equal(allowToPermission("https://docs.python.org"), "https://docs.python.org/*");
  assert.equal(allowToPermission("*.example.com"), "*://*.example.com/*");
  assert.equal(allowToPermission("path:/x"), null);
});

// ---- 0.11.3 deny exceptions
const INSTALL = "https://admin.shopify.com/store/*/oauth/install";
const INSTALL_URL = "https://admin.shopify.com/store/kk-dev/oauth/install?client_id=abc123";

test("deny_except pattern validation (https, exact host, a path, * within one segment, no built-ins)", () => {
  for (const [p, ok] of cases.except_valid) assert.equal(exceptError(p) === null, ok, `${p}: ${exceptError(p)}`);
  assert.match(exceptError("https://*.shopify.com/store/*/oauth/install"), /exact/);
  assert.match(exceptError("http://admin.shopify.com/store/*/oauth/install"), /https/);
  assert.match(exceptError("https://admin.shopify.com"), /path is required/);
  assert.match(exceptError("https://chromewebstore.google.com/detail/x"), /built-in/);
});

test("deny_except matching: the install path only; other paths and hosts stay denied", () => {
  const ed = cases.except_denied;
  const p = build({ deny_add: ed.deny_add, deny_except: ed.deny_except }, "e1");
  for (const [url, isDenied, ex] of ed.urls) {
    assert.equal(!!denied(url, p), isDenied, `denied ${url}: ${denied(url, p)}`);
    assert.equal(denyException(url, p), ex, `exception ${url}`);
  }
  assert.equal(denied("https://admin.shopify.com/store/x/oauth/other", p), "deny: path:/oauth");
  assert.equal(denied("https://www.mybank.com/oauth/install", p), "deny: *.mybank.com");
  assert.ok(exceptMatches(INSTALL, INSTALL_URL));
  assert.ok(!exceptMatches(INSTALL, "https://admin.shopify.com/store/kk-dev/oauth/other"));
});

test("adding a deny exception is held pending until Accept; ops there wait (policy_pending)", () => {
  const inc = build({ deny_except: [INSTALL] }, "e2");
  const { state, accepted } = applyIncoming({ active: DEFAULT_POLICY, pending: null }, inc);
  assert.equal(accepted, false);
  assert.deepEqual(state.pending.loosening, [{ kind: "deny_except_added", value: INSTALL }]);
  assert.deepEqual(state.active.deny_except, []);
  assert.ok(denied(INSTALL_URL, state.active));
  assert.ok(pendingAffects(state, { url: INSTALL_URL }));
  assert.ok(!pendingAffects(state, { url: "https://admin.shopify.com/store/kk-dev/oauth/other" }));
  assert.ok(!pendingAffects(state, { url: "https://example.com/oauth/install" }));
  const acc = acceptPending(state);
  assert.ok(!denied(INSTALL_URL, acc.active));
  assert.ok(denied("https://admin.shopify.com/store/kk-dev/oauth/other", acc.active));
  assert.ok(denied(INSTALL_URL, rejectPending(state).active));
  // a changed exception is a new one: pending again
  const changed = applyIncoming(acc, build({ deny_except: ["https://admin.shopify.com/store/*/oauth/grant"] }, "e3"));
  assert.equal(changed.accepted, false);
  assert.deepEqual(changed.state.pending.loosening.map((l) => l.kind), ["deny_except_added"]);
  assert.deepEqual(changed.state.active.deny_except, [], "the old exception is dropped at once (tightening)");
});

test("removing a deny exception applies at once", () => {
  const acc = acceptPending(applyIncoming({ active: DEFAULT_POLICY, pending: null }, build({ deny_except: [INSTALL] }, "e4")).state);
  assert.ok(!denied(INSTALL_URL, acc.active));
  const { state, accepted } = applyIncoming(acc, build({}, "e5"));
  assert.equal(accepted, true);
  assert.equal(state.pending, null);
  assert.ok(denied(INSTALL_URL, state.active));
});

test("validPolicy rejects bad deny_except from the host; older hosts without the key still validate", () => {
  assert.ok(validPolicy({ ...base(), deny_except: [INSTALL] }));
  const { deny_except: _, ...old } = { ...base(), deny_except: [] };
  assert.ok(validPolicy(old));
  for (const bad of ["https://*.shopify.com/x/y", "http://a.com/x", "https://a.com", "https://chromewebstore.google.com/x", "chrome://x/y"]) {
    assert.ok(!validPolicy({ ...base(), deny_except: [bad] }), bad);
  }
  assert.ok(!validPolicy({ ...base(), deny_except: INSTALL }));
});
