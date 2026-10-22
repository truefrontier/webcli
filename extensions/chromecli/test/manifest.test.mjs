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
    ...Object.values(manifest.icons), ...Object.values(manifest.action.default_icon), "policy.js", "shares.js",
    "page/extract.js", "page/actions.js", "gate.js", "popup.js", "popup-view.js", "popup.css", "confirm.html", "confirm.js",
    "confirm-view.js", "confirm.css", manifest.options_ui.page, "options.js", "options.css", "jev.js", "page/snapshot.js"]) {
    assert.ok(existsSync(here(f)), f);
  }
});

test("no remote code or eval in extension scripts", () => {
  for (const f of ["background.js", "policy.js", "shares.js", "page/extract.js", "page/actions.js", "gate.js", "popup.js",
    "popup-view.js", "popup.html", "popup.css", "confirm.html", "confirm.js", "confirm-view.js", "confirm.css",
    "options.html", "options.js", "options.css", "jev.js", "page/snapshot.js"]) {
    const src = readFileSync(here(f), "utf8");
    assert.ok(!/\beval\s*\(|new Function\s*\(/.test(src), `${f}: eval/new Function`);
    assert.ok(!/import\s*\(\s*["']https?:/.test(src) && !/<script[^>]+src=["']https?:/.test(src), `${f}: remote code`);
  }
});

for (const [page, sheet] of [["popup.html", "popup.css"], ["confirm.html", "confirm.css"], ["options.html", "options.css"]]) test(`${page} loads no remote assets and no inline script`, () => {
  const html = readFileSync(here(page), "utf8");
  const css = readFileSync(here(sheet), "utf8");
  assert.ok(!/(?:src|href)=["']https?:/i.test(html), `${page}: remote src/href`);
  assert.ok(!/<script(?![^>]*\bsrc=)[^>]*>/i.test(html), `${page}: inline <script> (blocked by the MV3 CSP)`);
  assert.ok(!/\son[a-z]+=/i.test(html), `${page}: inline event handler`);
  assert.ok(!/@import|url\(\s*["']?https?:/i.test(css), `${sheet}: remote import/url`);
});

test("the confirm window is not web-accessible and Deny is the default focus", () => {
  assert.ok(!("web_accessible_resources" in manifest));
  const html = readFileSync(here("confirm.html"), "utf8");
  assert.match(html, /<button id="deny"[^>]*autofocus/);
  assert.match(html, /<button id="allow"[^>]*disabled/);
  assert.match(html, /Gated because/);
});

test("Jev (BYOK): TypeSafe is an optional permission requested on Save; the key never goes back into the page", () => {
  assert.deepEqual(manifest.options_ui, { page: "options.html", open_in_tab: true });
  assert.ok(manifest.optional_host_permissions.some((p) => p === "https://*/*"));
  assert.ok(!(manifest.host_permissions || []).some((p) => /typesafe/.test(p)));
  const opts = readFileSync(here("options.js"), "utf8");
  assert.match(opts, /permissions\.request\(\{ origins: \[JEV_PERMISSION\] \}\)/);
  assert.ok(!/\$\("key"\)\.value\s*=(?!=)\s*[^"\s]/.test(opts), "options.js writes the saved key into the field");
  assert.ok(!/console\.|fetch\(/.test(opts), "options.js logs or fetches");
  const pop = readFileSync(here("popup.js"), "utf8");
  assert.ok(!/typesafeApiKey/.test(pop), "the popup reads the key");
  // Only the service worker's fetch uses the key.
  for (const f of ["popup.js", "confirm.js", "page/actions.js", "page/extract.js", "page/snapshot.js", "gate.js"]) {
    assert.ok(!/typesafeApiKey|api\.typesafe\.ai/.test(readFileSync(here(f), "utf8")), f);
  }
});
