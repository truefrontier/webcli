// Injected with chrome.scripting.executeScript({func: pageSnapshot, world: "ISOLATED"}). Self-contained.
// Interactive and labelled elements for find / Jev (spec 7): {id (ref), tag, role, type, name, label, placeholder,
// autocomplete, valueKind ("empty"|"filled"; never the value), inForm, formAction (origin+path), visible, rect, ...}.
// Refs are stable per element for this document (shared with read --elements), so a ref from either keeps pointing at
// the same element.
export function pageSnapshot(opts) {
  try {
  const max = Math.min(Math.max(1, Number(opts && opts.max) || 300), 300);
  const clean = (s) => String(s || "").replace(/\s+/g, " ").trim();
  // Open shadow roots (web components): deep queries, deep parents, the flat (rendered) tree.
  const up = (n) => n.parentElement || (n.parentNode && n.parentNode.host) || null;
  const shadowOf = (n) => {
    if (n.shadowRoot) return n.shadowRoot;
    const d = globalThis.chrome && globalThis.chrome.dom;
    if (!d || typeof d.openOrClosedShadowRoot !== "function") return null;
    try { return d.openOrClosedShadowRoot(n) || null; } catch { return null; }
  };
  const deepAll = (sel) => {
    const out = [];
    const visit = (root) => {
      out.push(...root.querySelectorAll(sel));
      for (const n of root.querySelectorAll("*")) { const r = shadowOf(n); if (r) visit(r); }
    };
    visit(document);
    return out;
  };
  const closestDeep = (el, sel) => { for (let n = el; n; n = up(n)) if (n.nodeType === 1 && n.matches(sel)) return n; return null; };
  const byId = (el, id) => (el.getRootNode && el.getRootNode().getElementById ? el.getRootNode().getElementById(id) : null) || document.getElementById(id);
  // Rendered (flat-tree) text: slots show their assigned light-DOM nodes (a shadow <button><slot></slot></button>
  // has an empty innerText, but its name is the slotted text).
  const slotText = (n) => {
    const out = [];
    const walk = (x) => {
      if (x.nodeType === 3) { out.push(x.nodeValue); return; }
      if (x.nodeType !== 1 && x.nodeType !== 11) return;
      if (x.nodeType === 1) {
        if (x.tagName === "STYLE" || x.tagName === "SCRIPT") return;
        const cs = getComputedStyle(x);
        if (cs.display === "none" || cs.visibility === "hidden") return;
        if (x.tagName === "SLOT") { const a = x.assignedNodes({ flatten: true }); (a.length ? a : Array.from(x.childNodes)).forEach(walk); return; }
      }
      const sr = x.nodeType === 1 ? shadowOf(x) : null;
      for (const c of (sr ? sr.childNodes : x.childNodes)) walk(c);
    };
    walk(n);
    return out.join(" ");
  };
  const hostLabel = (el) => {
    const host = el.getRootNode && el.getRootNode().host;
    return host ? clean(host.getAttribute("label") || host.getAttribute("aria-label") || "") : "";
  };
  const hidden = (el) => {
    if (el.hidden || el.getAttribute("aria-hidden") === "true") return true;
    const cs = getComputedStyle(el);
    return cs.display === "none" || cs.visibility === "hidden";
  };
  const labelOf = (el) => (el.labels && el.labels.length ? clean(Array.from(el.labels).map((l) => l.innerText).join(" ")) : "");
  function accessibleName(el) {
    const a = el.getAttribute("aria-label");
    if (a) return clean(a);
    const by = el.getAttribute("aria-labelledby");
    if (by) { const t = clean(by.split(/\s+/).map((id) => byId(el, id)?.innerText || "").join(" ")); if (t) return t; }
    const l = labelOf(el);
    if (l) return l;
    const hl = hostLabel(el);
    if (hl) return hl;
    if (el.tagName === "INPUT") {
      const type = (el.getAttribute("type") || "").toLowerCase();
      if (["submit", "button", "reset"].includes(type)) return clean(el.getAttribute("value") || (type === "submit" ? "Submit" : ""));
    }
    if (["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName)) return clean(el.getAttribute("placeholder") || el.getAttribute("title") || el.getAttribute("name") || "");
    return clean(el.innerText) || clean(slotText(el)) || clean(el.getAttribute("title") || el.getAttribute("alt") || "");
  }
  function role(el) {
    const r = el.getAttribute("role");
    if (r) return r.toLowerCase();
    const t = el.tagName, type = (el.getAttribute("type") || "text").toLowerCase();
    if (t === "BUTTON" || (t === "INPUT" && ["submit", "button", "reset", "image"].includes(type)) || t === "SUMMARY") return "button";
    if (t === "A") return "link";
    if (t === "SELECT") return "combobox";
    if (t === "TEXTAREA" || (t === "INPUT" && !["checkbox", "radio"].includes(type))) return "textbox";
    if (t === "INPUT") return type;
    return null;
  }
  const refs = globalThis.__chromecliRefs instanceof Map ? globalThis.__chromecliRefs : new Map();
  const ids = globalThis.__chromecliIds instanceof WeakMap ? globalThis.__chromecliIds : new WeakMap();
  globalThis.__chromecliRefs = refs;
  globalThis.__chromecliIds = ids;
  globalThis.__chromecliNext = globalThis.__chromecliNext || refs.size;
  const refFor = (el) => {
    let r = ids.get(el);
    if (!r || refs.get(r) !== el) { r = `e${++globalThis.__chromecliNext}`; ids.set(el, r); refs.set(r, el); }
    return r;
  };
  const sel = "a[href], button, input:not([type=hidden]), select, textarea, summary, [contenteditable=''], [contenteditable=true], " +
    "[role=button], [role=link], [role=checkbox], [role=radio], [role=tab], [role=menuitem], [role=switch], [role=combobox], " +
    "[role=textbox], [role=option], [role=searchbox]";
  const out = [];
  for (const el of deepAll(sel)) {
    if (out.length >= max) break;
    if (hidden(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;
    const tag = el.tagName.toLowerCase();
    const item = { id: refFor(el), tag, role: role(el), name: accessibleName(el).slice(0, 120) };
    if (tag === "input") item.type = (el.getAttribute("type") || "text").toLowerCase();
    const label = labelOf(el); if (label) item.label = label.slice(0, 120);
    const ph = el.getAttribute("placeholder"); if (ph) item.placeholder = clean(ph).slice(0, 120);
    const ac = el.getAttribute("autocomplete"); if (ac) item.autocomplete = ac.slice(0, 60);
    if (["input", "textarea", "select"].includes(tag)) item.valueKind = el.value ? "filled" : "empty";
    const form = el.form || closestDeep(el, "form");
    item.inForm = !!form;
    if (form) { try { const u = new URL(form.action || location.href); item.formAction = u.origin + u.pathname; } catch { /* skip */ } }
    item.visible = r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth;
    item.rect = { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) };
    // for the heuristics only (stripped before anything goes to Jev)
    if (el.getAttribute("name")) item.nameAttr = el.getAttribute("name").slice(0, 60);
    if (el.id) item.idAttr = el.id.slice(0, 60);
    out.push(item);
  }
  return { ok: true, url: location.href, title: document.title, elements: out };
  } catch (e) {
    return { ok: false, code: "script_error", message: `In-page error: ${String(e && e.name || "Error")}: ${String(e && e.message || e).slice(0, 300)}` };
  }
}
