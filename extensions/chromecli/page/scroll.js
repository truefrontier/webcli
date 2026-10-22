// Injected with chrome.scripting.executeScript({func: pageScroll, world: "ISOLATED"}), once per frame it reads
// (0.11.1: the service worker picks the frame). It must stay self-contained: Chrome serializes the function source,
// so nothing outside it is in scope.
// It moves one scroll position (the window's, or an inner scroll container's, open and closed shadow roots included:
// closed ones through chrome.dom.openOrClosedShadowRoot, which extension scripts may use) and reports it. It never
// changes the DOM, focus, styles or values. Viewers like signNow scroll an inner <div> (or a whole iframe), not the
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
// Survey modes for the service worker (nothing moves):
//   survey: true     {viewport, doc {scrollHeight, clientHeight, scrolls}, best {path, area, ...} | null, diag}
//   locate: CSS      {matches, shown, scrollable [{path, description, area, scrollHeight, clientHeight}], first}
//   locateInto: CSS  {matches, shown}
// Every reply has diag {viewport, iframes [{frameId, url, origin, rect}], regions [{kind: canvas|plugin|
// closed_shadow_host, ...}], closedShadowRoots}. Errors: {ok: false, code, reason, message, ...}; reasons no_match,
// multiple_matches (count, candidates), not_scrollable (scrollHeight, clientHeight), bad_selector, target_gone,
// page_exception.
export async function pageScroll(spec) {
  try {
    const s = spec && typeof spec === "object" ? spec : {};
    const doc = document.scrollingElement || document.documentElement;
    const up = (n) => n.parentElement || (n.parentNode && n.parentNode.host) || null;
    const dom = globalThis.chrome && globalThis.chrome.dom && typeof globalThis.chrome.dom.openOrClosedShadowRoot === "function"
      ? globalThis.chrome.dom : null;
    let closedRoots = 0;
    const shadowOf = (n) => {
      if (n.shadowRoot) return n.shadowRoot;
      if (!dom) return null;
      try { const r = dom.openOrClosedShadowRoot(n); if (r) closedRoots++; return r || null; } catch { return null; }
    };
    const roots = () => {
      const out = [document];
      const visit = (root) => { for (const n of root.querySelectorAll("*")) { const r = shadowOf(n); if (r) { out.push(r); visit(r); } } };
      closedRoots = 0;
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
    const rectOf = (el) => { const r = el.getBoundingClientRect(); return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) }; };
    const overflowY = (el) => /^(auto|scroll|overlay)$/.test(getComputedStyle(el).overflowY);
    const scrollable = (el) => el.scrollHeight > el.clientHeight + 10 && overflowY(el);
    const docScrolls = () => doc.scrollHeight > doc.clientHeight + 10;
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
    // What this frame can't show as DOM: its iframes (the service worker reads the ones it may), big canvases,
    // plugins (embed/object), and closed shadow hosts when chrome.dom isn't there to open them.
    const diag = () => {
      const all = roots();
      const iframes = [];
      const regions = [];
      const vpArea = vw() * vh();
      for (const root of all) {
        for (const el of root.querySelectorAll("iframe, frame")) {
          if (iframes.length >= 40 || !shown(el)) continue;
          let frameId = null;
          try {
            const g = globalThis.chrome && globalThis.chrome.runtime && globalThis.chrome.runtime.getFrameId;
            if (typeof g === "function") { const v = g(el); if (Number.isInteger(v) && v > 0) frameId = v; }
          } catch { /* not available */ }
          let url = "";
          try { url = el.contentWindow && el.contentWindow.location ? String(el.contentWindow.location.href) : ""; } catch { /* cross-origin */ }
          if (!url || url === "about:blank") url = el.src || (el.hasAttribute("srcdoc") ? "about:srcdoc" : url || "about:blank");
          let origin = null;
          try { const u = new URL(url); origin = /^https?:$/.test(u.protocol) ? u.origin : null; } catch { /* opaque */ }
          iframes.push({ frameId, url, origin, rect: rectOf(el) });
        }
        for (const el of root.querySelectorAll("canvas, embed, object")) {
          if (regions.length >= 20 || !shown(el)) continue;
          const a = visibleArea(el);
          if (el.localName === "canvas" && a < 0.04 * vpArea) continue;
          regions.push(el.localName === "canvas"
            ? { kind: "canvas", description: describe(el), rect: rectOf(el), reason: "canvas_pixels" }
            : { kind: "plugin", description: describe(el), type: String(el.getAttribute("type") || "").slice(0, 60), rect: rectOf(el), reason: "plugin_content" });
        }
      }
      if (!dom) {
        // Without chrome.dom a closed shadow root is invisible; a sized custom element with no shadowRoot and no
        // light DOM is the tell.
        for (const el of document.querySelectorAll("*")) {
          if (regions.length >= 30) break;
          if (!el.localName.includes("-") || el.shadowRoot || el.childElementCount || (el.textContent || "").trim()) continue;
          if (!shown(el) || visibleArea(el) < 0.01 * vpArea) continue;
          regions.push({ kind: "closed_shadow_host", description: describe(el), rect: rectOf(el), reason: "closed_shadow_root" });
        }
      }
      return { viewport: { w: vw(), h: vh() }, iframes, regions, closedShadowRoots: closedRoots };
    };
    const fail = (code, reason, message, extra = {}) => ({ ok: false, code, reason, message, ...extra, diag: diag() });
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
        if (el) { root = shadowOf(el); if (!root) return null; }
        let n = root;
        for (const i of seg) { n = n && n.children ? n.children[i] : null; if (!n) return null; }
        el = n;
      }
      return el;
    };
    const query = (sel) => {
      try { return { all: deepAll(sel) }; } catch { return { err: fail("bad_target", "bad_selector", `Not a valid CSS selector: ${String(sel).slice(0, 120)}`) }; }
    };
    const isDocEl = (el) => el === document.documentElement || el === document.body;
    const info = (el) => ({ path: pathOf(el), description: describe(el), area: Math.round(el === doc ? vw() * vh() : visibleArea(el)),
      scrollHeight: el.scrollHeight, clientHeight: el.clientHeight });
    // --container: the one scrollable match (html/body mean the window). 0 matches -> no_match; several scrollable
    // -> multiple_matches; matches that don't scroll -> not_scrollable.
    const locate = (sel) => {
      const q = query(sel);
      if (q.err) return q;
      const vis = q.all.filter(shown);
      const scroll = [];
      for (const el of vis) {
        const t = isDocEl(el) ? (scrollable(el) ? el : doc) : el;
        if (t === doc ? true : scrollable(t)) { if (!scroll.some((x) => x.el === t)) scroll.push({ el: t }); }
      }
      return { all: q.all, vis, scroll };
    };
    const autoTarget = () => {
      let best = null;
      let area = 0;
      for (const el of deepAll("*")) {
        if (el === doc || !scrollable(el) || !shown(el)) continue;
        const a = visibleArea(el);
        if (a > area) { best = el; area = a; }
      }
      return { best, area };
    };
    const frame = () => new Promise((r) => { let done = false; const f = () => { if (!done) { done = true; r(); } }; requestAnimationFrame(f); setTimeout(f, 100); });
    const settle = async (ms) => { await frame(); await frame(); if (ms > 0) { await new Promise((r) => setTimeout(r, ms)); await frame(); } };
    const moveTo = (el, top, left) => {
      const opts = { top: Math.max(0, top), ...(left != null ? { left: Math.max(0, left) } : {}) };
      try { el.scrollTo({ ...opts, behavior: "instant" }); } catch { el.scrollTop = opts.top; if (left != null) el.scrollLeft = opts.left; }
    };

    if (s.survey) {
      const { best, area } = autoTarget();
      return { ok: true, url: location.href, title: document.title,
        viewport: { w: vw(), h: vh() }, doc: { scrollHeight: doc.scrollHeight, clientHeight: doc.clientHeight, scrolls: docScrolls() },
        best: best ? { ...info(best), area: Math.round(area) } : null, devicePixelRatio: window.devicePixelRatio || 1, diag: diag() };
    }
    if (s.locate != null) {
      const l = locate(String(s.locate));
      if (l.err) return l.err;
      const first = l.vis[0] || l.all[0] || null;
      return { ok: true, url: location.href, matches: l.all.length, shown: l.vis.length,
        scrollable: l.scroll.slice(0, 10).map((x) => info(x.el)), first: first ? info(isDocEl(first) ? doc : first) : null, diag: diag() };
    }
    if (s.locateInto != null) {
      const q = query(String(s.locateInto));
      if (q.err) return q.err;
      return { ok: true, url: location.href, matches: q.all.length, shown: q.all.filter(shown).length };
    }

    let target = null;
    let auto = false;
    if (s.path != null) {
      target = resolvePath(s.path);
      if (!target) return fail("element_not_found", "target_gone", "The scroll target is gone (the page changed); start again.");
    } else if (s.container != null && s.container !== "") {
      const l = locate(String(s.container));
      if (l.err) return l.err;
      if (!l.all.length) return fail("element_not_found", "no_match", "No element matches --container.", { matches: 0 });
      if (!l.vis.length) return fail("element_not_found", "no_match", `--container matches ${l.all.length} element(s), none visible.`, { matches: l.all.length, shown: 0 });
      if (l.scroll.length > 1) {
        return fail("ambiguous_target", "multiple_matches", `--container matches ${l.scroll.length} scrollable elements; give a narrower selector.`,
          { count: l.scroll.length, matches: l.all.length, candidates: l.scroll.slice(0, 5).map((x) => { const i = info(x.el); delete i.path; return i; }) });
      }
      if (!l.scroll.length) {
        const f = l.vis[0];
        return fail("script_error", "not_scrollable", `--container matched ${describe(f)}, which doesn't scroll (scrollHeight ${f.scrollHeight}, clientHeight ${f.clientHeight}).`,
          { matches: l.all.length, scrollHeight: f.scrollHeight, clientHeight: f.clientHeight, description: describe(f) });
      }
      target = l.scroll[0].el;
    }
    let intoEl = null;
    if (s.into != null && s.into !== "") {
      const q = query(String(s.into));
      if (q.err) return q.err;
      if (!q.all.length) return fail("element_not_found", "no_match", "No element matches --into.", { matches: 0 });
      intoEl = q.all.find(shown) || q.all[0];
      if (!target) {
        for (let n = up(intoEl); n; n = up(n)) if (n !== doc && scrollable(n)) { target = n; break; }
        target = target || doc;
      }
    }
    if (!target) {
      const { best, area } = autoTarget();
      // A small scroll box (a code block, a menu) doesn't beat a scrolling window: it must cover 30% of the viewport.
      target = best && (!docScrolls() || area >= 0.3 * vw() * vh()) ? best : doc;
      auto = true;
    }
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
      else return fail("bad_request", "bad_spec", "to must be top, bottom or {top}.");
    } else {
      const overlap = Math.min(Math.max(0, Number(s.overlap ?? 40) || 0), 400);
      const px = s.by == null || s.by === "page" ? Math.max(Math.round(ch * 0.4), ch - overlap) : Number(s.by);
      if (!Number.isFinite(px) || px <= 0) return fail("bad_request", "bad_spec", "by must be page or a positive number of pixels.");
      moveTo(target, before + (s.dir === -1 ? -px : px));
    }
    await settle(Math.min(Math.max(0, Number(s.settleMs) || 0), 3000));
    const top = target.scrollTop;
    return { ok: true, url: location.href, title: document.title,
      target: { description: describe(target), kind: target === doc ? "document" : "element", auto, inShadow: target !== doc && inShadow(target),
        path: pathOf(target) },
      scrollTop: Math.round(top), scrollLeft: Math.round(target.scrollLeft), scrollHeight: target.scrollHeight,
      clientHeight: target.clientHeight, atTop: top <= 1, atEnd: top + target.clientHeight >= target.scrollHeight - 2,
      moved: Math.round(top - before), devicePixelRatio: window.devicePixelRatio || 1,
      scrolls: target === doc ? docScrolls() : target.scrollHeight > target.clientHeight + 10, diag: diag() };
  } catch (e) {
    return { ok: false, code: "script_error", reason: "page_exception", message: `In-page error: ${String(e && (e.name ? `${e.name}: ${e.message}` : e)).slice(0, 300)}` };
  }
}
