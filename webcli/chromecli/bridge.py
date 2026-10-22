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


def die_reply(reply: dict) -> None:
    err = reply.get("error") if isinstance(reply.get("error"), dict) else {}
    code = str(err.get("code") or "script_error")
    extra = {k: v for k, v in err.items() if k not in ("code", "message", "hint", "exit")}
    die(str(err.get("message") or code), protocol.exit_code(code), err.get("hint") or protocol.ERROR_HINTS.get(code),
        error=code, **extra)


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
