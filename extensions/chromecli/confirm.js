import { countdown, detailRows, headline, hostOf, opIcon, reasonItems } from "./confirm-view.js";

const $ = (id) => document.getElementById(id);
const id = new URLSearchParams(location.search).get("id") || "";
let info = null;
let expiresAt = 0;
let total = 60000;
let answered = false;

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}
function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "ic"); svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`); svg.append(use);
  return svg;
}

async function answer(a) {
  if (answered) return;
  answered = true;
  $("allow").disabled = true; $("deny").disabled = true;
  try { await chrome.runtime.sendMessage({ type: "confirmAnswer", id, answer: a }); } catch { /* window closes anyway */ }
  window.close();
}

function render() {
  $("headline").textContent = headline(info);
  document.title = `chromecli · ${headline(info)}?`;
  $("site").replaceChildren(el("span", "site-host", hostOf(info.origin)), ...(info.title ? [el("span", "site-sep", "·"), el("span", "site-title", info.title)] : []));
  $("site").title = info.url || "";
  $("op-use").setAttribute("href", `#i-${opIcon(info.op)}`);
  $("fav").dataset.op = info.op;

  $("reasons").replaceChildren(...reasonItems(info.reasons).map((r) => {
    const li = el("li", "reason");
    li.append(el("span", "reason-label", r.label), el("code", "reason-code", r.code));
    return li;
  }));

  $("details").replaceChildren(...detailRows(info).flatMap(([k, v, kind]) => {
    const dd = el("dd", kind || "", null);
    if (kind === "masked") dd.append(icon("lock"));
    dd.append(document.createTextNode(v));
    return [el("dt", "", k), dd];
  }));
  const html = info.element?.outerHTML;
  $("html-wrap").hidden = !html;
  $("html").textContent = html || "";
}

function tick() {
  if (answered) return;
  const c = countdown(expiresAt, Date.now());
  $("timer-label").textContent = c.label;
  $("timer").classList.toggle("urgent", c.urgent);
  $("bar-fill").style.transform = `scaleX(${Math.max(0, Math.min(1, (expiresAt - Date.now()) / total))})`;
  if (c.seconds <= 0) { window.close(); return; }
  setTimeout(tick, 250);
}

async function load() {
  let res;
  try { res = await chrome.runtime.sendMessage({ type: "confirmGet", id }); } catch (e) { res = { ok: false, error: String(e?.message || e) }; }
  if (!res?.ok) {
    $("headline").textContent = "Nothing to confirm";
    $("error").textContent = res?.error || "This request is no longer waiting.";
    $("error").hidden = false;
    $("allow").hidden = true;
    $("deny").textContent = "Close";
    $("deny").onclick = () => window.close();
    return;
  }
  info = res.info;
  expiresAt = res.expiresAt;
  total = Math.max(1000, expiresAt - Date.now());
  $("queue").textContent = res.queued ? `${res.queued} more waiting` : "";
  render();
  tick();
  $("deny").focus();
  // A short delay before Allow works, so a click or Enter meant for another window can't approve this one.
  setTimeout(() => { if (!answered) $("allow").disabled = false; }, 800);
}

$("deny").addEventListener("click", () => answer("deny"));
$("allow").addEventListener("click", () => answer("allow"));
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.preventDefault(); answer("deny"); } });
load();
