// Injected with chrome.scripting.executeScript({func: pageSnapshot, world: "ISOLATED"}). Self-contained.
// Interactive and labelled elements for find / Jev (spec 7): {id (ref), tag, role, type, name, label, placeholder,
// autocomplete, valueKind ("empty"|"filled"; never the value), inForm, formAction (origin+path), visible, rect, ...}.
// Refs are stable per element for this document (shared with read --elements), so a ref from either keeps pointing at
// the same element.
export function pageSnapshot(opts) {
  const max = Math.min(Math.max(1, Number(opts && opts.max) || 300), 300);
  const clean = (s) => String(s || "").replace(/\s+/g, " ").trim();
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
    if (by) { const t = clean(by.split(/\s+/).map((id) => document.getElementById(id)?.innerText || "").join(" ")); if (t) return t; }
    const l = labelOf(el);
    if (l) return l;
    if (el.tagName === "INPUT") {
      const type = (el.getAttribute("type") || "").toLowerCase();
      if (["submit", "button", "reset"].includes(type)) return clean(el.getAttribute("value") || (type === "submit" ? "Submit" : ""));
    }
    if (["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName)) return clean(el.getAttribute("placeholder") || el.getAttribute("title") || el.getAttribute("name") || "");
    return clean(el.innerText || el.getAttribute("title") || el.getAttribute("alt") || "");
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
  for (const el of document.querySelectorAll(sel)) {
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
    const form = el.form || el.closest("form");
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
}
