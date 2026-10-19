"""H4 storage hardening for the data home: owner-only permissions, no Time Machine backups, no Spotlight index, no
iCloud sync. apply() runs when the home is first created; check() is what `webcli doctor` reports.

H2 (Chrome keeps its cookies under the real "Chrome Safe Storage" Keychain key, not Playwright's mock keychain) lives
in browser.IGNORED_DEFAULT_ARGS. Nothing here reads the Keychain or any cookie.
"""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

# Spotlight skips a folder holding .metadata_never_index; .nosync marks it for sync tools that honor it.
MARKERS = (".metadata_never_index", ".nosync")


def _run(args: list[str]) -> subprocess.CompletedProcess | None:
    """A read-only (or exclusion-setting) system tool; None when it isn't there (not macOS) or hangs."""
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None


def mode(path: Path) -> int | None:
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None


def apply(root: Path) -> dict:
    """chmod 700, drop the Spotlight/.nosync markers, and exclude `root` from Time Machine. Returns check()."""
    root.chmod(0o700)
    for name in MARKERS:
        marker = root / name
        marker.touch(mode=0o600, exist_ok=True)
    for name in ("run", "logs", "identities", "sites"):
        if (root / name).is_dir():
            (root / name).chmod(0o700)
    _run(["tmutil", "addexclusion", str(root)])
    return check(root)


def time_machine_excluded(root: Path) -> bool | None:
    done = _run(["tmutil", "isexcluded", str(root)])
    if done is None or done.returncode != 0:
        return None
    return "[Excluded]" in done.stdout


def filevault() -> str | None:
    """"on", "off", or None when fdesetup can't say. Read-only."""
    done = _run(["fdesetup", "status"])
    if done is None:
        return None
    text = done.stdout.lower()
    return "on" if "filevault is on" in text else "off" if "filevault is off" in text else None


def check(root: Path) -> dict:
    """Each H4 item's state. ok: everything that can be checked is in place (FileVault off counts against it)."""
    perms = {name: oct(m) if (m := mode(root / name if name else root)) is not None else None
             for name in ("", "run", "logs", "identities", "sites")}
    perms = {name or ".": value for name, value in perms.items()}
    loose_sockets = sorted(str(p) for p in (root / "run").glob("*.sock") if mode(p) not in (None, 0o600)) \
        if (root / "run").is_dir() else []
    out = {
        "path": str(root),
        "exists": root.is_dir(),
        "permissions": perms,
        "private": all(v in (None, "0o700") for v in perms.values()) and perms["."] == "0o700",
        "socketsPrivate": not loose_sockets,
        "looseSockets": loose_sockets,
        "timeMachineExcluded": time_machine_excluded(root) if root.is_dir() else None,
        "spotlightExcluded": (root / ".metadata_never_index").exists(),
        "nosync": (root / ".nosync").exists(),
        "fileVault": filevault(),
    }
    out["ok"] = (out["exists"] and out["private"] and out["socketsPrivate"] and out["spotlightExcluded"]
                 and out["nosync"] and out["timeMachineExcluded"] is not False and out["fileVault"] != "off")
    return out
