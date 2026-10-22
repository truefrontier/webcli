"""The native-messaging host manifest: render, write, remove, and check
~/Library/Application Support/Google/Chrome/NativeMessagingHosts/com.truefrontier.chromecli.json."""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

from webcli.chromecli import EXTENSION_ID, HOST_NAME, extension_origin

HOST_SCRIPT = "chromecli-host"
_ID_RE = re.compile(r"^[a-p]{32}$")


def nmh_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "Google" / "Chrome" / "NativeMessagingHosts"


def manifest_path() -> Path:
    return nmh_dir() / f"{HOST_NAME}.json"


def valid_id(extension_id: str) -> bool:
    return bool(_ID_RE.match(extension_id or ""))


def host_path() -> Path | None:
    """The chromecli-host console script next to this Python (pipx/venv bin), else on PATH."""
    sibling = Path(sys.executable).parent / HOST_SCRIPT
    if sibling.exists():
        return sibling.absolute()
    found = shutil.which(HOST_SCRIPT)
    return Path(found).absolute() if found else None


def render(extension_id: str = EXTENSION_ID, path: Path | None = None) -> dict:
    path = path or host_path()
    return {
        "name": HOST_NAME,
        "description": "chromecli native host: lets the chromecli CLI read the Chrome tabs you share",
        "path": str(path) if path else None,
        "type": "stdio",
        "allowed_origins": [extension_origin(extension_id)],
    }


def installed() -> dict | None:
    """The installed manifest, or None (missing or unreadable)."""
    try:
        return json.loads(manifest_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def allowed_origins() -> list[str]:
    """Origins the host accepts as argv[1]: the built-in ID's, plus whatever the installed manifest allows."""
    out = [extension_origin()]
    data = installed() or {}
    for origin in data.get("allowed_origins") or []:
        if isinstance(origin, str) and origin not in out:
            out.append(origin)
    return out


def write(manifest: dict) -> Path:
    target = manifest_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp")
    tmp.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    tmp.chmod(0o644)
    tmp.replace(target)
    return target


def remove() -> bool:
    try:
        manifest_path().unlink()
        return True
    except FileNotFoundError:
        return False


def check(extension_id: str = EXTENSION_ID) -> dict:
    """{manifestPresent, manifestPath, hostPath, hostPathExists, allowedOrigins, idMatches, ok}."""
    data = installed()
    path = (data or {}).get("path")
    origins = (data or {}).get("allowed_origins") or []
    report = {
        "manifestPresent": data is not None,
        "manifestPath": str(manifest_path()),
        "hostPath": path,
        "hostPathExists": bool(path and Path(path).exists()),
        "allowedOrigins": origins,
        "expectedOrigin": extension_origin(extension_id),
        "idMatches": origins == [extension_origin(extension_id)],
    }
    report["ok"] = bool(report["manifestPresent"] and report["hostPathExists"] and report["idMatches"])
    return report
