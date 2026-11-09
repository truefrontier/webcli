"""The kill switch: tools the webcli-tools library has revoked. Core won't load a revoked tool or run its ops.

Read from ~/.webcli/tools/index-cache.json (the cached library index.json; `webcli tools` refreshes it) and
~/.webcli/tools/revoked.json. Each holds a "revoked" list (revoked.json may be the bare list); an entry is a tool
name, or {"name": ..., "versions": ["0.1.0", ...], "reason": ...}, where no versions means every version.

Offline, the last cached list still applies. A file that exists but can't be read fails closed: every community tool
counts as revoked until it is fixed or removed, so a damaged cache never switches a revoked tool back on.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from webcli.core import config

SOURCES = ("index-cache.json", "revoked.json")
STALE_DAYS = 7


def _entries(path: Path) -> list[dict] | None:
    """The file's revoked entries ([] when it doesn't exist), or None when it can't be read."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    items = data.get("revoked", []) if isinstance(data, dict) else data
    if not isinstance(items, list):
        return None
    out = []
    for item in items:
        item = {"name": item} if isinstance(item, str) else item
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) \
                or not isinstance(item.get("versions", []), list):
            return None
        out.append(item)
    return out


def reason(name: str, version: str | None = None) -> str | None:
    """Why `name` (at `version`) is revoked, or None if it isn't. version None matches only entries that revoke
    every version."""
    for source in SOURCES:
        path = config.home() / "tools" / source
        entries = _entries(path)
        if entries is None:
            return f"{path} can't be read, so no community tool runs until it is fixed or removed"
        for entry in entries:
            versions = entry.get("versions") or []
            if entry["name"] == name and (not versions or version in versions):
                return str(entry.get("reason") or "revoked by the webcli-tools library")
    return None


def is_revoked(name: str, version: str | None = None) -> bool:
    return reason(name, version) is not None


def freshness(community: bool) -> dict:
    """How old the cached library index is, for `webcli doctor`: the library's revocations arrive in it, and only
    `webcli tools` commands refresh it. Stale: a community tool is installed and the list is missing or older than
    STALE_DAYS."""
    path = config.home() / "tools" / SOURCES[0]
    try:
        age = round((time.time() - path.stat().st_mtime) / 86400, 1)
    except OSError:
        age = None
    stale = community and (age is None or age > STALE_DAYS)
    return {"cache": str(path), "ageDays": age, "stale": stale,
            "hint": "Refresh the library's revoked list: webcli tools search" if stale else None}
