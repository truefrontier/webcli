import { allowToPermission, permissionPattern } from "./policy.js";

const $ = (id) => document.getElementById(id);
let tab = null;
let state = null;

const send = (msg) => chrome.runtime.sendMessage(msg);

async function load() {
  const [t] = await chrome.tabs.query({ active: true, currentWindow: true });
  state = await send({ type: "state", tabId: t ? t.id : null });
  tab = state.tab;
  render();
}

function render() {
  $("version").textContent = `v${state.version}`;
  const h = state.host;
  $("host").innerHTML = "";
  $("host").append(h.connected ? "Host connected" : `Host not connected${h.lastError ? `: ${h.lastError}` : ""}`);
  $("host").className = h.connected ? "ok" : "err";
  $("title").textContent = tab ? tab.title || "(untitled)" : "No tab";
  $("origin").textContent = tab ? tab.origin || tab.url : "";
  $("denied").textContent = state.deniedReason ? `Can't be shared: ${state.deniedReason}` : "";
  const shareable = !!(tab && tab.origin && !state.deniedReason);
  const level = state.share?.level || null;
  $("share-read").disabled = !shareable;
  $("share-act").disabled = !shareable;
  $("unshare").disabled = !level;
  $("share-read").classList.toggle("on", level === "read");
  $("share-act").classList.toggle("on", level === "act");
  $("pause").textContent = state.paused ? "Resume agents" : "Pause agents";
  $("unshare-all").disabled = !state.shareCount;
  $("pending").hidden = !state.pending;
  $("pending-list").replaceChildren(...(state.pending?.loosening || []).map((l) => {
    const li = document.createElement("li");
    li.textContent = `${l.kind.replace(/_/g, " ")}: ${l.value}`;
    return li;
  }));
  $("audit").replaceChildren(...state.audit.map((a) => {
    const li = document.createElement("li");
    li.textContent = `${a.at.slice(11, 19)} ${a.op}${a.tab != null ? ` tab ${a.tab}` : ""}${a.origin ? ` ${a.origin}` : ""} → ${a.outcome}`;
    return li;
  }));
}

function msg(text, err = false) {
  $("msg").textContent = text;
  $("msg").className = err ? "err" : "muted";
}

async function share(level) {
  // permissions.request must run straight from the click (user gesture): nothing is awaited before it.
  const granted = chrome.permissions.request({ origins: [permissionPattern(tab.origin)] });
  if (!(await granted)) { msg("Chrome didn't grant access to this site; not shared.", true); return; }
  const res = await send({ type: "share", tabId: tab.id, origin: tab.origin, level });
  if (!res.ok) msg(res.error, true); else msg(level === "act" ? "Shared (act)." : "Shared (read).");
  await load();
}

$("share-read").addEventListener("click", () => share("read"));
$("share-act").addEventListener("click", () => share("act"));
$("unshare").addEventListener("click", async () => { await send({ type: "unshare", tabId: tab.id }); msg("Unshared."); await load(); });
$("unshare-all").addEventListener("click", async () => { await send({ type: "unshareAll" }); msg("All tabs unshared."); await load(); });
$("pause").addEventListener("click", async () => { await send({ type: "pause", paused: !state.paused }); await load(); });
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

load().then(() => send({ type: "reconnect" }).catch(() => {}));
