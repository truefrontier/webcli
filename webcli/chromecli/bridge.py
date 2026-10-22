"""The CLI side: connect to run/chromecli.sock, send one request line, read one reply line, and map an error reply
onto die(message, exit, error=code)."""

from __future__ import annotations

import json
import os
import socket
from typing import Any

from webcli import __version__
from webcli.chromecli import SOCKET_NAME, TIMEOUT_ENV, install, protocol
from webcli.core import config
from webcli.core.output import die

DEFAULT_TIMEOUT = 30.0


def timeout_seconds(override: float | None = None) -> float:
    if override:
        return float(override)
    try:
        return float(os.environ.get(TIMEOUT_ENV, "") or DEFAULT_TIMEOUT)
    except ValueError:
        return DEFAULT_TIMEOUT


def socket_path():
    return config.home() / "run" / SOCKET_NAME


class BridgeDown(Exception):
    pass


def _down_hint() -> str:
    check = install.check()
    if not check["manifestPresent"]:
        return "The native host isn't installed: run `chromecli install-host --confirm`, then reload the extension."
    if not check["idMatches"]:
        return "The host manifest allows another extension ID: re-run `chromecli install-host --confirm`."
    return ("Chrome isn't running, or the chromecli extension isn't loaded / enabled (chrome://extensions). "
            "It reconnects within 30s of Chrome starting.")


def call(op: str, args: dict | None = None, *, timeout: float | None = None, confirm: bool = False) -> dict:
    """The raw reply dict. Raises BridgeDown when there's no socket or nothing listens on it."""
    secs = timeout_seconds(timeout)
    path = socket_path()
    if not path.exists():
        raise BridgeDown(f"No chromecli socket at {path}.")
    req = {"v": protocol.VERSION, "op": op, "args": args or {}, "confirm": confirm,
           "client": {"version": __version__, "pid": os.getpid(), "timeoutMs": int(secs * 1000)}}
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(secs + 5)
    try:
        try:
            sock.connect(str(path))
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            raise BridgeDown(f"Nothing is listening on {path} ({exc.__class__.__name__}).") from None
        sock.sendall(json.dumps(req, separators=(",", ":")).encode() + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(1 << 20)
            if not chunk:
                break
            buf += chunk
    except socket.timeout:
        return protocol.reply_error(None, "timeout", f"No reply from chromecli-host within {secs + 5:g}s.")
    except OSError as exc:
        raise BridgeDown(f"The chromecli socket failed: {exc}") from None
    finally:
        sock.close()
    if not buf.strip():
        raise BridgeDown("chromecli-host closed the connection without a reply (Chrome closing?).")
    try:
        reply = json.loads(buf.split(b"\n", 1)[0])
    except json.JSONDecodeError:
        return protocol.reply_error(None, "bad_request", "chromecli-host sent a malformed reply.")
    return reply if isinstance(reply, dict) else protocol.reply_error(None, "bad_request", "Malformed reply.")


RELOAD = "reload the chromecli extension in chrome://extensions"


def version_check(host_version: Any, ext_version: Any) -> dict:
    """{"versions": {cli, host, extension}, "versionMismatch": bool, "reloadHint"?}. A running chromecli-host keeps the
    code it started with: after an upgrade it (and the extension) can be older than the installed CLI until the
    extension is reloaded, which restarts the host."""
    versions = {"cli": __version__, "host": host_version or None, "extension": ext_version or None}
    known = [v for v in versions.values() if v]
    out: dict = {"versions": versions, "versionMismatch": len(set(known)) > 1}
    if out["versionMismatch"]:
        out["reloadHint"] = (f"Versions differ (CLI {versions['cli']}, host {versions['host'] or '?'}, extension "
                             f"{versions['extension'] or '?'}): {RELOAD} (that restarts chromecli-host on the "
                             f"installed version), then run `chromecli status`.")
    return out


def _versions_for_error(err: dict) -> dict:
    """The version check for an exit-7 reply: the reply's own hostVersion/extVersion (0.11.4+ hosts), else a status
    call (status works even while the host refuses everything else)."""
    if err.get("hostVersion"):
        return version_check(err.get("hostVersion"), err.get("extVersion"))
    try:
        reply = call("status", timeout=3)
    except BridgeDown:
        return {}
    res = reply.get("result") if reply.get("ok") and isinstance(reply.get("result"), dict) else {}
    host = res.get("host") if isinstance(res.get("host"), dict) else {}
    return version_check(host.get("version"), res.get("extVersion")) if host.get("version") else {}


def die_reply(reply: dict) -> None:
    err = reply.get("error") if isinstance(reply.get("error"), dict) else {}
    code = str(err.get("code") or "script_error")
    exit_code = protocol.exit_code(code)
    extra = {k: v for k, v in err.items() if k not in ("code", "message", "hint", "exit", "hostVersion", "extVersion")}
    message = str(err.get("message") or code)
    hint = err.get("hint") or protocol.ERROR_HINTS.get(code)
    if exit_code == 7:
        check = _versions_for_error(err)
        if check.get("versionMismatch"):
            # Say it plainly: a stale host (e.g. one that predates a new [chromecli] key) is fixed by a reload.
            message = f"{message} The running chromecli-host is {check['versions']['host']} but the installed CLI is " \
                      f"{__version__}: {RELOAD}."
            hint = f"{check['reloadHint']} {hint or ''}".strip()
            extra.update(versions=check["versions"], versionMismatch=True)
    die(message, exit_code, hint, error=code, **extra)


def die_down(message: str) -> None:
    die(message, protocol.exit_code("bridge_down"), _down_hint(), error="bridge_down")


def request(op: str, args: dict | None = None, *, timeout: float | None = None) -> Any:
    """The result of `op`, or exit with the reply's error (bridge_down: exit 5)."""
    try:
        reply = call(op, args, timeout=timeout)
    except BridgeDown as exc:
        die_down(str(exc))
    if not reply.get("ok"):
        die_reply(reply)
    return reply.get("result")
