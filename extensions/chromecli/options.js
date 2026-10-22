import { DEFAULT_FLOOR, DEFAULT_MODEL, DEFAULT_RISK_FLOOR, JEV_PERMISSION } from "./jev.js";

const $ = (id) => document.getElementById(id);
let hasKey = false;

function msg(text, err = false) {
  $("msg").textContent = text;
  $("msg").className = err ? "msg err" : "msg";
  $("msg").hidden = !text;
}

function renderState() {
  $("jev-pill").dataset.status = hasKey ? "on" : "off";
  $("jev-pill-label").textContent = hasKey ? "Jev on" : "Jev off";
  $("key-state").textContent = hasKey ? "A key is saved. Leave the field empty to keep it." : "No key saved: chromecli uses its built-in heuristics.";
  $("key-state").classList.toggle("on", hasKey);
  $("remove").disabled = !hasKey;
  $("test").disabled = !hasKey;
}

async function load() {
  $("version").textContent = `v${chrome.runtime.getManifest().version}`;
  // Read presence only; the saved key is never put back into the page.
  const st = await chrome.storage.local.get(["typesafeApiKey", "jevModel", "jevFloor", "jevRiskFloor"]);
  hasKey = typeof st.typesafeApiKey === "string" && !!st.typesafeApiKey.trim();
  $("model").value = st.jevModel || DEFAULT_MODEL;
  $("floor").value = st.jevFloor ?? DEFAULT_FLOOR;
  $("risk-floor").value = st.jevRiskFloor ?? DEFAULT_RISK_FLOOR;
  renderState();
}

$("form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const key = $("key").value.trim();
  const model = $("model").value.trim() || DEFAULT_MODEL;
  const floor = Number($("floor").value);
  const riskFloor = Number($("risk-floor").value);
  if (!(floor >= 0.5 && floor <= 0.99)) { msg("Confidence floor must be between 0.5 and 0.99.", true); return; }
  if (!(riskFloor >= 0.5 && riskFloor <= 0.95)) { msg("Risk floor must be between 0.5 and 0.95.", true); return; }
  if (key || hasKey) {
    // Straight from the click (a user gesture): ask for the one host Jev needs.
    const granted = await chrome.permissions.request({ origins: [JEV_PERMISSION] }).catch(() => false);
    if (!granted) { msg("Chrome didn't grant access to api.typesafe.ai; nothing saved.", true); return; }
  }
  const data = { jevModel: model, jevFloor: floor, jevRiskFloor: riskFloor };
  if (key) data.typesafeApiKey = key;
  await chrome.storage.local.set(data);
  $("key").value = "";
  hasKey = hasKey || !!key;
  renderState();
  msg(key ? "Saved. Jev is on." : "Saved.");
});

$("remove").addEventListener("click", async () => {
  await chrome.storage.local.remove("typesafeApiKey");
  hasKey = false;
  renderState();
  msg("Key removed. Jev is off.");
});

$("test").addEventListener("click", async () => {
  $("test").disabled = true;
  msg("Asking Jev…");
  const res = await chrome.runtime.sendMessage({ type: "jevTest" }).catch((e) => ({ ok: false, error: String(e?.message || e) }));
  msg(res?.ok ? `Jev answered (classified a "Next page" link as ${res.choice}).` : `Jev test failed: ${res?.error || "unknown error"}`, !res?.ok);
  $("test").disabled = !hasKey;
});

$("reveal").addEventListener("click", () => {
  const show = $("key").type === "password";
  $("key").type = show ? "text" : "password";
  $("reveal").textContent = show ? "Hide" : "Show";
  $("reveal").setAttribute("aria-pressed", String(show));
});

load();
