import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  DEFAULT_DENY, DEFAULT_POLICY, acceptPending, allowToPermission, applyIncoming, denied, loosening, matches,
  originOf, patternKind, pendingAffects, permissionPattern, rejectPending, tighten, validPolicy,
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
