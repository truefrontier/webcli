// Injected with chrome.scripting.executeScript({func: extractPage, world: "ISOLATED"}). It must stay
// self-contained: Chrome serializes the function source, so nothing outside it is in scope.
// Reads only: text (innerText) or markdown built from the DOM, plus optional interactive elements.
// Never reads form values (inputs report valueKind "empty"|"filled" only).
export function extractPage(opts) {
  const as = opts && opts.as === "md" ? "md" : "text";
  const maxChars = Math.max(1, Number(opts && opts.maxChars) || 200000);
  const SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "SVG", "CANVAS", "IFRAME", "OBJECT", "EMBED"]);

  const hidden = (el) => {
    if (el.hidden || el.getAttribute("aria-hidden") === "true") return true;
    const cs = getComputedStyle(el);
    return cs.display === "none" || cs.visibility === "hidden";
  };
  const clean = (s) => (s || "").replace(/\s+/g, " ").trim();

  function md(node, ctx) {
    if (node.nodeType === Node.TEXT_NODE) return ctx.pre ? node.nodeValue : node.nodeValue.replace(/\s+/g, " ");
    if (node.nodeType !== Node.ELEMENT_NODE) return "";
    const el = node;
    if (SKIP.has(el.tagName) || hidden(el)) return "";
    const tag = el.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return "";
    const kids = (c = ctx) => Array.from(el.childNodes).map((n) => md(n, c)).join("");
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
      const t = by.split(/\s+/).map((id) => document.getElementById(id)?.innerText || "").join(" ");
      if (clean(t)) return clean(t);
    }
    if (el.labels && el.labels.length) return clean(Array.from(el.labels).map((l) => l.innerText).join(" "));
    if (["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName)) {
      return clean(el.getAttribute("placeholder") || el.getAttribute("title") || el.getAttribute("name") || "");
    }
    return clean(el.innerText || el.getAttribute("title") || el.getAttribute("alt") || "");
  }

  function elements() {
    const sel = "a[href], button, input:not([type=hidden]), select, textarea, summary, [contenteditable=''], " +
      "[contenteditable=true], [role=button], [role=link], [role=checkbox], [role=radio], [role=tab], " +
      "[role=menuitem], [role=switch], [role=combobox], [role=textbox], [role=option]";
    const refs = new Map();
    const out = [];
    let n = 0;
    for (const el of document.querySelectorAll(sel)) {
      if (out.length >= 300) break;
      if (hidden(el)) continue;
      const r = el.getBoundingClientRect();
      if (r.width === 0 && r.height === 0) continue;
      const ref = `e${++n}`;
      refs.set(ref, el);
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
      item.inForm = !!el.closest("form");
      item.disabled = !!el.disabled;
      item.rect = { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) };
      out.push(item);
    }
    // Refs live in this isolated world for this document only (later phases act on them).
    globalThis.__chromecliRefs = refs;
    return out;
  }

  let content = as === "md"
    ? md(document.body || document.documentElement, {}).replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim()
    : (document.body ? document.body.innerText : document.documentElement.innerText || "");
  const chars = content.length;
  const truncated = chars > maxChars;
  if (truncated) content = content.slice(0, maxChars);
  const result = { title: document.title, url: location.href, as, content, chars, truncated };
  if (opts && opts.elements) result.elements = elements();
  return result;
}
