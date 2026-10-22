"""The chromecli row of `webcli doctor` / `llmcli doctor` / `chromecli doctor`: host manifest present and pointing at
an existing chromecli-host, its allowed origin is this extension's ID, the socket, and whether the extension answers."""

from __future__ import annotations

import stat

from webcli.chromecli import EXTENSION_ID, bridge, install


def report(*, probe: bool = True) -> dict:
    host = install.check()
    path = bridge.socket_path()
    sock = {"path": str(path), "present": path.exists(), "mode": None}
    if sock["present"]:
        sock["mode"] = oct(stat.S_IMODE(path.stat().st_mode))
    out = {"extensionId": EXTENSION_ID, "host": host, "socket": sock, "connected": False, "extVersion": None,
           "extensionIdMatches": None, "paused": None, "shares": None}
    if probe and sock["present"]:
        try:
            reply = bridge.call("status", timeout=3)
        except bridge.BridgeDown as exc:
            out["error"] = str(exc)
        else:
            if reply.get("ok"):
                res = reply.get("result") or {}
                out.update(connected=True, extVersion=res.get("extVersion"),
                           extensionIdMatches=res.get("extensionId") == EXTENSION_ID, paused=res.get("paused"),
                           shares=len(res.get("shares") or []))
                host_info = res.get("host") if isinstance(res.get("host"), dict) else {}
                check = bridge.version_check(host_info.get("version"), res.get("extVersion"))
                out.update(versions=check["versions"], versionMismatch=check["versionMismatch"],
                           configWarnings=list(host_info.get("configWarnings") or []))
                if check["versionMismatch"]:
                    out["hint"] = check["reloadHint"]
            else:
                out["error"] = (reply.get("error") or {}).get("code")
    out["ok"] = bool(host["ok"] and out["connected"] and out["extensionIdMatches"])
    return out
