// 0.16.3: the registrable domain ("site", eTLD+1) of a hostname, from the bundled Public Suffix List (psl-data.js,
// ICANN and private sections). A share follows its tab across hosts of one site (developer.apple.com <->
// appstoreconnect.apple.com share apple.com) but never across sites; multi-tenant suffixes (github.io, vercel.app,
// pages.dev, herokuapp.com, ...) are public suffixes, so alice.github.io and bob.github.io are different sites.
import { PSL_RULES } from "./psl-data.js";

let RULES = null;
const rules = () => (RULES ||= new Set(PSL_RULES.split("\n")));

// The public suffix of a lowercase ASCII hostname (the longest matching rule; exceptions win; default rule "*").
function suffixLabels(labels) {
  const R = rules();
  for (let i = 0; i < labels.length; i++) {
    const cand = labels.slice(i).join(".");
    if (R.has(`!${cand}`)) return labels.length - i - 1;
    if (R.has(cand)) return labels.length - i;
    if (i + 1 < labels.length && R.has(`*.${labels.slice(i + 1).join(".")}`)) return labels.length - i;
  }
  return 1;
}

// "developer.apple.com" -> "apple.com"; "alice.github.io" -> "alice.github.io"; "www.example.co.uk" ->
// "example.co.uk". null for IP addresses, single-label hosts (localhost), a public suffix itself, or junk.
export function registrableDomain(host) {
  const h = String(host || "").toLowerCase().replace(/\.$/, "");
  if (!h || h.length > 253 || !/^[a-z0-9-]+(\.[a-z0-9-]+)+$/.test(h)) return null;
  if (/^\d+(\.\d+){3}$/.test(h)) return null; // IPv4 (IPv6 hosts are bracketed and fail the pattern above)
  const labels = h.split(".");
  const n = suffixLabels(labels);
  if (n >= labels.length) return null;
  return labels.slice(labels.length - n - 1).join(".");
}

// The site of an http(s) origin or URL, or null.
export function siteOf(originOrUrl) {
  try {
    const u = new URL(originOrUrl);
    return u.protocol === "http:" || u.protocol === "https:" ? registrableDomain(u.hostname) : null;
  } catch { return null; }
}

// The host permission a share asks for: every host of the site ("*://*.apple.com/*", which also matches apple.com),
// or just the origin's host when it has no site (an IP, localhost).
export function sitePermissionPattern(origin) {
  const site = siteOf(origin);
  if (site) return `*://*.${site}/*`;
  const u = new URL(origin);
  return `${u.protocol}//${u.hostname}/*`;
}

// Can a share made on `from` follow its tab to `to`? Same site, http(s) both, and never https -> http.
export function sameSite(from, to) {
  try {
    const a = new URL(from); const b = new URL(to);
    if (!["http:", "https:"].includes(a.protocol) || !["http:", "https:"].includes(b.protocol)) return false;
    if (a.protocol === "https:" && b.protocol === "http:") return false;
    const s = registrableDomain(a.hostname);
    return !!s && s === registrableDomain(b.hostname);
  } catch { return false; }
}
