// What the popup asks chrome.permissions.request for when Kevin shares a tab, and what it says if Chrome says no.
// Pure (no DOM, no chrome.*), so node --test can check every origin against the manifest the way Chrome does.
//
// 0.16.4: Chrome only lets an extension request an origin that ONE optional_host_permissions entry contains
// (URLPatternSet::ContainsPattern). The manifest has "http://*/*" and "https://*/*", so "*://*.apple.com/*"
// (http AND https) is contained by neither: Chrome threw "Only permissions specified in the manifest may be
// requested" without showing a prompt, and 0.16.3's popup read that as "not granted". Every origin here is
// scheme-specific.
import { allowToPermission } from "./policy.js";
import { siteOf } from "./psl.js";

// "*://X/*" -> "http://X/*", "https://X/*"; anything else as is. Order kept, duplicates dropped.
export function expandSchemes(patterns) {
  const out = [];
  for (const p of patterns) {
    const m = /^\*:\/\/(.+)$/.exec(String(p));
    for (const o of m ? [`http://${m[1]}`, `https://${m[1]}`] : [String(p)]) if (!out.includes(o)) out.push(o);
  }
  return out;
}

// The site part of a share's request: https://*.SITE/* for an https tab (a share never follows https -> http, so
// http hosts are never needed), http and https for an http tab (http -> https may follow). No site (an IP,
// localhost): just the tab's host, as before 0.16.3. "*.SITE" also matches SITE itself (apple.com).
export function sitePermissions(origin) {
  const u = new URL(origin);
  const site = siteOf(origin);
  if (!site) return [`${u.protocol}//${u.hostname}/*`];
  return u.protocol === "http:" ? [`http://*.${site}/*`, `https://*.${site}/*`] : [`https://*.${site}/*`];
}

// {origins, site, host, frames}: the one prompt for a share. Act also asks for the frame_act_allow origins (0.11.3).
export function shareRequest(origin, level, frameActAllow = []) {
  const frames = level === "act" ? expandSchemes((frameActAllow || []).map(allowToPermission).filter(Boolean)) : [];
  const origins = expandSchemes([...sitePermissions(origin), ...frames]);
  const hosts = frames.map((f) => f.replace(/^https?:\/\//, "").replace(/\/\*$/, ""));
  return { origins, site: siteOf(origin), host: new URL(origin).hostname, frames: [...new Set(hosts)] };
}

// The popup's message when the share didn't go through. `error` is the request's thrown error (Chrome refused the
// request itself, no prompt shown), or null when Kevin cancelled / Chrome declined.
export function shareDeniedMessage(req, error = null) {
  const where = req.site ? `*.${req.site}` : req.host;
  const plus = req.frames.length ? ` (and ${req.frames.join(", ")} for its app frames)` : "";
  if (error) return `Chrome refused the request for ${where}${plus}: ${String(error.message || error).slice(0, 160)}; not shared.`;
  return `Chrome didn't grant access to ${where}${plus}; not shared.`;
}

// Origins to request when Kevin accepts a policy that adds allow-list entries (host patterns become http + https).
export function loosenRequest(loosening = []) {
  return expandSchemes(loosening
    .filter((l) => l.kind === "read_allow_added" || l.kind === "act_allow_added" || l.kind === "frame_act_allow_added")
    .map((l) => allowToPermission(l.value)).filter(Boolean));
}
