// Injected with chrome.scripting.executeScript({func: pageAction, world: "ISOLATED"}). Self-contained: Chrome
// serializes the function source, so nothing outside it is in scope.
//
// spec = { mode: "describe" | "do", op: "click"|"type"|"fill"|"select"|"press", target, value?, key?, fingerprint? }
// target = {selector} | {ref} | {text, role?} | null (press: the focused element)
// Returns { ok: true, element, fingerprint, ...result } or { ok: false, code, message, candidates? }.
// It never returns a field's value: read-back reports only whether the value matches.
export function pageAction(spec) {
  const clean = (s) => String(s || "").replace(/\s+/g, " ").trim();
  const fail = (code, message, extra = {}) => ({ ok: false, code, message, ...extra });
  try {
  // Open shadow roots (web components like Shopify Polaris): deep queries, deep parents, deep text.
  const up = (n) => n.parentElement || (n.parentNode && n.parentNode.host) || null;
  const deepAll = (sel) => {
    const out = [];
    const visit = (root) => {
      out.push(...root.querySelectorAll(sel));
      for (const n of root.querySelectorAll("*")) if (n.shadowRoot) visit(n.shadowRoot);
    };
    visit(document);
    return out;
  };
  const closestDeep = (el, sel) => { for (let n = el; n; n = up(n)) if (n.nodeType === 1 && n.matches(sel)) return n; return null; };
  const byId = (el, id) => (el.getRootNode && el.getRootNode().getElementById ? el.getRootNode().getElementById(id) : null) || document.getElementById(id);
  const deepText = (n) => {
    const t = [n.innerText || ""];
    const walk = (x) => { if (x.shadowRoot) for (const c of x.shadowRoot.children) { t.push(c.innerText || ""); walk(c); } for (const c of x.children || []) walk(c); };
    walk(n);
    return t.join(" ");
  };
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
      for (const c of (x.shadowRoot ? x.shadowRoot.childNodes : x.childNodes)) walk(c);
    };
    walk(n);
    return out.join(" ");
  };
  const hostLabel = (el) => {
    const host = el.getRootNode && el.getRootNode().host;
    return host ? clean(host.getAttribute("label") || host.getAttribute("aria-label") || "") : "";
  };

  const hidden = (el) => {
    for (let n = el; n && n.nodeType === 1; n = up(n)) {
      if (n.hidden || n.getAttribute("aria-hidden") === "true") return true;
      const cs = getComputedStyle(n);
      if (cs.display === "none" || cs.visibility === "hidden") return true;
    }
    const r = el.getBoundingClientRect();
    return r.width === 0 && r.height === 0;
  };

  function labelOf(el) {
    if (el.labels && el.labels.length) return clean(Array.from(el.labels).map((l) => l.innerText).join(" "));
    return "";
  }
  function accessibleName(el) {
    const label = el.getAttribute("aria-label");
    if (label) return clean(label);
    const by = el.getAttribute("aria-labelledby");
    if (by) {
      const t = by.split(/\s+/).map((id) => byId(el, id)?.innerText || "").join(" ");
      if (clean(t)) return clean(t);
    }
    const l = labelOf(el);
    if (l) return l;
    const hl = hostLabel(el);
    if (hl) return hl;
    const tag = el.tagName;
    if (tag === "INPUT") {
      const type = (el.getAttribute("type") || "").toLowerCase();
      if (["submit", "button", "reset"].includes(type)) return clean(el.getAttribute("value") || (type === "submit" ? "Submit" : ""));
      if (type === "image") return clean(el.getAttribute("alt") || el.getAttribute("value") || "");
    }
    if (["INPUT", "TEXTAREA", "SELECT"].includes(tag)) {
      return clean(el.getAttribute("placeholder") || el.getAttribute("title") || el.getAttribute("name") || "");
    }
    return clean(el.innerText) || clean(slotText(el)) || clean(el.getAttribute("title") || el.getAttribute("alt") || "");
  }
  function implicitRole(el) {
    const explicit = el.getAttribute("role");
    if (explicit) return explicit.toLowerCase();
    const tag = el.tagName;
    const type = (el.getAttribute("type") || "text").toLowerCase();
    if (tag === "BUTTON" || (tag === "INPUT" && ["submit", "button", "reset", "image"].includes(type))) return "button";
    if (tag === "A" && el.hasAttribute("href")) return "link";
    if (tag === "SELECT") return "combobox";
    if (tag === "TEXTAREA") return "textbox";
    if (tag === "INPUT" && type === "checkbox") return "checkbox";
    if (tag === "INPUT" && type === "radio") return "radio";
    if (tag === "INPUT") return "textbox";
    if (tag === "SUMMARY") return "button";
    if (el.isContentEditable) return "textbox";
    return null;
  }
  function snippet(el) {
    // 200 chars of outerHTML, with value attributes of fields blanked.
    let html = el.outerHTML || "";
    if (["INPUT", "TEXTAREA"].includes(el.tagName)) html = html.replace(/\svalue=("[^"]*"|'[^']*'|[^\s>]*)/gi, ' value="…"');
    html = html.replace(/\s+/g, " ");
    return html.length > 200 ? html.slice(0, 200) + "…" : html;
  }
  function describe(el, ref) {
    const tag = el.tagName.toLowerCase();
    const typeAttr = (el.getAttribute("type") || "").toLowerCase();
    const d = {
      ref: ref || null, tag, role: implicitRole(el), name: accessibleName(el).slice(0, 120),
      type: tag === "input" ? (typeAttr || "text") : typeAttr,
      id: el.id || null, nameAttr: el.getAttribute("name") || null, label: labelOf(el).slice(0, 120) || null,
      title: el.getAttribute("title") || null, autocomplete: el.getAttribute("autocomplete") || null,
      inForm: !!(el.form || closestDeep(el, "form")),
      defaultSubmit: tag === "button" && !!(el.form || closestDeep(el, "form")) && el.type === "submit",
      isField: ["input", "textarea", "select"].includes(tag) || el.isContentEditable,
      disabled: !!el.disabled, outerHTML: snippet(el),
    };
    // A submit input's value is its label (not user data); other fields' values are never returned.
    if (tag === "input" && ["submit", "button", "reset", "image"].includes(d.type)) d.value = el.getAttribute("value") || null;
    if (tag === "a") { try { const u = new URL(el.href); d.href = u.origin + u.pathname; } catch { /* skip */ } }
    if (tag === "form" || d.inForm) {
      const f = el.form || closestDeep(el, "form");
      try { const u = new URL(f.action || location.href); d.formAction = u.origin + u.pathname; } catch { /* skip */ }
    }
    return d;
  }
  const fingerprint = (d) => [d.tag, d.role, d.name, d.type, d.id, d.nameAttr, d.outerHTML].join("\u0001");

  const INTERACTIVE = "a[href], button, input:not([type=hidden]), select, textarea, summary, [contenteditable=''], " +
    "[contenteditable=true], [role=button], [role=link], [role=checkbox], [role=radio], [role=tab], [role=menuitem], " +
    "[role=switch], [role=combobox], [role=textbox], [role=option]";

  function retarget(el) {
    if (!el.shadowRoot || el.matches(INTERACTIVE)) return el;
    const inner = Array.from(el.shadowRoot.querySelectorAll(INTERACTIVE)).filter((x) => !hidden(x));
    return inner.length === 1 ? inner[0] : el;
  }
  function resolve(target) {
    if (!target) {
      const el = document.activeElement && document.activeElement !== document.body ? document.activeElement : null;
      return el ? { el } : { el: null, focused: true };
    }
    if (target.ref) {
      const refs = globalThis.__chromecliRefs;
      const el = refs && refs.get(String(target.ref));
      if (!el || !el.isConnected) {
        return { error: fail("element_not_found", `Ref ${target.ref} is gone (refs die on navigation or re-render). Run read --elements again.`) };
      }
      return { el, ref: String(target.ref) };
    }
    let found;
    if (target.selector) {
      try { found = deepAll(target.selector); } catch (e) {
        return { error: fail("bad_target", `Invalid CSS selector: ${String(e.message || e).slice(0, 200)}`) };
      }
      // A selector naming a web-component host (e.g. <s-button id="save">) means its one inner control.
      found = found.map(retarget).filter((el) => !hidden(el));
    } else if (target.text != null) {
      const want = clean(target.text).toLowerCase();
      const role = target.role ? String(target.role).toLowerCase() : null;
      const pool = deepAll(INTERACTIVE).filter((el) => !hidden(el) && (!role || implicitRole(el) === role));
      const exact = pool.filter((el) => accessibleName(el).toLowerCase() === want);
      found = exact.length ? exact : pool.filter((el) => accessibleName(el).toLowerCase().includes(want));
    } else {
      return { error: fail("bad_target", "Give exactly one target: selector, ref, or text.") };
    }
    if (!found.length) return { error: fail("element_not_found", "No visible element matches the target.") };
    if (found.length > 1) {
      return { error: fail("ambiguous_target", `${found.length} visible elements match; narrow the target.`, {
        candidates: found.slice(0, 10).map((el) => { const d = describe(el); return { tag: d.tag, role: d.role, name: d.name, id: d.id }; }) }) };
    }
    return { el: found[0] };
  }

  const r = resolve(spec.target);
  if (r.error) return r.error;
  const el = r.el;
  if (!el && spec.op !== "press") return fail("element_not_found", "No element.");
  const element = el ? describe(el, r.ref) : null;
  const fp = element ? fingerprint(element) : "focused:none";
  if (el) {
    if (hidden(el)) return fail("element_not_found", "The element isn't visible.", { element });
    if (el.disabled) return fail("element_not_found", "The element is disabled.", { element });
  }
  if (spec.op === "select" && el && el.tagName !== "SELECT") return fail("bad_target", "select needs a <select> element.", { element });
  if ((spec.op === "fill" || spec.op === "type") && el && !(["INPUT", "TEXTAREA"].includes(el.tagName) || el.isContentEditable)) {
    return fail("bad_target", `${spec.op} needs an input, textarea, or contenteditable element.`, { element });
  }
  if (spec.mode === "inspect") {
    // Pre-approval matching (read-only): is the element the one a grant's selector names, and what amounts are near it?
    const selectorHits = {};
    for (const sel of spec.selectors || []) { try { selectorHits[sel] = el ? deepAll(sel).map(retarget).includes(el) : false; } catch { selectorHits[sel] = false; } }
    const amountTexts = {};
    for (const sel of spec.amountSelectors || []) {
      try { amountTexts[sel] = deepAll(sel).slice(0, 5).map((n) => clean(deepText(n)).slice(0, 300)); } catch { amountTexts[sel] = []; }
    }
    if (spec.autoAmount && el) {
      const texts = [];
      for (let n = el, i = 0; n && i < 5; n = up(n), i++) texts.push(clean(deepText(n)).slice(0, 600));
      const totalRe = /\b(order total|total|amount due|due today|you pay)\b/i;
      for (const n of deepAll("*")) {
        if (texts.length > 20) break;
        const own = Array.from(n.childNodes).filter((c) => c.nodeType === 3).map((c) => c.nodeValue).join(" ");
        if (totalRe.test(own) && !hidden(n)) texts.push(clean(deepText(up(n) || n)).slice(0, 300));
      }
      amountTexts._auto = texts;
    }
    return { ok: true, element, fingerprint: fp, selectorHits, amountTexts, url: location.href };
  }
  if (spec.mode !== "do") return { ok: true, element, fingerprint: fp, url: location.href };
  if (spec.fingerprint && spec.fingerprint !== fp) {
    return fail("element_not_found", "The element changed while waiting for your OK; nothing was done.", { element });
  }

  const fire = (node, type, init = {}) => node.dispatchEvent(new (init.key != null ? KeyboardEvent
    : type.startsWith("pointer") ? (globalThis.PointerEvent || MouseEvent) : type.startsWith("mouse") || type === "click" ? MouseEvent : Event)(
    type, { bubbles: true, cancelable: true, composed: true, ...init }));
  const setValue = (node, v) => {
    const proto = node.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype
      : node.tagName === "SELECT" ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    if (setter) setter.call(node, v); else node.value = v;
  };
  const readValue = (node) => (node.isContentEditable && !("value" in node) ? node.innerText : node.value);

  try { el?.scrollIntoView({ block: "center", inline: "nearest" }); } catch { /* ignore */ }
  const value = spec.value == null ? "" : String(spec.value);
  switch (spec.op) {
    case "click": {
      el.focus?.();
      for (const t of ["pointerdown", "mousedown", "pointerup", "mouseup"]) fire(el, t, { button: 0 });
      el.click();
      return { ok: true, element, dispatched: true, url: location.href };
    }
    case "fill": {
      el.focus?.();
      if (el.isContentEditable && !("value" in el)) el.innerText = value; else setValue(el, value);
      fire(el, "input"); fire(el, "change");
      return { ok: true, element, dispatched: true, readBack: readValue(el) === value, url: location.href };
    }
    case "type": {
      el.focus?.();
      const before = readValue(el) || "";
      for (const ch of value) {
        fire(el, "keydown", { key: ch });
        fire(el, "keypress", { key: ch });
        if (el.isContentEditable && !("value" in el)) el.innerText = (el.innerText || "") + ch;
        else setValue(el, (el.value || "") + ch);
        el.dispatchEvent(new InputEvent("input", { bubbles: true, data: ch, inputType: "insertText" }));
        fire(el, "keyup", { key: ch });
      }
      fire(el, "change");
      return { ok: true, element, dispatched: true, readBack: readValue(el) === before + value, url: location.href };
    }
    case "select": {
      const opts = Array.from(el.options);
      const opt = opts.find((o) => o.value === value) || opts.find((o) => clean(o.text) === clean(value))
        || opts.find((o) => clean(o.text).toLowerCase() === clean(value).toLowerCase());
      if (!opt) return fail("element_not_found", "No option with that value or label.", { element, options: opts.slice(0, 30).map((o) => ({ value: o.value, label: clean(o.text) })) });
      el.focus?.();
      setValue(el, opt.value);
      fire(el, "input"); fire(el, "change");
      return { ok: true, element, dispatched: true, readBack: el.value === opt.value, selected: { value: opt.value, label: clean(opt.text) }, url: location.href };
    }
    case "press": {
      const node = el || document.activeElement || document.body;
      const key = String(spec.key || "");
      const code = key.length === 1 ? (/[a-z]/i.test(key) ? `Key${key.toUpperCase()}` : /\d/.test(key) ? `Digit${key}` : key === " " ? "Space" : "") : key;
      const go = fire(node, "keydown", { key, code });
      if (key.length === 1) fire(node, "keypress", { key, code });
      fire(node, "keyup", { key, code });
      let effect = null;
      // Synthetic key events don't trigger default actions; do the common ones explicitly.
      if (go && key === "Enter") {
        const form = node.form || closestDeep(node, "form");
        const tag = node.tagName;
        if (["INPUT"].includes(tag) && form) { form.requestSubmit ? form.requestSubmit() : form.submit(); effect = "submitted_form"; }
        else if (["BUTTON", "A", "SUMMARY"].includes(tag) || node.getAttribute?.("role") === "button") { node.click(); effect = "activated"; }
      } else if (go && (key === " " || key === "Space") && (node.tagName === "BUTTON" || node.getAttribute?.("role") === "button")) {
        node.click(); effect = "activated";
      }
      return { ok: true, element, dispatched: true, effect, url: location.href };
    }
  }
  return fail("bad_request", `Unknown page op ${spec.op}.`);
  } catch (e) {
    return fail("script_error", `In-page error: ${String(e && e.name || "Error")}: ${String(e && e.message || e).slice(0, 300)}`);
  }
}

// Wait helper (read-only): does the page have a visible match for selector / text right now?
export function pageProbe(spec) {
  try {
  const clean = (s) => String(s || "").replace(/\s+/g, " ").trim();
  // Open shadow roots (web components like Shopify Polaris): deep queries, deep parents, deep text.
  const up = (n) => n.parentElement || (n.parentNode && n.parentNode.host) || null;
  const deepAll = (sel) => {
    const out = [];
    const visit = (root) => {
      out.push(...root.querySelectorAll(sel));
      for (const n of root.querySelectorAll("*")) if (n.shadowRoot) visit(n.shadowRoot);
    };
    visit(document);
    return out;
  };
  const closestDeep = (el, sel) => { for (let n = el; n; n = up(n)) if (n.nodeType === 1 && n.matches(sel)) return n; return null; };
  const byId = (el, id) => (el.getRootNode && el.getRootNode().getElementById ? el.getRootNode().getElementById(id) : null) || document.getElementById(id);
  const deepText = (n) => {
    const t = [n.innerText || ""];
    const walk = (x) => { if (x.shadowRoot) for (const c of x.shadowRoot.children) { t.push(c.innerText || ""); walk(c); } for (const c of x.children || []) walk(c); };
    walk(n);
    return t.join(" ");
  };
  const hostLabel = (el) => {
    const host = el.getRootNode && el.getRootNode().host;
    return host ? clean(host.getAttribute("label") || host.getAttribute("aria-label") || "") : "";
  };
  if (spec.selector) {
    let els;
    try { els = deepAll(spec.selector); } catch (e) { return { ok: false, code: "bad_target", message: String(e.message || e).slice(0, 200) }; }
    const vis = els.filter((el) => { const r = el.getBoundingClientRect(); const cs = getComputedStyle(el); return (r.width || r.height) && cs.visibility !== "hidden" && cs.display !== "none"; });
    return { ok: true, found: vis.length > 0, count: vis.length, url: location.href };
  }
  const text = String(spec.text || "");
  const body = document.body ? deepText(document.body) : "";
  return { ok: true, found: body.includes(text), url: location.href };
  } catch (e) {
    return { ok: false, code: "script_error", message: `In-page error: ${String(e && e.message || e).slice(0, 300)}` };
  }
}
