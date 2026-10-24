import { CONFIRM_ARM_MS, answerHow, confirmInput, countdown, detailRows, headline, hostOf, opIcon, reasonItems }
  from "./confirm-view.js";

const $ = (id) => document.getElementById(id);
const id = new URLSearchParams(location.search).get("id") || "";
let info = null;
let expiresAt = 0;
let total = 60000;
let answered = false;
// 0.16.2: keys and clicks in the first CONFIRM_ARM_MS are ignored (counted for the host log).
const openedAt = performance.now();
const since = () => performance.now() - openedAt;
const ignored = { keys: 0, clicks: 0 };
let loaded = false;

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

async function answer(a, via) {
  if (answered) return;
  answered = true;
  $("allow").disabled = true; $("deny").disabled = true;
  const how = answerHow(via, since(), ignored);
  try { await chrome.runtime.sendMessage({ type: "confirmAnswer", id, answer: a, how }); } catch { /* window closes anyway */ }
  window.close();
}

function render() {
  // 0.12.1: a recording request says Start, not Allow once, and has no gate reasons.
  const rec = info.op === "record";
  $("allow").textContent = rec ? "Start recording" : "Allow once";
  document.querySelector(".eyebrow").textContent = rec ? "An agent asks you to" : "An agent wants to";
  document.querySelector(".reasons-card").style.display = rec ? "none" : "";
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
    $("deny").disabled = false;
    $("deny").onclick = () => window.close();
    return;
  }
  info = res.info;
  expiresAt = res.expiresAt;
  total = Math.max(1000, expiresAt - Date.now());
  $("queue").textContent = res.queued ? `${res.queued} more waiting` : "";
  render();
  tick();
  loaded = true;
  // 0.16.2: nothing has focus (a keystroke meant for another app must not land on a button), and both buttons stay
  // disabled until the window has been up CONFIRM_ARM_MS.
  document.activeElement?.blur?.();
  setTimeout(arm, Math.max(0, CONFIRM_ARM_MS - since()));
}

function arm() {
  if (answered || !loaded) return;
  $("deny").disabled = false;
  $("allow").disabled = false;
  document.body.classList.add("armed");
}

function onButton(button, e) {
  const d = confirmInput({ kind: "click", button, detail: e.detail, sinceMs: since() });
  if (d === "ignore") { if (e.detail > 0) ignored.clicks++; return; }
  answer(d, "click");
}
$("deny").addEventListener("click", (e) => { if (loaded) onButton("deny", e); });
$("allow").addEventListener("click", (e) => onButton("allow", e));
// Capture phase, before any button sees it: keys never pick a button; only Escape (after the first second) denies.
window.addEventListener("keydown", (e) => {
  const d = confirmInput({ kind: "key", key: e.key, sinceMs: since() });
  if (since() < CONFIRM_ARM_MS) ignored.keys++;
  if (e.key === "Enter" || e.key === " " || e.key === "Spacebar" || d === "deny" || since() < CONFIRM_ARM_MS) {
    e.preventDefault(); e.stopPropagation();
  }
  if (d === "deny") answer("deny", "escape");
}, true);
// Clicks on the still-disabled buttons never fire "click"; count them (and swallow any early click) here.
window.addEventListener("pointerdown", (e) => {
  if (since() < CONFIRM_ARM_MS) { ignored.clicks++; e.preventDefault(); e.stopPropagation(); }
}, true);
load();
