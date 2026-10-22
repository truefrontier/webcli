// Injected with chrome.scripting.executeScript({func: extractPage, world: "ISOLATED"}). It must stay
// self-contained: Chrome serializes the function source, so nothing outside it is in scope.
// Reads only: text (innerText) or markdown built from the DOM, plus optional interactive elements.
// Never reads form values (inputs report valueKind "empty"|"filled" only).
export function extractPage(opts) {
  try {
  const as = opts && opts.as === "md" ? "md" : "text";
  const maxChars = Math.max(1, Number(opts && opts.maxChars) || 200000);
  const SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "SVG", "CANVAS", "IFRAME", "OBJECT", "EMBED"]);

  const hidden = (el) => {
    if (el.hidden || el.getAttribute("aria-hidden") === "true") return true;
    const cs = getComputedStyle(el);
    return cs.display === "none" || cs.visibility === "hidden";
  };
  const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
  // Open shadow roots (web components): deep queries, deep parents, the flat (rendered) tree.
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
  // Children as rendered: a shadow host shows its shadow tree; a <slot> shows what's assigned to it.
  const flatChildren = (el) => {
    if (el.shadowRoot) return Array.from(el.shadowRoot.childNodes);
    if (el.tagName === "SLOT" && el.assignedNodes) { const a = el.assignedNodes({ flatten: true }); if (a.length) return a; }
    return Array.from(el.childNodes);
  };
  const hasShadow = () => deepAll("*").some((n) => n.shadowRoot);
  const BLOCK = new Set(["P", "DIV", "SECTION", "ARTICLE", "MAIN", "HEADER", "FOOTER", "ASIDE", "NAV", "FORM", "LI", "UL", "OL",
    "H1", "H2", "H3", "H4", "H5", "H6", "TABLE", "TR", "PRE", "BLOCKQUOTE", "FIELDSET", "LABEL", "DL", "DT", "DD", "FIGURE"]);
  function flatText(node) {
    if (node.nodeType === Node.TEXT_NODE) return node.nodeValue.replace(/\s+/g, " ");
    if (node.nodeType !== Node.ELEMENT_NODE) return "";
    const el = node;
    if (SKIP.has(el.tagName) || hidden(el)) return "";
    if (el.tagName === "BR") return "\n";
    if (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT") return "";
    const inner = flatChildren(el).map(flatText).join("");
    const block = BLOCK.has(el.tagName) || /^(block|flex|grid|list-item|table)/.test(getComputedStyle(el).display);
    return block ? `\n${inner}\n` : inner;
  }

  function md(node, ctx) {
    if (node.nodeType === Node.TEXT_NODE) return ctx.pre ? node.nodeValue : node.nodeValue.replace(/\s+/g, " ");
    if (node.nodeType !== Node.ELEMENT_NODE) return "";
    const el = node;
    if (SKIP.has(el.tagName) || hidden(el)) return "";
    const tag = el.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return "";
    const kids = (c = ctx) => flatChildren(el).map((n) => md(n, c)).join("");
    switch (tag) {
      case "H1": case "H2": case "H3": case "H4": case "H5": case "H6":
        return `\n\n${"#".repeat(Number(tag[1]))} ${clean(kids())}\n\n`;
      case "P": case "SECTION": case "ARTICLE": case "MAIN": case "HEADER": case "FOOTER": case "ASIDE": case "NAV":
      case "DIV": case "FORM": case "FIGURE": case "DL":
        return `\n\n${kids()}\n\n`;
      case "BR": return "\n";
      case "HR": return "\n\n---\n\n";
      case "STRONG": case "B": { const t = clean(kids()); return t ? `**${t}**` : ""; }
      case "EM": case "I": { const t = clean(kids()); return t ? `*${t}*` : ""; }
      case "CODE": return ctx.pre ? kids() : "`" + clean(kids()) + "`";
      case "PRE": return "\n\n```\n" + kids({ ...ctx, pre: true }).replace(/\n+$/, "") + "\n```\n\n";
      case "BLOCKQUOTE": return "\n\n" + clean(kids()).split("\n").map((l) => "> " + l).join("\n") + "\n\n";
      case "A": {
        const t = clean(kids());
        const href = el.getAttribute("href");
        if (!t) return "";
        if (!href || href.startsWith("javascript:") || href.startsWith("#")) return t;
        let abs = href;
        try { abs = new URL(href, location.href).href; } catch { /* keep raw */ }
        return `[${t}](${abs})`;
      }
      case "IMG": { const alt = clean(el.getAttribute("alt")); return alt ? `![${alt}]` : ""; }
      case "UL": case "OL": {
        const depth = (ctx.depth || 0);
        const items = Array.from(el.children).filter((c) => c.tagName === "LI" && !hidden(c));
        return "\n" + items.map((li, i) => {
          const body = clean(Array.from(li.childNodes).map((n) => md(n, { ...ctx, depth: depth + 1 })).join(""));
          return `${"  ".repeat(depth)}${tag === "OL" ? `${i + 1}.` : "-"} ${body}`;
        }).join("\n") + "\n";
      }
      case "TABLE": {
        const rows = Array.from(el.querySelectorAll("tr")).slice(0, 200)
          .map((tr) => Array.from(tr.children).map((td) => clean(td.innerText).replace(/\|/g, "\\|")));
        if (!rows.length) return "";
        const width = Math.max(...rows.map((r) => r.length));
        const line = (r) => "| " + Array.from({ length: width }, (_, i) => r[i] || "").join(" | ") + " |";
        return "\n\n" + [line(rows[0]), "|" + "---|".repeat(width), ...rows.slice(1).map(line)].join("\n") + "\n\n";
      }
      default: return kids();
    }
  }

  function accessibleName(el) {
    const label = el.getAttribute("aria-label");
    if (label) return clean(label);
    const by = el.getAttribute("aria-labelledby");
    if (by) {
      const t = by.split(/\s+/).map((id) => byId(el, id)?.innerText || "").join(" ");
      if (clean(t)) return clean(t);
    }
    if (el.labels && el.labels.length) return clean(Array.from(el.labels).map((l) => l.innerText).join(" "));
    const hl = hostLabel(el);
    if (hl) return hl;
    if (["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName)) {
      return clean(el.getAttribute("placeholder") || el.getAttribute("title") || el.getAttribute("name") || "");
    }
    return clean(el.innerText) || clean(slotText(el)) || clean(el.getAttribute("title") || el.getAttribute("alt") || "");
  }

  function elements() {
    const sel = "a[href], button, input:not([type=hidden]), select, textarea, summary, [contenteditable=''], " +
      "[contenteditable=true], [role=button], [role=link], [role=checkbox], [role=radio], [role=tab], " +
      "[role=menuitem], [role=switch], [role=combobox], [role=textbox], [role=option]";
    // Stable refs per element for this document, shared with page/snapshot.js (find): a ref from either keeps
    // pointing at the same element.
    const refs = globalThis.__chromecliRefs instanceof Map ? globalThis.__chromecliRefs : new Map();
    const ids = globalThis.__chromecliIds instanceof WeakMap ? globalThis.__chromecliIds : new WeakMap();
    globalThis.__chromecliRefs = refs;
    globalThis.__chromecliIds = ids;
    globalThis.__chromecliNext = globalThis.__chromecliNext || refs.size;
    const out = [];
    for (const el of deepAll(sel)) {
      if (out.length >= 300) break;
      if (hidden(el)) continue;
      const r = el.getBoundingClientRect();
      if (r.width === 0 && r.height === 0) continue;
      let ref = ids.get(el);
      if (!ref || refs.get(ref) !== el) { ref = `e${++globalThis.__chromecliNext}`; ids.set(el, ref); refs.set(ref, el); }
      const tag = el.tagName.toLowerCase();
      const item = { ref, tag, role: el.getAttribute("role") || null, name: accessibleName(el).slice(0, 120) };
      if (tag === "input") item.type = (el.getAttribute("type") || "text").toLowerCase();
      if (["input", "textarea", "select"].includes(tag)) {
        item.valueKind = el.value ? "filled" : "empty";
        if (el.getAttribute("autocomplete")) item.autocomplete = el.getAttribute("autocomplete");
      }
      if (tag === "a") {
        try { const u = new URL(el.href); item.href = u.origin + u.pathname; } catch { /* skip */ }
      }
      item.inForm = !!closestDeep(el, "form");
      if (el.getRootNode() !== document) item.inShadow = true;
      item.disabled = !!el.disabled;
      item.rect = { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) };
      out.push(item);
    }
    // Refs live in this isolated world for this document only; page actions resolve them (--ref).
    return out;
  }

  let content = as === "md"
    ? md(document.body || document.documentElement, {}).replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim()
    : !document.body ? (document.documentElement.innerText || "")
    : hasShadow() ? flatText(document.body).replace(/[ \t]+/g, " ").replace(/ *\n */g, "\n").replace(/\n{3,}/g, "\n\n").trim()
    : document.body.innerText;
  const chars = content.length;
  const truncated = chars > maxChars;
  if (truncated) content = content.slice(0, maxChars);
  const result = { title: document.title, url: location.href, as, content, chars, truncated };
  if (opts && opts.elements) result.elements = elements();
  return result;
  } catch (e) {
    return { ok: false, code: "script_error", message: `In-page error: ${String(e && e.name || "Error")}: ${String(e && e.message || e).slice(0, 300)}` };
  }
}
