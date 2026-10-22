// Injected with chrome.scripting.executeScript({func: pageScroll, world: "ISOLATED"}). It must stay self-contained:
// Chrome serializes the function source, so nothing outside it is in scope.
// 0.10.2: moves one scroll position (the window's, or an inner scroll container's, open shadow roots included) and
// reports it. It never changes the DOM, focus, styles or values. Viewers like signNow scroll an inner <div>, not the
// window, so with no container the target is auto-detected: the largest visible element whose content overflows
// (scrollHeight > clientHeight + 10) with overflow-y auto/scroll/overlay, else document.scrollingElement.
// spec (exactly one action; none = by page):
//   measure: true                      no movement, just the target and its position
//   to: "top" | "bottom" | {top, left?} absolute
//   by: "page" | N (px), dir: 1 | -1    relative; a page is clientHeight - overlap (default 40px)
//   into: CSS                          scroll that element into view (block start)
//   container: CSS | path: [[i...]...] | "document"   the target (path: from an earlier reply, child indexes per
//                                      shadow root); otherwise auto-detected
//   settleMs: 0..3000                  wait for render (two animation frames, then this, then a frame)
export async function pageScroll(spec) {
  try {
    const s = spec && typeof spec === "object" ? spec : {};
    const fail = (code, message) => ({ ok: false, code, message });
    const doc = document.scrollingElement || document.documentElement;
    const up = (n) => n.parentElement || (n.parentNode && n.parentNode.host) || null;
    const roots = () => {
      const out = [document];
      const visit = (root) => { for (const n of root.querySelectorAll("*")) if (n.shadowRoot) { out.push(n.shadowRoot); visit(n.shadowRoot); } };
      visit(document);
      return out;
    };
    const deepAll = (sel) => { const out = []; for (const r of roots()) out.push(...r.querySelectorAll(sel)); return out; };
    const vw = () => window.innerWidth || document.documentElement.clientWidth || 0;
    const vh = () => window.innerHeight || document.documentElement.clientHeight || 0;
    const shown = (el) => {
      const cs = getComputedStyle(el);
      if (cs.display === "none" || cs.visibility === "hidden") return false;
      const r = el.getBoundingClientRect();
      return r.width > 0 && r.height > 0;
    };
    const visibleArea = (el) => {
      const r = el.getBoundingClientRect();
      const w = Math.min(r.right, vw()) - Math.max(r.left, 0);
      const h = Math.min(r.bottom, vh()) - Math.max(r.top, 0);
      return w > 0 && h > 0 ? w * h : 0;
    };
    const overflowY = (el) => /^(auto|scroll|overlay)$/.test(getComputedStyle(el).overflowY);
    const scrollable = (el) => el.scrollHeight > el.clientHeight + 10 && overflowY(el);
    const first = (sel) => {
      let all;
      try { all = deepAll(sel); } catch (e) { return { err: fail("bad_target", `Not a valid CSS selector: ${String(sel).slice(0, 120)}`) }; }
      if (!all.length) return { el: null };
      return { el: all.find(shown) || all[0] };
    };
    const inShadow = (el) => !!(el.getRootNode && el.getRootNode() instanceof ShadowRoot);
    const describe = (el) => {
      if (el === doc) return "document (window scroll)";
      let d = el.localName;
      if (el.id) d += `#${el.id}`;
      const cls = typeof el.className === "string" ? el.className.trim().split(/\s+/).filter(Boolean).slice(0, 3) : [];
      if (cls.length) d += `.${cls.join(".")}`;
      const label = el.getAttribute("aria-label");
      if (label) d += `[aria-label="${label.slice(0, 40)}"]`;
      const host = inShadow(el) ? el.getRootNode().host : null;
      if (host) d += ` (in <${host.localName}${host.id ? `#${host.id}` : ""}> shadow root)`;
      return d.slice(0, 160);
    };
    // A path the next call can resolve to the same element: child indexes from the document, then from each shadow root.
    const pathOf = (el) => {
      if (el === doc) return "document";
      const segs = [];
      let n = el;
      while (n) {
        const seg = [];
        let x = n;
        while (x.parentNode && x.parentNode.nodeType === 1) { seg.unshift(Array.prototype.indexOf.call(x.parentNode.children, x)); x = x.parentNode; }
        const root = x.parentNode;
        if (!root) return null;
        seg.unshift(Array.prototype.indexOf.call(root.children, x));
        segs.unshift(seg);
        n = root.nodeType === 11 ? root.host : null;
      }
      return segs;
    };
    const resolvePath = (p) => {
      if (p === "document") return doc;
      if (!Array.isArray(p) || !p.length) return null;
      let root = document;
      let el = null;
      for (const seg of p) {
        if (el) { root = el.shadowRoot; if (!root) return null; }
        let n = root;
        for (const i of seg) { n = n && n.children ? n.children[i] : null; if (!n) return null; }
        el = n;
      }
      return el;
    };
    const autoTarget = () => {
      let best = null;
      let area = 0;
      for (const el of deepAll("*")) {
        if (el === doc || !scrollable(el) || !shown(el)) continue;
        const a = visibleArea(el);
        if (a > area) { best = el; area = a; }
      }
      // A small scroll box (a code block, a menu) doesn't beat a scrolling window: it must cover 30% of the viewport.
      const docScrolls = doc.scrollHeight > doc.clientHeight + 10;
      if (best && (!docScrolls || area >= 0.3 * vw() * vh())) return best;
      return doc;
    };
    const frame = () => new Promise((r) => { let done = false; const f = () => { if (!done) { done = true; r(); } }; requestAnimationFrame(f); setTimeout(f, 100); });
    const settle = async (ms) => { await frame(); await frame(); if (ms > 0) { await new Promise((r) => setTimeout(r, ms)); await frame(); } };
    const moveTo = (el, top, left) => {
      const opts = { top: Math.max(0, top), ...(left != null ? { left: Math.max(0, left) } : {}) };
      try { el.scrollTo({ ...opts, behavior: "instant" }); } catch { el.scrollTop = opts.top; if (left != null) el.scrollLeft = opts.left; }
    };

    let target = null;
    let auto = false;
    if (s.path != null) {
      target = resolvePath(s.path);
      if (!target) return fail("element_not_found", "The scroll target is gone (the page changed); start again.");
    } else if (s.container != null && s.container !== "") {
      const f = first(String(s.container));
      if (f.err) return f.err;
      if (!f.el) return fail("element_not_found", "No element matches --container.");
      target = f.el === document.documentElement || f.el === document.body ? (scrollable(f.el) ? f.el : doc) : f.el;
    }
    let intoEl = null;
    if (s.into != null && s.into !== "") {
      const f = first(String(s.into));
      if (f.err) return f.err;
      if (!f.el) return fail("element_not_found", "No element matches --into.");
      intoEl = f.el;
      if (!target) {
        for (let n = up(intoEl); n; n = up(n)) if (n !== doc && scrollable(n)) { target = n; break; }
        target = target || doc;
      }
    }
    if (!target) { target = autoTarget(); auto = true; }
    const before = target.scrollTop;
    const ch = target.clientHeight;
    if (s.measure) {
      // nothing moves
    } else if (intoEl) {
      try { intoEl.scrollIntoView({ block: "start", inline: "nearest", behavior: "instant" }); } catch { intoEl.scrollIntoView(true); }
    } else if (s.to != null) {
      if (s.to === "top") moveTo(target, 0);
      else if (s.to === "bottom") moveTo(target, target.scrollHeight);
      else if (typeof s.to === "object" && Number.isFinite(Number(s.to.top))) moveTo(target, Number(s.to.top), s.to.left != null ? Number(s.to.left) : null);
      else return fail("bad_request", "to must be top, bottom or {top}.");
    } else {
      const overlap = Math.min(Math.max(0, Number(s.overlap ?? 40) || 0), 400);
      const px = s.by == null || s.by === "page" ? Math.max(Math.round(ch * 0.4), ch - overlap) : Number(s.by);
      if (!Number.isFinite(px) || px <= 0) return fail("bad_request", "by must be page or a positive number of pixels.");
      moveTo(target, before + (s.dir === -1 ? -px : px));
    }
    await settle(Math.min(Math.max(0, Number(s.settleMs) || 0), 3000));
    const top = target.scrollTop;
    return { ok: true, url: location.href, title: document.title,
      target: { description: describe(target), kind: target === doc ? "document" : "element", auto, inShadow: target !== doc && inShadow(target),
        path: pathOf(target) },
      scrollTop: Math.round(top), scrollLeft: Math.round(target.scrollLeft), scrollHeight: target.scrollHeight,
      clientHeight: target.clientHeight, atTop: top <= 1, atEnd: top + target.clientHeight >= target.scrollHeight - 2,
      moved: Math.round(top - before), devicePixelRatio: window.devicePixelRatio || 1 };
  } catch (e) {
    return { ok: false, code: "script_error", message: `In-page error: ${String(e && (e.name ? `${e.name}: ${e.message}` : e)).slice(0, 300)}` };
  }
}
