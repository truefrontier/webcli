import { allowToPermission, permissionPattern } from "./policy.js";
import { auditRow, confirmsLabel, hostOf, jevView, LEVEL_HINT, levelOf, loosenLabel, monogram, safeFavicon, shortcutKeys, statusOf } from "./popup-view.js";

const $ = (id) => document.getElementById(id);
let tab = null;
let state = null;

const send = (msg) => chrome.runtime.sendMessage(msg);

function el(tag, props = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") n.className = v;
    else if (k.startsWith("aria-") || k === "title" || k === "role") n.setAttribute(k, v);
    else n[k] = v;
  }
  n.append(...kids.filter((k) => k != null));
  return n;
}

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "ic");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

// Favicon tile: the tab's icon when Chrome has one, else a monogram.
function setFavicon(node, favIconUrl, title, origin) {
  const letter = monogram(title, origin);
  const src = safeFavicon(favIconUrl);
  node.replaceChildren();
  if (src) {
    const img = el("img", { alt: "", referrerPolicy: "no-referrer", decoding: "async" });
    img.addEventListener("error", () => node.replaceChildren(letter), { once: true });
    img.src = src;
    node.append(img);
  } else node.append(letter);
}

async function load() {
  const [t] = await chrome.tabs.query({ active: true, currentWindow: true });
  state = await send({ type: "state", tabId: t ? t.id : null });
  tab = state.tab;
  if (tab && !tab.favIconUrl && t?.favIconUrl) tab.favIconUrl = t.favIconUrl;
  render();
}

function render() {
  $("version").textContent = `v${state.version}`;

  // header status + host alert
  const st = statusOf(state);
  $("status").dataset.status = st.key;
  $("status-label").textContent = st.label;
  $("status").setAttribute("aria-label", `Status: ${st.label}`);
  const h = state.host || {};
  $("host-alert").hidden = !!h.connected;
  $("host-error").textContent = h.lastError || "Waiting for chromecli-host. Run chromecli doctor.";

  // agent actions waiting in the confirm window
  $("confirms").hidden = !state.confirms;
  $("confirms-label").textContent = confirmsLabel(state.confirms);

  // current tab
  $("title").textContent = tab ? tab.title || "(untitled)" : "No tab";
  $("title").title = tab?.title || "";
  $("origin").textContent = tab ? tab.origin || tab.url || "" : "";
  $("origin").title = tab ? tab.url || "" : "";
  setFavicon($("fav"), tab?.favIconUrl, tab?.title, tab?.origin);

  const reason = state.deniedReason;
  $("denied").hidden = !reason;
  $("denied-reason").textContent = reason || "";
  $("denied-why").textContent = !reason ? ""
    : /^built-in/.test(reason) ? "Browser pages, other schemes, and the Web Store are never shareable."
    : /^deny/.test(reason) ? "Sign-in, banking, payment, and password-manager pages stay private. Edit deny in [chromecli] to change it (that needs your Accept)."
    : "Only http(s) pages can be shared.";

  const shareable = !!(tab && tab.origin && !reason);
  const level = levelOf(state.share);
  $("seg").dataset.level = level;
  $("seg").classList.toggle("locked", !shareable && level === "off");
  for (const b of $("seg").querySelectorAll(".seg-btn")) {
    const on = b.dataset.level === level;
    b.setAttribute("aria-pressed", String(on));
    b.disabled = b.dataset.level === "off" ? false : !shareable;
  }
  $("hint").textContent = !tab ? "" : !shareable && level === "off" ? "" : LEVEL_HINT[level];
  $("hint").hidden = !$("hint").textContent;

  // pause
  $("pause").setAttribute("aria-checked", String(!!state.paused));
  $("pause-label").textContent = state.paused ? "Agents paused" : "Pause agents";
  document.querySelector(".pause-card").classList.toggle("on", !!state.paused);

  // Jev
  const jv = jevView(!!state.jevConfigured);
  $("jev-chip").textContent = jv.chip;
  $("jev-chip").dataset.on = String(jv.on);
  $("jev-sub").textContent = jv.sub;

  // pending policy
  $("pending").hidden = !state.pending;
  $("pending-list").replaceChildren(...(state.pending?.loosening || []).map((l) =>
    el("li", {}, el("span", { class: "k" }, loosenLabel(l)), el("span", { class: "v" }, String(l.value)))));

  // shared tabs
  const shared = [...(state.shared || [])].sort((a, b) => (b.tabId === tab?.id) - (a.tabId === tab?.id) || (b.at || 0) - (a.at || 0));
  const count = state.shareCount ?? shared.length;
  $("shared-count").textContent = count ? String(count) : "";
  $("unshare-all").disabled = !count;
  $("shared").replaceChildren(...shared.map(sharedRow));
  $("shared-empty").hidden = count > 0;

  // audit tail
  const audit = state.audit || [];
  $("audit-count").textContent = audit.length ? String(audit.length) : "";
  $("audit").replaceChildren(...audit.map((a) => {
    const r = auditRow(a);
    return el("li", { title: `${a.at} ${r.op} ${r.target} → ${r.outcome}` },
      el("span", { class: "t" }, r.time), el("span", { class: "op" }, r.op),
      el("span", { class: "tg" }, r.target), el("span", { class: `out ${r.tone}` }, r.outcome));
  }));
  $("audit").hidden = !audit.length;
  $("audit-empty").hidden = !!audit.length;
}

function sharedRow(s) {
  const fav = el("span", { class: "fav", "aria-hidden": "true" });
  setFavicon(fav, s.favIconUrl, s.title, s.origin);
  const host = hostOf(s.origin);
  const title = s.title || host;
  const isCurrent = s.tabId === tab?.id;
  const main = el("button", { class: "srow-main", title: `${title}\n${host}`, "aria-label": `Switch to ${title} (${host})` },
    el("span", { class: "srow-title" }, title));
  main.addEventListener("click", async () => {
    try {
      await chrome.tabs.update(s.tabId, { active: true });
      if (s.windowId != null) await chrome.windows.update(s.windowId, { focused: true });
    } catch { /* tab gone */ }
  });
  const x = el("button", { class: "icon-btn", title: "Unshare", "aria-label": `Unshare ${title}` }, icon("x"));
  x.addEventListener("click", async () => { await send({ type: "unshare", tabId: s.tabId }); msg("Unshared."); await load(); });
  return el("li", { class: `srow${isCurrent ? " current" : ""}` }, fav, main,
    el("span", { class: `chip ${s.level}`, title: s.level === "act" ? "Shared: act (badge A)" : "Shared: read (badge R)" },
      s.level === "act" ? "Act" : "Read"), x);
}

let msgTimer = null;
function msg(text, err = false) {
  clearTimeout(msgTimer);
  $("msg").textContent = text;
  $("msg").className = err ? "msg err" : "msg";
  $("msg").hidden = !text;
  if (text && !err) msgTimer = setTimeout(() => { $("msg").hidden = true; }, 2500);
}

async function share(level) {
  // permissions.request must run straight from the click (user gesture): nothing is awaited before it.
  const granted = chrome.permissions.request({ origins: [permissionPattern(tab.origin)] });
  if (!(await granted)) { msg("Chrome didn't grant access to this site; not shared.", true); return; }
  const res = await send({ type: "share", tabId: tab.id, origin: tab.origin, level });
  if (!res.ok) msg(res.error, true); else msg(level === "act" ? "Shared (act)." : "Shared (read).");
  await load();
}

async function unshare() {
  await send({ type: "unshare", tabId: tab.id });
  msg("Unshared.");
  await load();
}

for (const b of document.querySelectorAll(".seg-btn")) {
  b.addEventListener("click", () => {
    if (!tab || b.disabled || b.getAttribute("aria-pressed") === "true") return;
    if (b.dataset.level === "off") unshare(); else share(b.dataset.level);
  });
}
// Arrow keys move focus inside the segmented control (activation stays on Enter / Space / click).
$("seg").addEventListener("keydown", (e) => {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
  const btns = [...$("seg").querySelectorAll(".seg-btn:not(:disabled)")];
  const i = btns.indexOf(document.activeElement);
  const next = e.key === "Home" ? 0 : e.key === "End" ? btns.length - 1
    : (i + (e.key === "ArrowRight" ? 1 : -1) + btns.length) % btns.length;
  btns[next]?.focus();
  e.preventDefault();
});

$("confirms").addEventListener("click", async () => { await send({ type: "showConfirm" }); window.close(); });
$("jev-options").addEventListener("click", () => { chrome.runtime.openOptionsPage?.(); window.close(); });
$("unshare-all").addEventListener("click", async () => { await send({ type: "unshareAll" }); msg("All tabs unshared."); await load(); });
$("pause").addEventListener("click", async () => { await send({ type: "pause", paused: !state.paused }); await load(); });
$("retry").addEventListener("click", async () => {
  $("retry").disabled = true;
  await send({ type: "reconnect" }).catch(() => {});
  setTimeout(async () => { $("retry").disabled = false; await load(); }, 600);
});
$("accept").addEventListener("click", async () => {
  const origins = (state.pending?.loosening || [])
    .filter((l) => l.kind === "read_allow_added" || l.kind === "act_allow_added")
    .map((l) => allowToPermission(l.value)).filter(Boolean);
  if (origins.length) { try { await chrome.permissions.request({ origins }); } catch { /* optional */ } }
  await send({ type: "acceptPolicy" });
  msg("Policy accepted.");
  await load();
});
$("reject").addEventListener("click", async () => { await send({ type: "rejectPolicy" }); msg("Policy change rejected."); await load(); });

// Keep the activity panel open/closed between popups (a UI preference only).
const AUDIT_KEY = "chromecli.auditOpen";
$("audit-wrap").open = localStorage.getItem(AUDIT_KEY) === "1";
$("audit-wrap").addEventListener("toggle", () => localStorage.setItem(AUDIT_KEY, $("audit-wrap").open ? "1" : "0"));

// The pause shortcut as Chrome has it (the user may have changed it in chrome://extensions/shortcuts).
chrome.commands?.getAll?.().then((cmds) => {
  const keys = shortcutKeys(cmds.find((c) => c.name === "pause-agents")?.shortcut);
  $("shortcut").replaceChildren(...keys.map((k) => el("kbd", {}, k)));
  $("shortcut").setAttribute("aria-label", keys.length ? `Shortcut ${keys.join(" ")}` : "No shortcut set");
}).catch(() => {});

load().then(() => send({ type: "reconnect" }).catch(() => {}));
