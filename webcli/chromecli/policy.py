"""chromecli's origin policy: defaults, [chromecli] config parse/validate, and pattern matching. Mirrored in
extensions/chromecli/policy.js (the extension enforces; the host only supplies the policy). Shared test cases:
extensions/chromecli/test/policy_cases.json (pytest and node --test both run them).

Patterns:
    host              that host, any scheme and port            e.g. "accounts.google.com"
    *.host            the host and every subdomain             e.g. "*.chase.com" (chase.com too)
    scheme://host[:port]  that origin (host may be *.host)     e.g. "http://127.0.0.1:8765"
    host/path         that host, paths starting with /path      e.g. "github.com/login"
    path:/prefix      any origin whose path contains /prefix at a segment start, e.g. "path:/checkout"

Built-in deny, which config can't remove: every scheme other than http and https (chrome://, chrome-extension://,
file://, devtools://, view-source:, about:, data:, ...), the Chrome Web Store (chromewebstore.google.com,
chrome.google.com/webstore).

Loosening (adding to read_allow / act_allow, removing a default or previous deny, implicit_active off -> read, history
false -> true) is held as "pending" by the extension until Kevin clicks Accept in its popup; tightening applies at once.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import urlsplit

from webcli.chromecli import CONFIG_SECTION
from webcli.core.errors import ConfigError

BUILTIN_DENY_HOSTS = ("chromewebstore.google.com", "chrome.google.com/webstore")
ALLOWED_SCHEMES = ("http", "https")
DEFAULT_DENY = (
    "*.chase.com", "*.bankofamerica.com", "*.wellsfargo.com", "*.schwab.com", "*.fidelity.com",
    "accounts.google.com", "login.microsoftonline.com", "*.okta.com", "*.auth0.com", "appleid.apple.com",
    "*.1password.com", "vault.bitwarden.com", "*.lastpass.com", "checkout.stripe.com", "*.paypal.com",
    "pay.google.com", "path:/checkout", "path:/cart", "path:/oauth", "path:/login",
)
DEFAULTS: dict[str, Any] = {
    "implicit_active": "off",
    "history": True,
    "confirm_timeout": 60,
    "read_allow": [],
    "act_allow": [],
    "deny": list(DEFAULT_DENY),
}
KEYS = tuple(DEFAULTS)

_HOST = r"(?:\*\.)?[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*"
_ORIGIN_RE = re.compile(rf"^(https?)://({_HOST})(?::(\d{{1,5}}))?/?$")
_HOST_RE = re.compile(rf"^({_HOST})$")
_HOST_PATH_RE = re.compile(rf"^({_HOST})(/[^\s]*)$")
_PATH_RE = re.compile(r"^path:(/[^\s]*)$")


def pattern_kind(pattern: str) -> str | None:
    """'origin' | 'host' | 'host_path' | 'path', or None when the pattern is invalid."""
    if not isinstance(pattern, str):
        return None
    p = pattern.strip().lower()
    if _PATH_RE.match(p) and len(p) > len("path:/"):
        return "path"
    if _ORIGIN_RE.match(p):
        return "origin"
    if _HOST_RE.match(p):
        return "host"
    if _HOST_PATH_RE.match(p):
        return "host_path"
    return None


def _host_match(pat_host: str, host: str) -> bool:
    if pat_host.startswith("*."):
        base = pat_host[2:]
        return host == base or host.endswith("." + base)
    return host == pat_host


def _default_port(scheme: str) -> int:
    return 443 if scheme == "https" else 80


def matches(pattern: str, url: str) -> bool:
    """Whether `url` falls under `pattern` (see the module doc). Hosts and paths compare case-insensitively."""
    kind = pattern_kind(pattern)
    if kind is None:
        return False
    p = pattern.strip().lower()
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return False
    scheme = parts.scheme.lower()
    path = (parts.path or "/").lower()
    if kind == "path":
        prefix = _PATH_RE.match(p).group(1)
        return prefix in path
    if kind == "host":
        return bool(host) and _host_match(p, host)
    if kind == "host_path":
        m = _HOST_PATH_RE.match(p)
        return bool(host) and _host_match(m.group(1), host) and path.startswith(m.group(2))
    m = _ORIGIN_RE.match(p)
    want_port = int(m.group(3)) if m.group(3) else _default_port(m.group(1))
    have_port = port if port is not None else _default_port(scheme)
    return scheme == m.group(1) and _host_match(m.group(2), host) and have_port == want_port


def builtin_denied(url: str) -> str | None:
    """The built-in reason `url` can never be shared, or None."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "unparseable URL"
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        return f"built-in: {parts.scheme or 'no'} scheme"
    for pat in BUILTIN_DENY_HOSTS:
        if matches(pat, url):
            return f"built-in: {pat}"
    return None


def denied(url: str, policy: dict) -> str | None:
    """Why `url` is denied under `policy` (built-in first, then its deny list), or None."""
    reason = builtin_denied(url)
    if reason:
        return reason
    for pat in policy.get("deny", ()):
        if matches(pat, url):
            return f"deny: {pat}"
    return None


def origin_of(url: str) -> str | None:
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in ALLOWED_SCHEMES or not parts.hostname:
            return None
        port = f":{parts.port}" if parts.port and parts.port != _default_port(parts.scheme.lower()) else ""
    except ValueError:
        return None
    return f"{parts.scheme.lower()}://{parts.hostname.lower()}{port}"


def policy_hash(policy: dict) -> str:
    body = {k: policy[k] for k in KEYS}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def _finish(policy: dict) -> dict:
    policy = {k: policy[k] for k in KEYS}
    policy["builtin_deny"] = ["non-http(s) schemes (chrome://, chrome-extension://, file://, devtools://, ...)",
                              *BUILTIN_DENY_HOSTS]
    policy["hash"] = policy_hash(policy)
    return policy


def default_policy() -> dict:
    return _finish({k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()})


def _strings(section: dict, key: str, *, allow_paths: bool) -> list[str]:
    value = section[key]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"[{CONFIG_SECTION}] {key} must be a list of strings.", hint=_hint())
    out = []
    for v in value:
        kind = pattern_kind(v)
        if kind is None:
            raise ConfigError(f"[{CONFIG_SECTION}] {key}: {v!r} is not a valid pattern.", hint=_hint())
        if not allow_paths and kind in ("path", "host_path"):
            raise ConfigError(f"[{CONFIG_SECTION}] {key}: {v!r}: allow lists take origins or hosts, not paths.",
                              hint=_hint())
        if v.strip().lower() not in out:
            out.append(v.strip().lower())
    return out


def _hint() -> str:
    return (f"Fix [{CONFIG_SECTION}] in ~/.webcli/config.toml (keys: {', '.join(KEYS)}; patterns: host, *.host, "
            "scheme://host[:port], host/path, path:/prefix).")


def parse(section: Any) -> dict:
    """The policy for a [chromecli] table (None/absent = defaults). Invalid -> ConfigError (exit 7)."""
    if section is None:
        return default_policy()
    if not isinstance(section, dict):
        raise ConfigError(f"[{CONFIG_SECTION}] must be a table.", hint=_hint())
    unknown = sorted(set(section) - set(KEYS))
    if unknown:
        raise ConfigError(f"[{CONFIG_SECTION}] has unknown keys: {', '.join(unknown)}.", hint=_hint())
    merged = {**DEFAULTS, **section}
    if merged["implicit_active"] not in ("off", "read"):
        raise ConfigError(f'[{CONFIG_SECTION}] implicit_active must be "off" or "read".', hint=_hint())
    if not isinstance(merged["history"], bool):
        raise ConfigError(f"[{CONFIG_SECTION}] history must be true or false.", hint=_hint())
    ct = merged["confirm_timeout"]
    if isinstance(ct, bool) or not isinstance(ct, int) or not 10 <= ct <= 600:
        raise ConfigError(f"[{CONFIG_SECTION}] confirm_timeout must be an integer from 10 to 600.", hint=_hint())
    merged = dict(merged)
    merged["read_allow"] = _strings(merged, "read_allow", allow_paths=False)
    merged["act_allow"] = _strings(merged, "act_allow", allow_paths=False)
    merged["deny"] = _strings(merged, "deny", allow_paths=True)
    return _finish(merged)


def load() -> dict:
    """The policy from ~/.webcli/config.toml ([chromecli]); ConfigError when invalid."""
    from webcli.core import config

    return parse(config.load().get(CONFIG_SECTION))


def loosening(old: dict, new: dict) -> list[dict]:
    """What `new` widens relative to `old` (empty: `new` is the same or tighter)."""
    out = []
    for key in ("read_allow", "act_allow"):
        out += [{"kind": f"{key}_added", "value": v} for v in new[key] if v not in old[key]]
    out += [{"kind": "deny_removed", "value": v} for v in old["deny"] if v not in new["deny"]]
    if old["implicit_active"] == "off" and new["implicit_active"] == "read":
        out.append({"kind": "implicit_active", "value": "read"})
    if not old["history"] and new["history"]:
        out.append({"kind": "history_enabled", "value": True})
    return out


def tighten(old: dict, new: dict) -> dict:
    """The policy in force while `new`'s loosening waits for Kevin: new's tightening, none of its loosening."""
    merged = {
        "implicit_active": "read" if old["implicit_active"] == new["implicit_active"] == "read" else "off",
        "history": bool(old["history"] and new["history"]),
        "confirm_timeout": new["confirm_timeout"],
        "read_allow": [v for v in new["read_allow"] if v in old["read_allow"]],
        "act_allow": [v for v in new["act_allow"] if v in old["act_allow"]],
        "deny": [*new["deny"], *[v for v in old["deny"] if v not in new["deny"]]],
    }
    return _finish(merged)
