// chromecli origin policy: pattern matching, built-in deny, loosening / tightening.
// Mirrors webcli/chromecli/policy.py; both run test/policy_cases.json. Pure ES module (node --test).

export const BUILTIN_DENY_HOSTS = ["chromewebstore.google.com", "chrome.google.com/webstore"];
export const ALLOWED_SCHEMES = ["http:", "https:"];
export const DEFAULT_DENY = [
  "*.chase.com", "*.bankofamerica.com", "*.wellsfargo.com", "*.schwab.com", "*.fidelity.com",
  "accounts.google.com", "login.microsoftonline.com", "*.okta.com", "*.auth0.com", "appleid.apple.com",
  "*.1password.com", "vault.bitwarden.com", "*.lastpass.com", "checkout.stripe.com", "*.paypal.com",
  "pay.google.com", "path:/checkout", "path:/cart", "path:/oauth", "path:/login",
];
export const DEFAULT_POLICY = Object.freeze({
  implicit_active: "off", history: true, confirm_timeout: 60, read_allow: [], act_allow: [],
  deny: DEFAULT_DENY, hash: "builtin-default",
});

const HOST = String.raw`(?:\*\.)?[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*`;
const ORIGIN_RE = new RegExp(String.raw`^(https?)://(${HOST})(?::(\d{1,5}))?/?$`);
const HOST_RE = new RegExp(`^(${HOST})$`);
const HOST_PATH_RE = new RegExp(String.raw`^(${HOST})(/\S*)$`);
const PATH_RE = /^path:(\/\S*)$/;

export function patternKind(pattern) {
  if (typeof pattern !== "string") return null;
  const p = pattern.trim().toLowerCase();
  if (PATH_RE.test(p) && p.length > "path:/".length) return "path";
  if (ORIGIN_RE.test(p)) return "origin";
  if (HOST_RE.test(p)) return "host";
  if (HOST_PATH_RE.test(p)) return "host_path";
  return null;
}

function hostMatch(patHost, host) {
  if (patHost.startsWith("*.")) {
    const base = patHost.slice(2);
    return host === base || host.endsWith("." + base);
  }
  return host === patHost;
}

const defaultPort = (scheme) => (scheme === "https" ? 443 : 80);

function parse(url) {
  try {
    const u = new URL(url);
    return { scheme: u.protocol.replace(/:$/, "").toLowerCase(), host: u.hostname.toLowerCase(),
      port: u.port ? Number(u.port) : null, path: (u.pathname || "/").toLowerCase() };
  } catch {
    return null;
  }
}

export function matches(pattern, url) {
  const kind = patternKind(pattern);
  const u = parse(url);
  if (!kind || !u) return false;
  const p = pattern.trim().toLowerCase();
  if (kind === "path") return u.path.includes(PATH_RE.exec(p)[1]);
  if (kind === "host") return !!u.host && hostMatch(p, u.host);
  if (kind === "host_path") {
    const m = HOST_PATH_RE.exec(p);
    return !!u.host && hostMatch(m[1], u.host) && u.path.startsWith(m[2]);
  }
  const m = ORIGIN_RE.exec(p);
  const want = m[3] ? Number(m[3]) : defaultPort(m[1]);
  const have = u.port ?? defaultPort(u.scheme);
  return u.scheme === m[1] && hostMatch(m[2], u.host) && have === want;
}

export function builtinDenied(url) {
  let u;
  try { u = new URL(url); } catch { return "unparseable URL"; }
  if (!ALLOWED_SCHEMES.includes(u.protocol.toLowerCase())) {
    return `built-in: ${u.protocol.replace(/:$/, "") || "no"} scheme`;
  }
  for (const pat of BUILTIN_DENY_HOSTS) if (matches(pat, url)) return `built-in: ${pat}`;
  return null;
}

export function denied(url, policy) {
  const b = builtinDenied(url);
  if (b) return b;
  for (const pat of policy?.deny || []) if (matches(pat, url)) return `deny: ${pat}`;
  return null;
}

export function originOf(url) {
  try {
    const u = new URL(url);
    if (!ALLOWED_SCHEMES.includes(u.protocol) || !u.hostname) return null;
    return `${u.protocol}//${u.hostname.toLowerCase()}${u.port ? ":" + u.port : ""}`;
  } catch {
    return null;
  }
}

// The host-permission match pattern for an origin (ports are dropped: Chrome match patterns match every port).
export function permissionPattern(origin) {
  const u = new URL(origin);
  return `${u.protocol}//${u.hostname}/*`;
}

export function loosening(oldP, newP) {
  const out = [];
  for (const key of ["read_allow", "act_allow"]) {
    for (const v of newP[key]) if (!oldP[key].includes(v)) out.push({ kind: `${key}_added`, value: v });
  }
  for (const v of oldP.deny) if (!newP.deny.includes(v)) out.push({ kind: "deny_removed", value: v });
  if (oldP.implicit_active === "off" && newP.implicit_active === "read") out.push({ kind: "implicit_active", value: "read" });
  if (!oldP.history && newP.history) out.push({ kind: "history_enabled", value: true });
  return out;
}

export function tighten(oldP, newP) {
  return {
    implicit_active: oldP.implicit_active === "read" && newP.implicit_active === "read" ? "read" : "off",
    history: !!(oldP.history && newP.history),
    confirm_timeout: newP.confirm_timeout,
    read_allow: newP.read_allow.filter((v) => oldP.read_allow.includes(v)),
    act_allow: newP.act_allow.filter((v) => oldP.act_allow.includes(v)),
    deny: [...newP.deny, ...oldP.deny.filter((v) => !newP.deny.includes(v))],
    hash: `${newP.hash}~tightened`,
  };
}

// Validate a pushed policy's shape (the host is an untrusted caller).
export function validPolicy(p) {
  if (!p || typeof p !== "object") return false;
  const lists = ["read_allow", "act_allow", "deny"];
  if (!lists.every((k) => Array.isArray(p[k]) && p[k].every((v) => patternKind(v) !== null))) return false;
  if (!["off", "read"].includes(p.implicit_active) || typeof p.history !== "boolean") return false;
  return Number.isInteger(p.confirm_timeout) && typeof p.hash === "string";
}

function normalize(p) {
  return {
    implicit_active: p.implicit_active, history: p.history, confirm_timeout: p.confirm_timeout,
    read_allow: p.read_allow.map((v) => v.trim().toLowerCase()),
    act_allow: p.act_allow.map((v) => v.trim().toLowerCase()),
    deny: p.deny.map((v) => v.trim().toLowerCase()), hash: p.hash,
  };
}

// state = {active, pending: {policy, loosening} | null}; returns {state, accepted}.
export function applyIncoming(state, incoming) {
  const active = state?.active || DEFAULT_POLICY;
  const inc = normalize(incoming);
  const loose = loosening(active, inc);
  if (loose.length === 0) return { state: { active: inc, pending: null }, accepted: true };
  return { state: { active: tighten(active, inc), pending: { policy: inc, loosening: loose } }, accepted: false };
}

export function acceptPending(state) {
  if (!state?.pending) return state;
  return { active: state.pending.policy, pending: null };
}

export function rejectPending(state) {
  return { active: state?.active || DEFAULT_POLICY, pending: null };
}

// Would accepting the pending policy change the answer for this URL / feature? (-> policy_pending, exit 4)
export function pendingAffects(state, { url = null, feature = null } = {}) {
  const pend = state?.pending;
  if (!pend) return false;
  if (feature) return pend.loosening.some((l) => l.kind === feature);
  if (!url) return false;
  const active = state.active;
  if (denied(url, active) && !denied(url, pend.policy)) return true;
  return pend.loosening.some((l) => (l.kind === "read_allow_added" || l.kind === "act_allow_added") && matches(l.value, url));
}

// Host-permission match pattern for an allow-list entry (origin or host pattern), or null.
export function allowToPermission(pattern) {
  const kind = patternKind(pattern);
  const p = String(pattern).trim().toLowerCase();
  if (kind === "origin") { const m = ORIGIN_RE.exec(p); return `${m[1]}://${m[2]}/*`; }
  if (kind === "host") return `*://${p}/*`;
  return null;
}
