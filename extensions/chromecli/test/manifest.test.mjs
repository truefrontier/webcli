import { test } from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync, existsSync } from "node:fs";

const here = (p) => new URL(`../${p}`, import.meta.url);
const manifest = JSON.parse(readFileSync(here("manifest.json")));

function idFromKey(b64) {
  const hex = createHash("sha256").update(Buffer.from(b64, "base64")).digest("hex").slice(0, 32);
  return [...hex].map((c) => String.fromCharCode(97 + parseInt(c, 16))).join("");
}

test("manifest key gives the committed extension ID", () => {
  const pub = readFileSync(here("extension-key-pub.txt"), "utf8").trim();
  assert.equal(manifest.key, pub);
  assert.equal(idFromKey(manifest.key), readFileSync(here("EXTENSION_ID"), "utf8").trim());
});

test("no private key is committed", () => {
  for (const f of ["extension-key.pem", "key.pem", "extension-key-b64.txt"]) assert.ok(!existsSync(here(f)), f);
  assert.ok(!readFileSync(here("extension-key-pub.txt"), "utf8").includes("PRIVATE"));
});

test("permissions: no blanket host access, no debugger", () => {
  assert.equal(manifest.manifest_version, 3);
  assert.ok(!("host_permissions" in manifest));
  assert.ok(!manifest.permissions.includes("debugger"));
  assert.ok(!manifest.permissions.includes("<all_urls>"));
  assert.deepEqual(manifest.optional_host_permissions, ["http://*/*", "https://*/*"]);
  assert.ok(manifest.permissions.includes("nativeMessaging"));
  assert.ok(manifest.commands["pause-agents"]);
});

test("every referenced file exists", () => {
  for (const f of [manifest.background.service_worker, manifest.action.default_popup,
    ...Object.values(manifest.icons), "policy.js", "shares.js", "page/extract.js", "popup.js"]) {
    assert.ok(existsSync(here(f)), f);
  }
});

test("no remote code or eval in extension scripts", () => {
  for (const f of ["background.js", "policy.js", "shares.js", "page/extract.js", "popup.js"]) {
    const src = readFileSync(here(f), "utf8");
    assert.ok(!/\beval\s*\(|new Function\s*\(/.test(src), `${f}: eval/new Function`);
    assert.ok(!/import\s*\(\s*["']https?:/.test(src) && !/<script[^>]+src=["']https?:/.test(src), `${f}: remote code`);
  }
});
